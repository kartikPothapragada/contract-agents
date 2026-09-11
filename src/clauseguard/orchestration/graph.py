"""LangGraph orchestration.

Why LangGraph over CrewAI / AutoGen / a custom loop (write-up Q2)
----------------------------------------------------------------
The control flow this problem needs is a *state machine with a suspend point*,
not a conversation. Three requirements drove the choice:

1. **The human gate must suspend and resume across a process boundary.** A
   lawyer does not review a contract inside a 30-second function call; the run
   has to stop, persist, and resume hours later possibly in a different process.
   LangGraph's ``interrupt()`` plus a checkpointer is exactly this primitive.
   CrewAI's human input is a blocking console prompt, and AutoGen's is a turn in
   a chat loop -- both model the human as a fast participant rather than an
   asynchronous approver.
2. **Routing must be explicit and inspectable.** Escalate-vs-repair-vs-continue
   is a business rule with an audit requirement behind it. Conditional edges put
   that rule in one readable function. A conversational framework would leave
   the decision inside a model's choice of who to speak to next, which is not
   something you can show a compliance reviewer.
3. **Bounded loops.** The repair cycle needs a hard budget. An explicit edge back
   to the extractor with a counter on the state is trivially auditable; an
   agent-decides-when-to-stop loop is not.

What was traded away, honestly: CrewAI would have reached a demo faster and its
role abstraction reads more naturally. AutoGen's group chat would have produced
richer emergent inter-agent dialogue. Both were rejected because this problem
wants *less* emergence, not more -- the value of a contract-risk gate is that it
behaves the same way every time.
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from clauseguard.agents.drafter import DrafterAgent
from clauseguard.agents.extractor import ExtractorAgent
from clauseguard.agents.intake import IntakeAgent
from clauseguard.agents.policy import PolicyAgent
from clauseguard.agents.risk import RiskAgent
from clauseguard.agents.verifier import VerifierAgent
from clauseguard.llm.backends import LLMClient
from clauseguard.orchestration.state import MAX_REPAIRS, GraphState
from clauseguard.orchestration.trace import TraceBus
from clauseguard.schemas.messages import (
    AgentError,
    AgentRole,
    EscalationRequest,
    HumanAction,
    HumanDecision,
    MessageType,
    Severity,
)
from clauseguard.schemas.messages import (
    ClauseFinding,
    ClauseSpan,
    DeviationAssessment,
    DocumentProfile,
    Entailment,
    RedlineMemo,
    RiskScore,
    ValueTier,
    Verdict,
    VerificationVerdict,
)
from clauseguard.tools.playbook import PlaybookStore
from clauseguard.tools.scoring import aggregate

CHECKPOINTED_TYPES = (
    DocumentProfile,
    ClauseSpan,
    ClauseFinding,
    DeviationAssessment,
    RiskScore,
    VerificationVerdict,
    EscalationRequest,
    HumanDecision,
    RedlineMemo,
    Severity,
    Entailment,
    Verdict,
    HumanAction,
    ValueTier,
)
"""Types the checkpointer may rehydrate.

Enumerated rather than allowing the whole module: a checkpoint is untrusted
input on resume, and an allowlist that names classes cannot be widened by adding
a new class to the module. The cost is one line per payload type, paid once.
"""


def build_graph(
    llm: LLMClient,
    playbook: PlaybookStore,
    bus: TraceBus,
    human_responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
):
    """Compile the agent graph.

    ``human_responder`` is an escape hatch for scripted and evaluation runs. When
    it is None the graph genuinely suspends on ``interrupt()`` and must be
    resumed with a ``Command``; that is the production path. When supplied, the
    same packet is handed to a callable instead, so a 20-contract evaluation
    sweep does not require 20 console prompts. The decision is recorded
    identically either way, so a scripted run and a real one produce the same
    trace shape.
    """
    intake = IntakeAgent(llm, playbook, bus)
    extractor = ExtractorAgent(llm, playbook, bus)
    policy = PolicyAgent(llm, playbook, bus)
    risk = RiskAgent(llm, playbook, bus)
    verifier = VerifierAgent(llm, playbook, bus)
    drafter = DrafterAgent(llm, playbook, bus)

    # ----------------------------------------------------------------- nodes #
    def node_intake(state: GraphState) -> dict:
        return intake.run(state)

    def node_extract(state: GraphState) -> dict:
        return extractor.run(state)

    def node_policy(state: GraphState) -> dict:
        return policy.run(state)

    def node_risk(state: GraphState) -> dict:
        return risk.run(state)

    def node_verify(state: GraphState) -> dict:
        return verifier.run(state)

    def node_escalation_check(state: GraphState) -> dict:
        """Combine risk scores and verification failures into one decision."""
        agg = aggregate(playbook, state.get("scores", []), state.get("unverified", []))
        if not agg.escalate:
            return {"escalation": None, "status": "auto_approved"}

        assessments = {a.clause_type: a for a in state["assessments"]}
        scores = {s.clause_type: s for s in state.get("scores", [])}
        verdicts = {v.target_clause_type: v for v in state.get("verdicts", [])}
        packet = []
        for clause_type in agg.escalating_clause_types:
            a = assessments.get(clause_type)
            if not a:
                continue
            packet.append(
                {
                    "clause_type": clause_type,
                    "rule_id": a.rule_id,
                    "rule_title": a.rule_title,
                    "severity": a.severity.value,
                    "risk_score": scores[clause_type].score if clause_type in scores else None,
                    "standard_position": a.standard_position,
                    "observed_position": a.observed_position,
                    "rationale": a.rationale,
                    "cited_span": a.cited_span_text,
                    "suggested_redline": a.suggested_redline,
                    "unverified": clause_type in state.get("unverified", []),
                    # The reviewer is told *why* the system distrusts itself,
                    # not merely that it does.
                    "verification_issues": (
                        verdicts[clause_type].issues if clause_type in verdicts else []
                    ),
                    "owner": (
                        playbook.by_clause_type(clause_type).owner
                        if playbook.by_clause_type(clause_type)
                        else "general-counsel@northwind.example"
                    ),
                }
            )

        worst = max(
            (assessments[c].severity for c in agg.escalating_clause_types if c in assessments),
            key=lambda s: s.rank,
            default=Severity.MATERIAL,
        )
        request = EscalationRequest(
            reasons=agg.reasons,
            clause_types=agg.escalating_clause_types,
            aggregate_risk=agg.total,
            recommended_action=(
                "Do not sign. Counsel review required before counter-signature."
                if worst is Severity.UNACCEPTABLE
                else "Counsel review recommended before counter-signature."
            ),
            review_packet=packet,
        )
        bus.emit(
            AgentRole.ORCHESTRATOR,
            AgentRole.HUMAN,
            MessageType.ESCALATION_REQUEST,
            request,
        )
        return {"escalation": request, "status": "awaiting_human"}

    def node_human_gate(state: GraphState) -> dict:
        """HUMAN-IN-THE-LOOP CHECKPOINT.

        Everything the reviewer needs is in the packet: the standard position,
        what the contract actually says, the verbatim span, the model's
        reasoning, the computed score, and whether the system could verify its
        own claim. A reviewer who has to open the contract to understand the
        escalation has not been given a reviewable artefact.
        """
        escalation = state["escalation"]
        packet = {
            "doc_id": state["doc_id"],
            "aggregate_risk": escalation.aggregate_risk,
            "reasons": escalation.reasons,
            "recommended_action": escalation.recommended_action,
            "items": escalation.review_packet,
            "actions_available": ["APPROVE", "OVERRIDE", "REJECT"],
        }

        raw = human_responder(packet) if human_responder else interrupt(packet)

        try:
            action = HumanAction(str(raw.get("action", "APPROVE")).upper())
        except ValueError:
            action = HumanAction.APPROVE
        overrides = {
            k: Severity(str(v).upper()) for k, v in (raw.get("severity_overrides") or {}).items()
        }
        decision = HumanDecision(
            action=action,
            reviewer=raw.get("reviewer", "unknown"),
            note=raw.get("note"),
            severity_overrides=overrides,
        )
        bus.emit(
            AgentRole.HUMAN,
            AgentRole.ORCHESTRATOR,
            MessageType.HUMAN_DECISION,
            decision,
        )

        updates: dict[str, Any] = {"human_decision": decision, "status": "human_reviewed"}
        if action is HumanAction.REJECT:
            # A rejection is routed as a repair with the reviewer's scope, which
            # is the whole reason the gate exists: the human redirects the
            # system rather than merely vetoing its output.
            targets = raw.get("recheck_clause_types") or escalation.clause_types
            updates["repair_targets"] = list(targets)
            updates["repair_count"] = state.get("repair_count", 0) + 1
        return updates

    def node_draft(state: GraphState) -> dict:
        return drafter.run(state)

    # ------------------------------------------------------------------ edges #
    def route_after_verify(state: GraphState) -> str:
        if state.get("repair_targets") and state.get("repair_count", 0) < MAX_REPAIRS:
            return "repair"
        return "escalation_check"

    def node_repair(state: GraphState) -> dict:
        """Increment the budget on the way back round the loop.

        Counting here rather than in the extractor means the budget is spent by
        the act of looping, so no agent can reset it by being re-entered.
        """
        n = state.get("repair_count", 0) + 1
        bus.emit(
            AgentRole.ORCHESTRATOR,
            AgentRole.EXTRACTOR,
            MessageType.AGENT_ERROR,
            AgentError(
                error_type="verification_repair",
                message=(
                    f"repair pass {n}/{MAX_REPAIRS} for "
                    f"{state.get('repair_targets')}: re-extracting clauses whose "
                    "assessments could not be grounded"
                ),
                recoverable=True,
            ),
        )
        return {"repair_count": n, "status": f"repairing_{n}"}

    def route_after_escalation_check(state: GraphState) -> str:
        return "human_gate" if state.get("escalation") else "draft"

    def route_after_human(state: GraphState) -> str:
        decision = state.get("human_decision")
        if (
            decision
            and decision.action is HumanAction.REJECT
            and state.get("repair_count", 0) <= MAX_REPAIRS
        ):
            return "repair"
        return "draft"

    graph = StateGraph(GraphState)
    graph.add_node("intake", node_intake)
    graph.add_node("extract", node_extract)
    graph.add_node("policy", node_policy)
    graph.add_node("risk", node_risk)
    graph.add_node("verify", node_verify)
    graph.add_node("repair", node_repair)
    graph.add_node("escalation_check", node_escalation_check)
    graph.add_node("human_gate", node_human_gate)
    graph.add_node("draft", node_draft)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "extract")
    graph.add_edge("extract", "policy")
    graph.add_edge("policy", "risk")
    graph.add_edge("risk", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"repair": "repair", "escalation_check": "escalation_check"},
    )
    graph.add_edge("repair", "extract")
    graph.add_conditional_edges(
        "escalation_check",
        route_after_escalation_check,
        {"human_gate": "human_gate", "draft": "draft"},
    )
    graph.add_conditional_edges(
        "human_gate", route_after_human, {"repair": "repair", "draft": "draft"}
    )
    graph.add_edge("draft", END)

    # State carries typed payload objects, so the checkpointer is told explicitly
    # which module may be deserialised. Without the allowlist LangGraph emits a
    # deprecation warning per type; with a blanket allowance it would happily
    # rehydrate arbitrary classes from a checkpoint. Naming the one module keeps
    # both properties.
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINTED_TYPES)
    return graph.compile(checkpointer=MemorySaver(serde=serde))
