"""Policy agent -- compare observed clause language against the playbook.

This is the only agent that runs on the deep tier, and the only one doing
genuine legal reasoning. Everything upstream narrows the problem so that this
agent sees one clause and one rule at a time; everything downstream checks or
packages what it produced.

It reaches the playbook through the retrieval tool rather than a prompt dump
(see ``tools/retrieval.py``), and it is required to cite the verbatim span its
judgement rests on. That citation requirement is not documentation -- it is the
input to the verifier, and an assessment without one is unfalsifiable.

Absence handling. A clause that is missing is assessed, not skipped. "No
liability cap anywhere in this MSA" is the single highest-value finding the
system can produce, and a pipeline that only reasons over text it found would
silently return a clean report for the worst contract it ever sees.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from clauseguard.agents.base import Agent
from clauseguard.orchestration.state import GraphState
from clauseguard.schemas.messages import (
    AgentRole,
    DeviationAssessment,
    MessageType,
    Severity,
)

SYSTEM = """You are a commercial contracts counsel at Northwind Industries \
reviewing vendor paper against the company playbook.

For the clause you are given, decide the severity of any deviation:

- COMPLIANT: matches the standard position, or falls within the pre-approved \
fallback.
- MINOR: departs from the standard position in a way that is commercially \
tolerable and does not shift material risk.
- MATERIAL: shifts real risk onto Northwind, or removes a protection the \
playbook requires. Needs counsel review.
- UNACCEPTABLE: hits one of the stated never-acceptable triggers.

Rules you must follow:
- Judge only against the rule you are given. Do not import positions from \
general legal knowledge or from other clauses.
- cited_span_text must be a verbatim quote from the clause text supplied. It is \
checked against the source document character by character. Do not reconstruct \
it from memory, and do not tidy its punctuation.
- Every number you state in your rationale must appear in the clause text. If \
the clause says three months, do not write twelve.
- If the clause is absent, judge the consequence of its absence.
- suggested_redline should be language Northwind would propose, not a \
description of what to change."""


class PolicyResponse(BaseModel):
    severity: str = Field(description="COMPLIANT | MINOR | MATERIAL | UNACCEPTABLE")
    observed_position: str = Field(description="What the contract actually says, in one sentence")
    rationale: str
    cited_span_text: str = Field(default="", description="Verbatim quote supporting the judgement")
    suggested_redline: str | None = None
    triggered_rule_text: str | None = Field(
        default=None, description="Which standard/fallback/trigger line was decisive"
    )


class PolicyAgent(Agent):
    role = AgentRole.POLICY
    tier = "deep"  # the one place worth paying for a stronger model

    def run(self, state: GraphState) -> dict:
        profile = state["profile"]
        findings = state["findings"]
        targets = state.get("repair_targets") or [f.clause_type for f in findings]
        existing = {a.clause_type: a for a in state.get("assessments", [])}

        assessments: list[DeviationAssessment] = []
        for finding in findings:
            if finding.clause_type not in targets and finding.clause_type in existing:
                assessments.append(existing[finding.clause_type])
                continue

            # Retrieval tool: find the governing rule from the observed language,
            # not from the clause-type label. The label came from a model; the
            # clause text came from the document.
            probe = (
                finding.span.text[:600]
                if finding.span
                else finding.clause_type.replace("_", " ")
            )
            hits, call_msg = self.call_tool(
                "playbook_retrieval",
                {"clause_type": finding.clause_type, "k": 3, "mode": "hybrid"},
                lambda p=probe: [
                    {"rule_id": r.rule_id, "clause_type": r.clause_type, "rank": h.rank}
                    for r, h in self.playbook.retrieve(p, k=3)
                ],
            )

            # Prefer the exact clause-type rule; fall back to the top fused hit.
            # Disagreement between the two is itself worth recording: it usually
            # means the extractor labelled a clause wrongly.
            rule = self.playbook.by_clause_type(finding.clause_type)
            if rule is None and hits:
                rule = self.playbook.rules[hits[0]["rule_id"]]
            if rule is None:
                self.emit_error(
                    "no_applicable_rule",
                    f"no playbook rule for {finding.clause_type}; clause skipped",
                )
                continue
            if hits and hits[0]["rule_id"] != rule.rule_id:
                self.emit_error(
                    "rule_retrieval_disagreement",
                    f"{finding.clause_type}: label maps to {rule.rule_id} but "
                    f"retrieval ranked {hits[0]['rule_id']} first on the clause "
                    "text -- possible clause-type mislabel upstream",
                    recoverable=True,
                )

            if finding.found and finding.span:
                clause_block = f"CLAUSE TEXT AS IT APPEARS IN THE CONTRACT:\n{finding.span.text}"
            else:
                clause_block = (
                    "CLAUSE STATUS: ABSENT. No language governing this clause type "
                    f"was located in the contract. Extractor notes: "
                    f"{finding.notes or 'none'}\n"
                    "Judge the consequence of this absence for Northwind. "
                    "Leave cited_span_text empty -- there is nothing to quote."
                )

            resp, usage, latency = self.ask(
                task="policy.assess",
                system=SYSTEM,
                user=(
                    f"CONTRACT TYPE: {profile.doc_type} with {profile.counterparty or 'vendor'}\n"
                    f"PLAYBOOK v{self.playbook.version}\n\n"
                    f"{rule.as_prompt_block()}\n\n{clause_block}"
                ),
                schema=PolicyResponse,
                context={"finding": finding, "rule": rule, "profile": profile},
            )

            try:
                severity = Severity(resp.severity.upper())
            except ValueError:
                # An unparseable severity is escalated, not defaulted to
                # COMPLIANT. Failing safe here means failing loud.
                severity = Severity.MATERIAL
                self.emit_error(
                    "unparseable_severity",
                    f"{finding.clause_type}: model returned severity "
                    f"{resp.severity!r}; coerced to MATERIAL for human review",
                )

            assessment = DeviationAssessment(
                clause_type=finding.clause_type,
                rule_id=rule.rule_id,
                rule_title=rule.title,
                standard_position=rule.standard_position,
                observed_position=resp.observed_position,
                severity=severity,
                rationale=resp.rationale,
                suggested_redline=resp.suggested_redline,
                cited_span_text=resp.cited_span_text or None,
            )
            assessments.append(assessment)
            self.emit(
                MessageType.DEVIATION_ASSESSMENT,
                assessment,
                recipient=AgentRole.RISK,
                parent=call_msg,
                usage=usage,
                latency_ms=latency,
            )

        return {"assessments": assessments, "status": "assessed"}
