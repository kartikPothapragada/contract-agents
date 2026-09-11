"""Tests for the deterministic tool layer.

These are the parts of the system that must behave identically on every run, so
they are the parts worth testing hardest. Anything an LLM decides is tested at
the orchestration level instead (``test_graph.py``), where the stub backend
holds the model output fixed.
"""

from __future__ import annotations

import pytest

from clauseguard.schemas.messages import Severity, ValueTier
from clauseguard.tools.playbook import load_playbook
from clauseguard.tools.retrieval import BM25, Document, reciprocal_rank_fusion, tokenize
from clauseguard.tools.scoring import aggregate, score_clause
from clauseguard.tools.segmentation import segment_contract
from clauseguard.tools.verification import check_numeric_consistency, check_span


@pytest.fixture(scope="module")
def pb():
    return load_playbook()


# --------------------------------------------------------------------------- #
# retrieval
# --------------------------------------------------------------------------- #
def test_tokenize_preserves_numerals():
    """Numerals carry the discriminative signal in contract policy text."""
    toks = tokenize("Payment is due net forty-five (45) days at 99.5% uptime")
    assert "45" in toks
    assert "99.5" in toks
    assert "is" not in toks  # stopword


def test_bm25_ranks_lexical_match_first():
    docs = [
        Document("a", "breach notification within seventy-two 72 hours of awareness"),
        Document("b", "payment is due net forty-five 45 days from invoice"),
        Document("c", "governing law is the State of Delaware"),
    ]
    hits = BM25(docs).search("72 hours breach notification", k=3)
    assert hits[0].doc_id == "a"


def test_rrf_prefers_agreement_over_one_confident_run():
    """A doc ranked 2nd by both retrievers should beat one ranked 1st by one."""
    from clauseguard.tools.retrieval import Hit

    run_a = [Hit("x", 9.9, 1, "bm25"), Hit("y", 1.0, 2, "bm25")]
    run_b = [Hit("z", 9.9, 1, "dense"), Hit("y", 1.0, 2, "dense")]
    fused = reciprocal_rank_fusion([run_a, run_b], k=3)
    assert fused[0].doc_id == "y"


# --------------------------------------------------------------------------- #
# segmentation
# --------------------------------------------------------------------------- #
def test_segmentation_offsets_are_exact():
    text = (
        "MASTER SERVICES AGREEMENT\n\n"
        "1. Definitions\n\nCapitalised terms have the meanings given.\n\n"
        "2. Limitation of Liability\n\nLiability is capped at fees paid.\n\n"
        "3. Governing Law\n\nDelaware law applies.\n"
    )
    result = segment_contract(text)
    assert result.method == "numbered-heading"
    assert len(result.sections) == 3
    for section in result.sections:
        # The offset must address the real document, not a normalised copy.
        assert text[section.char_start : section.char_end].strip() == section.text


def test_segmentation_ignores_inline_cross_references():
    """'as set out in Section 7.2' mid-sentence must not open a new section."""
    text = (
        "1. Scope\n\nServices are provided as set out in Section 7.2 below.\n\n"
        "2. Fees\n\nFees are payable net 45.\n\n"
        "3. Term\n\nOne year.\n"
    )
    assert len(segment_contract(text).sections) == 3


def test_segmentation_falls_back_with_low_confidence():
    text = "Some prose.\n\nMore prose without any numbering at all.\n\nAnd more.\n"
    result = segment_contract(text)
    assert result.method == "paragraph-fallback"
    assert result.confidence < 0.5


# --------------------------------------------------------------------------- #
# verification -- the hallucination trap
# --------------------------------------------------------------------------- #
SOURCE = (
    "8.2 Limitation of Liability. In no event shall Vendor's aggregate "
    "liability exceed the fees paid in the three (3) months preceding the claim."
)


def test_check_span_accepts_verbatim_quote():
    assert check_span("aggregate liability exceed the fees paid in the three", SOURCE).grounded


def test_check_span_tolerates_whitespace_and_case():
    assert check_span("AGGREGATE  LIABILITY   EXCEED  THE FEES PAID", SOURCE).grounded


def test_check_span_rejects_fabricated_quote():
    check = check_span(
        "Vendor shall maintain cyber insurance of USD 5,000,000 at all times", SOURCE
    )
    assert not check.grounded
    assert check.fuzzy_ratio < 0.92


def test_check_span_rejects_substituted_number():
    """A swapped figure must fall below the fuzzy floor, not squeak past it."""
    check = check_span(
        "aggregate liability exceed the fees paid in the thirty (30) months preceding",
        SOURCE,
    )
    assert not check.grounded


def test_check_span_rejects_empty_citation():
    assert not check_span("", SOURCE).grounded


def test_numeric_consistency_flags_invented_figure():
    issues = check_numeric_consistency(
        "fees paid in the three (3) months preceding",
        "The cap is limited to about USD 500,000",
    )
    assert issues and "500000" in issues[0]


def test_numeric_consistency_allows_figures_from_the_rule():
    """The regression this encodes: comparing only against the quote flagged
    every correctly reasoned deviation, because a rationale legitimately names
    the playbook's figure as well as the contract's."""
    issues = check_numeric_consistency(
        "payable within thirty (30) days of invoice",
        "net 30 is shorter than the standard net 45",
        reference_texts=["Payment is due net forty-five (45) days from receipt"],
    )
    assert issues == []


# --------------------------------------------------------------------------- #
# scoring -- the escalation gate
# --------------------------------------------------------------------------- #
def test_score_is_reproducible_and_shows_its_working(pb):
    a = score_clause(pb, "LIMITATION_OF_LIABILITY", Severity.UNACCEPTABLE, ValueTier.MID)
    b = score_clause(pb, "LIMITATION_OF_LIABILITY", Severity.UNACCEPTABLE, ValueTier.MID)
    assert a.score == b.score == 5.0
    assert "threshold" in a.formula


def test_value_tier_scales_risk(pb):
    small = score_clause(pb, "DATA_PROTECTION", Severity.MATERIAL, ValueTier.SMALL)
    strategic = score_clause(pb, "DATA_PROTECTION", Severity.MATERIAL, ValueTier.STRATEGIC)
    assert strategic.score > small.score


def test_compliant_clause_never_escalates(pb):
    score = score_clause(pb, "LIMITATION_OF_LIABILITY", Severity.COMPLIANT, ValueTier.STRATEGIC)
    assert score.score == 0.0
    assert not score.escalate


def test_unknown_clause_type_defaults_to_mid_criticality(pb):
    """Scoring an unrecognised clause as harmless is the wrong failure direction."""
    assert pb.criticality("SOMETHING_NOT_IN_THE_PLAYBOOK") == 0.5


def test_low_criticality_deviations_alone_do_not_escalate(pb):
    """A deliberate property, not an oversight.

    Four MATERIAL deviations on low-weight clauses (payment, renewal, audit,
    forum) total 7.56 at strategic tier -- below the aggregate threshold of 14.
    That is the intended behaviour: pulling counsel in for four administrative
    quibbles is how a gate gets ignored. The weights encode which clauses are
    worth someone's afternoon.
    """
    scores = [
        score_clause(pb, ct, Severity.MATERIAL, ValueTier.STRATEGIC)
        for ct in ("PAYMENT_TERMS", "AUTO_RENEWAL", "AUDIT_RIGHTS", "GOVERNING_LAW")
    ]
    assert not aggregate(pb, scores).escalate


def test_aggregate_escalates_on_accumulated_deviations(pb):
    """No single clause crosses the per-clause threshold of 6.0, but together
    they cross the aggregate threshold -- death by a thousand MATERIALs."""
    scores = [
        score_clause(pb, ct, Severity.MATERIAL, ValueTier.STRATEGIC)
        for ct in ("PAYMENT_TERMS", "AUTO_RENEWAL", "AUDIT_RIGHTS",
                   "GOVERNING_LAW", "SLA", "ASSIGNMENT", "CONFIDENTIALITY")
    ]
    assert all(not s.escalate for s in scores), "a clause crossed on its own"
    agg = aggregate(pb, scores)
    assert agg.escalate
    # and the reviewer is handed every contributing clause, not an empty packet
    assert set(agg.escalating_clause_types) >= {s.clause_type for s in scores}


def test_verification_failure_escalates_regardless_of_risk_level(pb):
    """System uncertainty is its own escalation trigger, independent of severity."""
    scores = [score_clause(pb, "PAYMENT_TERMS", Severity.COMPLIANT, ValueTier.SMALL)]
    agg = aggregate(pb, scores, verification_failures=["PAYMENT_TERMS"])
    assert agg.escalate
    assert "PAYMENT_TERMS" in agg.escalating_clause_types


def test_clean_contract_does_not_escalate(pb):
    scores = [
        score_clause(pb, ct, Severity.COMPLIANT, ValueTier.MID)
        for ct in ("LIMITATION_OF_LIABILITY", "DATA_PROTECTION", "PAYMENT_TERMS")
    ]
    assert not aggregate(pb, scores).escalate
