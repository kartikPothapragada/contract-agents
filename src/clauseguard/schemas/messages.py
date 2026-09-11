"""Inter-agent message schema (v1.0).

Every unit of communication between agents is an :class:`AgentMessage` envelope
carrying a discriminated-union ``payload``. Nothing is passed between agents as a
bare dict or free-form string: the envelope is the contract.

Design notes
------------
* ``schema_version`` is explicit so a rolling upgrade can run two agent versions
  side by side (see write-up, Q2 / production considerations).
* ``trace_id`` correlates every message emitted for one contract run;
  ``parent_msg_id`` reconstructs the causal DAG, not just a flat log.
* ``usage`` is attached at the envelope level rather than buried in agent code so
  cost accounting is a property of the transport, not of per-agent diligence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    INTAKE = "intake"
    EXTRACTOR = "extractor"
    POLICY = "policy"
    RISK = "risk"
    VERIFIER = "verifier"
    DRAFTER = "drafter"
    HUMAN = "human"


class MessageType(str, Enum):
    DOCUMENT_PROFILE = "document.profile"
    CLAUSE_FINDING = "clause.finding"
    DEVIATION_ASSESSMENT = "deviation.assessment"
    RISK_SCORE = "risk.score"
    VERIFICATION_VERDICT = "verification.verdict"
    ESCALATION_REQUEST = "escalation.request"
    HUMAN_DECISION = "human.decision"
    REDLINE_MEMO = "redline.memo"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    AGENT_ERROR = "agent.error"


class Severity(str, Enum):
    """Deviation severity. Ordinal -- comparison order drives routing."""

    COMPLIANT = "COMPLIANT"
    MINOR = "MINOR"
    MATERIAL = "MATERIAL"
    UNACCEPTABLE = "UNACCEPTABLE"

    @property
    def rank(self) -> int:
        return {"COMPLIANT": 0, "MINOR": 1, "MATERIAL": 2, "UNACCEPTABLE": 3}[self.value]


class Entailment(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"


class Verdict(str, Enum):
    PASS = "PASS"
    REPAIR = "REPAIR"
    ESCALATE = "ESCALATE"


class HumanAction(str, Enum):
    APPROVE = "APPROVE"
    OVERRIDE = "OVERRIDE"
    REJECT = "REJECT"


class ValueTier(str, Enum):
    """Contract value band. Drives the risk multiplier."""

    SMALL = "SMALL"          # < $100k TCV
    MID = "MID"              # $100k - $1M TCV
    STRATEGIC = "STRATEGIC"  # > $1M TCV


# --------------------------------------------------------------------------- #
# Payloads (discriminated on ``kind``)
# --------------------------------------------------------------------------- #
class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentProfile(_Payload):
    kind: Literal["document.profile"] = "document.profile"
    doc_id: str
    doc_type: str = Field(description="MSA | DPA | SOW | NDA | AMENDMENT | UNKNOWN")
    counterparty: str | None = None
    governing_law: str | None = None
    effective_date: str | None = None
    value_tier: ValueTier = ValueTier.MID
    section_count: int = 0
    in_scope_clause_types: list[str] = Field(default_factory=list)
    classification_confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class ClauseSpan(_Payload):
    kind: Literal["clause.span"] = "clause.span"
    text: str
    char_start: int
    char_end: int
    section_ref: str | None = None


class ClauseFinding(_Payload):
    kind: Literal["clause.finding"] = "clause.finding"
    clause_type: str
    found: bool
    span: ClauseSpan | None = None
    extraction_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    retrieval_section_refs: list[str] = Field(default_factory=list)
    notes: str | None = None


class DeviationAssessment(_Payload):
    kind: Literal["deviation.assessment"] = "deviation.assessment"
    clause_type: str
    rule_id: str
    rule_title: str
    standard_position: str
    observed_position: str
    severity: Severity
    rationale: str
    suggested_redline: str | None = None
    cited_span_text: str | None = Field(
        default=None,
        description="Verbatim quote the assessment rests on. The verifier checks "
        "this substring against the source document -- the single most effective "
        "hallucination trap in the system.",
    )


class RiskScore(_Payload):
    kind: Literal["risk.score"] = "risk.score"
    clause_type: str
    severity: Severity
    severity_points: float
    criticality_weight: float
    value_multiplier: float
    score: float
    escalate: bool
    formula: str


class VerificationVerdict(_Payload):
    kind: Literal["verification.verdict"] = "verification.verdict"
    target_clause_type: str
    span_exact_match: bool
    span_fuzzy_ratio: float = Field(ge=0.0, le=1.0, default=0.0)
    entailment: Entailment = Entailment.SUPPORTED
    issues: list[str] = Field(default_factory=list)
    verdict: Verdict = Verdict.PASS


class EscalationRequest(_Payload):
    kind: Literal["escalation.request"] = "escalation.request"
    reasons: list[str]
    clause_types: list[str]
    aggregate_risk: float
    recommended_action: str
    review_packet: list[dict[str, Any]] = Field(default_factory=list)


class HumanDecision(_Payload):
    kind: Literal["human.decision"] = "human.decision"
    action: HumanAction
    reviewer: str
    note: str | None = None
    severity_overrides: dict[str, Severity] = Field(
        default_factory=dict,
        description="clause_type -> reviewer-set severity. Recorded separately "
        "from the model's own output so disagreement is measurable.",
    )


class RedlineMemo(_Payload):
    kind: Literal["redline.memo"] = "redline.memo"
    doc_id: str
    executive_summary: str
    deviations: list[dict[str, Any]] = Field(default_factory=list)
    negotiation_priority: list[str] = Field(default_factory=list)
    aggregate_risk: float = 0.0
    residual_risk_note: str | None = None
    human_reviewed: bool = False
    reviewer: str | None = None


class ToolCall(_Payload):
    kind: Literal["tool.call"] = "tool.call"
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(_Payload):
    kind: Literal["tool.result"] = "tool.result"
    tool_name: str
    ok: bool
    result: Any = None
    error: str | None = None


class AgentError(_Payload):
    kind: Literal["agent.error"] = "agent.error"
    error_type: str
    message: str
    recoverable: bool = True


Payload = Annotated[
    Union[
        DocumentProfile,
        ClauseSpan,
        ClauseFinding,
        DeviationAssessment,
        RiskScore,
        VerificationVerdict,
        EscalationRequest,
        HumanDecision,
        RedlineMemo,
        ToolCall,
        ToolResult,
        AgentError,
    ],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "n/a"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            model="aggregate",
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            usd=round(self.usd + other.usd, 6),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    msg_id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    trace_id: str
    parent_msg_id: str | None = None
    sender: AgentRole
    recipient: AgentRole
    msg_type: MessageType
    created_at: str = Field(default_factory=_now)
    payload: Payload
    usage: Usage | None = None
    latency_ms: float | None = None

    def summary(self) -> str:
        return (
            f"{self.sender.value:>12} -> {self.recipient.value:<12} "
            f"[{self.msg_type.value}] {self.msg_id}"
        )
