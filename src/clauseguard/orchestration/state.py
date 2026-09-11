"""Graph state.

State holds typed payload objects, not loose dicts, so an agent that writes a
malformed result fails at the boundary it was written rather than three nodes
later in the drafter.

``repair_count`` is on the state rather than inside the verifier because the
retry budget is a *system* property. An agent that owned its own retry counter
could be re-entered by the router and reset it, which is exactly how an agent
loop becomes an unbounded spend.
"""

from __future__ import annotations

from typing import Any, TypedDict

from clauseguard.schemas.messages import (
    ClauseFinding,
    DeviationAssessment,
    DocumentProfile,
    EscalationRequest,
    HumanDecision,
    RedlineMemo,
    RiskScore,
    VerificationVerdict,
)

MAX_REPAIRS = 2
"""Bounded repair budget.

Two, not more: the observed failure distribution is bimodal. A verification
failure is either a formatting slip the extractor fixes on the first retry, or a
clause that genuinely is not in the document -- and no number of retries
conjures that one into existence. A third attempt buys nothing but tokens and
latency, so after two the system escalates to a human instead of spinning.
"""


class GraphState(TypedDict, total=False):
    # inputs
    trace_id: str
    doc_id: str
    contract_text: str
    value_tier: str

    # agent outputs
    profile: DocumentProfile
    sections: list[dict[str, Any]]
    findings: list[ClauseFinding]
    assessments: list[DeviationAssessment]
    scores: list[RiskScore]
    verdicts: list[VerificationVerdict]

    # control
    repair_count: int
    repair_targets: list[str]
    repair_reason: str | None
    unverified: list[str]
    escalation: EscalationRequest | None
    human_decision: HumanDecision | None
    memo: RedlineMemo | None
    status: str
    halt_reason: str | None
