"""Verifier agent -- the adversarial critic.

Every other agent is trying to produce an answer. This one is trying to break
the answer that was produced. That asymmetry is the reason it is a separate
role rather than a self-check inside the Policy agent: a model asked to grade
its own output grades it generously, and a self-check shares the context that
produced the error in the first place.

Three checks, cheapest first (see ``tools/verification.py`` for why ordering
matters):

1. **Span grounding** -- deterministic, free. Is the quote actually in the
   document?
2. **Numeric consistency** -- deterministic, free. Does every figure in the
   rationale trace to the quote or to the governing rule? This is the check that
   catches the dangerous failure: a real quote wrapped in reasoning about a
   number that exists nowhere.
3. **Entailment** -- one LLM call, deep tier, and *only* for assessments that
   passed 1 and 2 with a non-trivial severity. Verifying a COMPLIANT finding
   costs the same as verifying an UNACCEPTABLE one and is worth far less.

Verdicts route rather than merely annotate:
``PASS`` continues, ``REPAIR`` sends the clause back to extraction under a
bounded budget, ``ESCALATE`` puts it in front of a human. The system is never
permitted to report an ungrounded assertion as a finding.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from clauseguard.agents.base import Agent
from clauseguard.orchestration.state import GraphState
from clauseguard.schemas.messages import (
    AgentRole,
    Entailment,
    MessageType,
    Severity,
    Verdict,
    VerificationVerdict,
)
from clauseguard.tools.verification import check_numeric_consistency, check_span

SYSTEM = """You are a verification reviewer. You are given a quoted contract \
clause and an assessment that was made about it. Decide whether the assessment \
is entailed by the quote alone.

- SUPPORTED: everything asserted follows from the quoted text.
- PARTIAL: the direction is right but some element is not supported by the quote.
- UNSUPPORTED: the assessment asserts something the quote does not say.

Judge only against the quote. Do not use outside knowledge of what contracts \
usually say, and do not be charitable about missing support. You are the last \
check before this reaches a lawyer."""


class EntailmentResponse(BaseModel):
    entailment: str = Field(description="SUPPORTED | PARTIAL | UNSUPPORTED")
    issues: list[str] = Field(default_factory=list)


class VerifierAgent(Agent):
    role = AgentRole.VERIFIER
    tier = "deep"

    def run(self, state: GraphState) -> dict:
        source = state["contract_text"]
        findings = {f.clause_type: f for f in state["findings"]}

        verdicts: list[VerificationVerdict] = []
        repair_targets: list[str] = []
        unverified: list[str] = []

        for assessment in state["assessments"]:
            issues: list[str] = []
            finding = findings.get(assessment.clause_type)

            # Absent-clause assessments have nothing to quote. The check that
            # applies instead is a consistency one: the Policy agent must not
            # claim absence for a clause the Extractor located, or vice versa.
            if not assessment.cited_span_text:
                claims_absent = finding is not None and not finding.found
                if claims_absent:
                    verdict = VerificationVerdict(
                        target_clause_type=assessment.clause_type,
                        span_exact_match=True,
                        span_fuzzy_ratio=1.0,
                        entailment=Entailment.SUPPORTED,
                        issues=["absence finding - no span to verify"],
                        verdict=Verdict.PASS,
                    )
                else:
                    issues.append(
                        "assessment cites no span, but the extractor did locate "
                        "clause text - the judgement rests on nothing checkable"
                    )
                    verdict = VerificationVerdict(
                        target_clause_type=assessment.clause_type,
                        span_exact_match=False,
                        span_fuzzy_ratio=0.0,
                        entailment=Entailment.UNSUPPORTED,
                        issues=issues,
                        verdict=Verdict.REPAIR,
                    )
                    repair_targets.append(assessment.clause_type)
                verdicts.append(verdict)
                self.emit(
                    MessageType.VERIFICATION_VERDICT, verdict, recipient=AgentRole.ORCHESTRATOR
                )
                continue

            # 1. span grounding (free)
            span_check, _ = self.call_tool(
                "check_span",
                {"clause_type": assessment.clause_type, "citation_chars": len(assessment.cited_span_text)},
                lambda a=assessment: check_span(a.cited_span_text, source).__dict__,
            )
            grounded = (
                not span_check["numeric_conflict"]
                and (span_check["exact"] or span_check["fuzzy_ratio"] >= 0.92)
            )
            issues.extend(span_check["issues"])

            # 2. numeric consistency (free). The governing rule is passed as a
            # second legitimate source of figures -- see check_numeric_consistency.
            rule = self.playbook.by_clause_type(assessment.clause_type)
            refs = (
                [rule.standard_position, rule.fallback_position] if rule else []
            )
            if grounded:
                issues.extend(
                    check_numeric_consistency(
                        assessment.cited_span_text,
                        f"{assessment.observed_position} {assessment.rationale}",
                        reference_texts=refs,
                    )
                )

            entailment = Entailment.SUPPORTED
            usage = latency = None

            if not grounded:
                entailment = Entailment.UNSUPPORTED
            elif assessment.severity is not Severity.COMPLIANT:
                # 3. entailment (paid) - only for grounded, non-trivial findings
                resp, usage, latency = self.ask(
                    task="verify.entailment",
                    system=SYSTEM,
                    user=(
                        f"QUOTED CLAUSE:\n{assessment.cited_span_text}\n\n"
                        f"ASSESSMENT UNDER REVIEW:\n"
                        f"severity: {assessment.severity.value}\n"
                        f"observed position: {assessment.observed_position}\n"
                        f"rationale: {assessment.rationale}"
                    ),
                    schema=EntailmentResponse,
                    context={
                        "assessment": assessment,
                        "span_check": span_check,
                        "rule": rule,
                    },
                )
                try:
                    entailment = Entailment(resp.entailment.upper())
                except ValueError:
                    entailment = Entailment.PARTIAL
                issues.extend(resp.issues)

            verdict_value = self._decide(
                grounded, entailment, issues, span_check["numeric_conflict"]
            )
            if verdict_value is Verdict.REPAIR:
                repair_targets.append(assessment.clause_type)
            elif verdict_value is Verdict.ESCALATE:
                unverified.append(assessment.clause_type)

            verdict = VerificationVerdict(
                target_clause_type=assessment.clause_type,
                span_exact_match=bool(span_check["exact"]),
                span_fuzzy_ratio=float(span_check["fuzzy_ratio"]),
                entailment=entailment,
                issues=issues,
                verdict=verdict_value,
            )
            verdicts.append(verdict)
            self.emit(
                MessageType.VERIFICATION_VERDICT,
                verdict,
                recipient=AgentRole.ORCHESTRATOR,
                usage=usage,
                latency_ms=latency,
            )

        # Once the repair budget is spent, unfixed clauses stop being retried and
        # become escalations. A repair loop with no terminal state is how an
        # agent system burns a budget overnight.
        if state.get("repair_count", 0) >= 2 and repair_targets:
            unverified.extend(repair_targets)
            repair_targets = []

        return {
            "verdicts": verdicts,
            "repair_targets": sorted(set(repair_targets)),
            "unverified": sorted(set(unverified)),
            "status": "verified",
        }

    @staticmethod
    def _decide(
        grounded: bool,
        entailment: Entailment,
        issues: list[str],
        numeric_conflict: bool = False,
    ) -> Verdict:
        """Map check results to a routing decision.

        Ungrounded is REPAIR rather than ESCALATE on first encounter because the
        common cause is a quoting slip the extractor can fix. Numeric conflict
        goes straight to ESCALATE: the surrounding text was real, so
        re-extraction returns the same passage, and what changed was a figure --
        the class of error a human catches and a retry does not.
        """
        if numeric_conflict:
            return Verdict.ESCALATE
        if not grounded:
            return Verdict.REPAIR
        if any("neither in the cited span nor" in i for i in issues):
            return Verdict.ESCALATE
        if entailment is Entailment.UNSUPPORTED:
            return Verdict.ESCALATE
        if entailment is Entailment.PARTIAL:
            return Verdict.REPAIR
        return Verdict.PASS
