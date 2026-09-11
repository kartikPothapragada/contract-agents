"""Intake agent -- classify the document and decide what is worth analysing.

Role boundary: intake answers *what is this and which rules could possibly
apply*. It never assesses risk. Keeping classification separate from assessment
means a misclassified document produces a visibly wrong profile in the trace,
rather than a plausible-looking risk report built on a wrong premise.

This agent is also where the system's main cost lever sits. The playbook has 14
rules; a given NDA engages maybe four of them. Scoping here means the expensive
deep-tier Policy agent runs on a handful of clauses instead of all fourteen --
roughly a 60-70% reduction in deep-tier calls on the corpus used for evaluation.
The safeguard against over-pruning is ``ALWAYS_IN_SCOPE``: three clause types
whose absence is itself a finding, so they are never dropped by a model's
scoping judgement.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from clauseguard.agents.base import Agent
from clauseguard.orchestration.state import GraphState
from clauseguard.schemas.messages import AgentRole, DocumentProfile, MessageType, ValueTier
from clauseguard.tools.segmentation import segment_contract

ALWAYS_IN_SCOPE = ("LIMITATION_OF_LIABILITY", "INDEMNIFICATION", "DATA_PROTECTION")
"""Never pruned by the model.

Their *absence* from a contract is a material finding in its own right -- an
agreement with no liability cap is the single worst outcome this system exists
to catch. Letting a scoping model decide these are 'not applicable' would make
the system's worst failure mode also its quietest.
"""

SYSTEM = """You are the intake analyst in a contract review system at Northwind \
Industries. You classify inbound vendor contracts and decide which playbook \
clause types are plausibly engaged.

Be conservative when scoping: including a clause type that turns out to be \
absent costs one cheap lookup, whereas excluding one that is present means a \
risk is never reviewed. When in doubt, include it."""


class IntakeResponse(BaseModel):
    doc_type: str = Field(description="MSA | DPA | SOW | NDA | AMENDMENT | UNKNOWN")
    counterparty: str | None = Field(default=None, description="Vendor legal entity name")
    governing_law: str | None = None
    effective_date: str | None = Field(default=None, description="ISO date if stated")
    value_tier: str = Field(default="MID", description="SMALL | MID | STRATEGIC")
    in_scope_clause_types: list[str] = Field(default_factory=list)
    classification_confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reasoning: str = ""


class IntakeAgent(Agent):
    role = AgentRole.INTAKE
    tier = "fast"  # classification is a cheap-tier task; no deep reasoning needed

    def run(self, state: GraphState) -> dict:
        text = state["contract_text"]

        seg, _ = self.call_tool(
            "segment_contract",
            {"chars": len(text)},
            lambda: segment_contract(text).as_tool_result(),
        )
        result = segment_contract(text)
        headings = [f"{s.ref} {s.heading}".strip() for s in result.sections][:40]

        known_types = self.playbook.clause_types()
        user = (
            f"PLAYBOOK CLAUSE TYPES (choose only from this list):\n"
            f"{', '.join(known_types)}\n\n"
            f"SECTION HEADINGS:\n" + "\n".join(f"- {h}" for h in headings) + "\n\n"
            f"FIRST 3000 CHARACTERS OF THE CONTRACT:\n{text[:3000]}"
        )

        resp, usage, latency = self.ask(
            task="intake.classify",
            system=SYSTEM,
            user=user,
            schema=IntakeResponse,
            context={"text": text, "headings": headings, "clause_types": known_types},
        )

        # Reconcile the model's scope against the playbook. Two defences:
        # drop anything not in the playbook (a hallucinated clause type would
        # otherwise propagate into a rule lookup that quietly returns nothing),
        # and union in the always-on set.
        scoped = [c for c in resp.in_scope_clause_types if c in known_types]
        dropped = [c for c in resp.in_scope_clause_types if c not in known_types]
        if dropped:
            self.emit_error(
                "unknown_clause_type",
                f"intake proposed clause types absent from playbook v"
                f"{self.playbook.version}: {dropped}",
                recoverable=True,
            )
        scoped = sorted(set(scoped) | set(ALWAYS_IN_SCOPE))

        try:
            tier = ValueTier(resp.value_tier.upper())
        except ValueError:
            tier = ValueTier.MID
        if state.get("value_tier"):
            tier = ValueTier(state["value_tier"])  # explicit caller input wins

        profile = DocumentProfile(
            doc_id=state["doc_id"],
            doc_type=resp.doc_type,
            counterparty=resp.counterparty,
            governing_law=resp.governing_law,
            effective_date=resp.effective_date,
            value_tier=tier,
            section_count=len(result.sections),
            in_scope_clause_types=scoped,
            classification_confidence=min(
                resp.classification_confidence, result.confidence
            ),
        )
        self.emit(
            MessageType.DOCUMENT_PROFILE,
            profile,
            recipient=AgentRole.EXTRACTOR,
            usage=usage,
            latency_ms=latency,
        )
        return {
            "profile": profile,
            "sections": seg["sections"],
            "repair_count": 0,
            "status": "profiled",
        }
