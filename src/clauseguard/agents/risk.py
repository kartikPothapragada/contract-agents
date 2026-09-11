"""Risk agent -- turn categorical severities into a scored escalation decision.

Deliberately contains no LLM call. Its full reasoning is in
``tools/scoring.py``: the model supplies judgement, Python supplies arithmetic
and the threshold. That makes the gate reproducible, unit-testable, and
explainable to an auditor who wants to know why a given contract was or was not
put in front of a lawyer.

Being a node rather than a helper function is a deliberate choice too: it keeps
the score in the interaction trace as a first-class message with its own formula
string, so the escalation decision is legible in the same log as everything else.
"""

from __future__ import annotations

from clauseguard.agents.base import Agent
from clauseguard.orchestration.state import GraphState
from clauseguard.schemas.messages import AgentRole, MessageType, RiskScore
from clauseguard.tools.scoring import score_clause


class RiskAgent(Agent):
    role = AgentRole.RISK

    def run(self, state: GraphState) -> dict:
        profile = state["profile"]
        scores: list[RiskScore] = []
        for assessment in state["assessments"]:
            score, _ = self.call_tool(
                "score_clause",
                {
                    "clause_type": assessment.clause_type,
                    "severity": assessment.severity.value,
                    "value_tier": profile.value_tier.value,
                },
                lambda a=assessment: score_clause(
                    self.playbook, a.clause_type, a.severity, profile.value_tier
                ),
            )
            scores.append(score)
            self.emit(
                MessageType.RISK_SCORE,
                score,
                recipient=AgentRole.VERIFIER,
            )
        return {"scores": scores, "status": "scored"}
