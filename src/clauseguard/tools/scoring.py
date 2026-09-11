"""Deterministic risk scoring.

This is the one judgement in the system that an LLM is explicitly *not* allowed
to make.

Rationale (write-up Q4). The escalation threshold decides whether a human
lawyer sees a contract. If an LLM computes that number, three failure modes open
at once: arithmetic drift, prompt-sensitivity (the same clause scoring 5.8 on
one run and 6.2 on the next, flipping the gate), and unauditability -- a
regulator asking "why was this not reviewed?" cannot be answered with a
sampled token. So the model supplies *categorical judgement* (severity), and
Python supplies the *arithmetic and the threshold*. That split keeps the gate
reproducible and lets the whole escalation policy be unit-tested.

    score = severity_points x criticality_weight x value_multiplier

Every factor is sourced from the versioned playbook, not hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass

from clauseguard.schemas.messages import RiskScore, Severity, ValueTier
from clauseguard.tools.playbook import PlaybookStore


@dataclass
class AggregateRisk:
    total: float
    max_clause: float
    escalate: bool
    reasons: list[str]
    escalating_clause_types: list[str]


def score_clause(
    store: PlaybookStore,
    clause_type: str,
    severity: Severity,
    value_tier: ValueTier,
) -> RiskScore:
    pts = float(store.severity_points[severity.value])
    weight = store.criticality(clause_type)
    mult = float(store.value_multipliers[value_tier.value])
    score = round(pts * weight * mult, 3)
    threshold = float(store.thresholds["clause_escalation_score"])
    return RiskScore(
        clause_type=clause_type,
        severity=severity,
        severity_points=pts,
        criticality_weight=weight,
        value_multiplier=mult,
        score=score,
        escalate=score >= threshold,
        formula=(
            f"{pts} (severity {severity.value}) x {weight} (criticality) "
            f"x {mult} (tier {value_tier.value}) = {score}; "
            f"threshold {threshold}"
        ),
    )


def aggregate(
    store: PlaybookStore,
    scores: list[RiskScore],
    verification_failures: list[str] | None = None,
) -> AggregateRisk:
    """Combine clause scores into one escalation decision.

    Three independent escalation triggers, deliberately OR-ed rather than
    blended into a single number:

    1. any single clause at or above the clause threshold -- one catastrophic
       term matters even in an otherwise clean contract;
    2. aggregate at or above the aggregate threshold -- death by a thousand
       MINORs is a real outcome in vendor paper;
    3. any unverified assertion -- if the system cannot prove its own claim
       against the source text, a human decides, not the model.

    Trigger 3 is the one that makes this a safety gate rather than a scoring
    heuristic: it fires on *system uncertainty*, independent of risk level.
    """
    verification_failures = verification_failures or []
    total = round(sum(s.score for s in scores), 3)
    max_clause = max((s.score for s in scores), default=0.0)
    clause_thr = float(store.thresholds["clause_escalation_score"])
    agg_thr = float(store.thresholds["aggregate_escalation_score"])

    reasons: list[str] = []
    escalating = [s.clause_type for s in scores if s.escalate]

    for s in scores:
        if s.escalate:
            reasons.append(
                f"{s.clause_type} scored {s.score} (>= {clause_thr}) -- "
                f"{s.severity.value} deviation on a clause weighted "
                f"{s.criticality_weight}"
            )
    if total >= agg_thr:
        deviations = [s for s in scores if s.severity is not Severity.COMPLIANT]
        reasons.append(
            f"aggregate risk {total} (>= {agg_thr}) across {len(deviations)} deviations"
        )
        # When the aggregate is what fired, the reviewer needs every contributing
        # deviation, not only those that individually crossed the clause
        # threshold. Escalating on aggregate risk and then handing over an empty
        # packet tells a lawyer "something is wrong, find it yourself".
        for s in deviations:
            if s.clause_type not in escalating:
                escalating.append(s.clause_type)
    for ct in verification_failures:
        reasons.append(
            f"{ct}: assessment failed verification -- escalated as unverifiable "
            "rather than reported as fact"
        )
        if ct not in escalating:
            escalating.append(ct)

    return AggregateRisk(
        total=total,
        max_clause=max_clause,
        escalate=bool(reasons),
        reasons=reasons,
        escalating_clause_types=escalating,
    )
