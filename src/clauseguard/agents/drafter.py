"""Drafter agent -- assemble the counsel-ready memo.

Runs last, and is given only material that has already been verified or
explicitly signed off by a human. It has no access to the raw contract text.

That restriction is the point. A drafter that could re-read the contract would
be able to reintroduce an assertion the verifier just rejected, which would make
every check upstream advisory. Narrowing its input to the approved record means
the memo can only ever be a re-presentation of findings that survived the gate.

The negotiation ordering and the prose are the model's contribution. The numbers
are not: the aggregate risk and the per-clause scores are passed through from
the deterministic scorer and overwritten after generation, so the memo cannot
disagree with the audit trail about its own figures.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from clauseguard.agents.base import Agent
from clauseguard.orchestration.state import GraphState
from clauseguard.schemas.messages import (
    AgentRole,
    HumanAction,
    MessageType,
    RedlineMemo,
    Severity,
)

SYSTEM = """You are drafting a contract review memo for the Northwind deal team.

Audience: a commercial lead who is not a lawyer and has ten minutes. \
Lead with the commercial consequence, not the clause number.

- The executive summary is at most four sentences and states plainly whether \
this contract can be signed as drafted.
- negotiation_priority orders the clause types by what to fight for first, \
weighing severity against how winnable the point usually is. Ordering by \
severity alone is not useful advice.
- Do not restate every clause. Silence on a clause means it was acceptable.
- Where a human reviewer overrode the system, respect the override and do not \
re-argue it."""


class DrafterResponse(BaseModel):
    executive_summary: str
    negotiation_priority: list[str] = Field(default_factory=list)
    residual_risk_note: str | None = None


class DrafterAgent(Agent):
    role = AgentRole.DRAFTER
    tier = "deep"

    def run(self, state: GraphState) -> dict:
        profile = state["profile"]
        assessments = {a.clause_type: a for a in state["assessments"]}
        scores = {s.clause_type: s for s in state.get("scores", [])}
        decision = state.get("human_decision")
        verdicts = {v.target_clause_type: v for v in state.get("verdicts", [])}

        # Apply reviewer overrides before drafting. Recorded as a distinct field
        # so model/human disagreement stays measurable rather than being
        # laundered into the memo.
        overrides = decision.severity_overrides if decision else {}
        rows: list[dict] = []
        for clause_type, a in assessments.items():
            effective = overrides.get(clause_type, a.severity)
            verdict = verdicts.get(clause_type)
            rows.append(
                {
                    "clause_type": clause_type,
                    "rule_id": a.rule_id,
                    "severity": effective.value,
                    "model_severity": a.severity.value,
                    "human_overridden": clause_type in overrides,
                    "observed_position": a.observed_position,
                    "rationale": a.rationale,
                    "suggested_redline": a.suggested_redline,
                    "risk_score": scores[clause_type].score if clause_type in scores else None,
                    "verification": verdict.verdict.value if verdict else "NOT_VERIFIED",
                    "cited_span": (a.cited_span_text or "")[:300] or None,
                }
            )

        deviations = [r for r in rows if r["severity"] != Severity.COMPLIANT.value]
        aggregate = round(
            sum(
                scores[r["clause_type"]].score
                for r in rows
                if r["clause_type"] in scores
            ),
            3,
        )

        brief = "\n\n".join(
            f"{r['clause_type']} [{r['severity']}"
            + (" - HUMAN OVERRIDE" if r["human_overridden"] else "")
            + f"] (rule {r['rule_id']}, risk {r['risk_score']})\n"
            f"observed: {r['observed_position']}\n"
            f"rationale: {r['rationale']}"
            for r in deviations
        ) or "No deviations from the playbook were found."

        resp, usage, latency = self.ask(
            task="draft.memo",
            system=SYSTEM,
            user=(
                f"CONTRACT: {profile.doc_type} with {profile.counterparty or 'vendor'}\n"
                f"VALUE TIER: {profile.value_tier.value}\n"
                f"AGGREGATE RISK SCORE: {aggregate}\n"
                f"HUMAN REVIEW: "
                f"{decision.action.value + ' by ' + decision.reviewer if decision else 'not required'}\n"
                f"{'REVIEWER NOTE: ' + decision.note if decision and decision.note else ''}\n\n"
                f"VERIFIED DEVIATIONS:\n{brief}"
            ),
            schema=DrafterResponse,
            context={"rows": rows, "profile": profile, "aggregate": aggregate, "decision": decision},
        )

        # Keep only clause types that actually exist; a hallucinated entry in the
        # priority list would send someone to negotiate a clause nobody assessed.
        priority = [c for c in resp.negotiation_priority if c in assessments]
        priority += [
            r["clause_type"]
            for r in sorted(deviations, key=lambda x: -(x["risk_score"] or 0))
            if r["clause_type"] not in priority
        ]

        memo = RedlineMemo(
            doc_id=profile.doc_id,
            executive_summary=resp.executive_summary,
            deviations=sorted(deviations, key=lambda r: -(r["risk_score"] or 0)),
            negotiation_priority=priority,
            aggregate_risk=aggregate,
            residual_risk_note=resp.residual_risk_note,
            human_reviewed=decision is not None,
            reviewer=decision.reviewer if decision else None,
        )
        self.emit(
            MessageType.REDLINE_MEMO,
            memo,
            recipient=AgentRole.HUMAN,
            usage=usage,
            latency_ms=latency,
        )
        status = (
            "rejected_by_reviewer"
            if decision and decision.action is HumanAction.REJECT
            else "complete"
        )
        return {"memo": memo, "status": status}
