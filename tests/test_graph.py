"""Orchestration tests.

Run against the stub backend so the "model" is held fixed: anything that fails
here is a routing, budget or gate bug, not model variance. That separation is
the reason the stub backend exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clauseguard.orchestration.runner import ReviewRun
from clauseguard.orchestration.state import MAX_REPAIRS
from clauseguard.schemas.messages import AgentRole, MessageType, Severity

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "contracts" / "synthetic"

pytestmark = pytest.mark.skipif(
    not (CORPUS / "manifest.json").exists(),
    reason="run scripts/generate_contracts.py first",
)


def _approve(packet):
    return {"action": "APPROVE", "reviewer": "test@northwind.example"}


def _run(doc_id: str, responder=_approve, tmp_path=None) -> ReviewRun:
    text = (CORPUS / f"{doc_id}.txt").read_text(encoding="utf-8")
    run = ReviewRun(
        doc_id,
        text,
        backend="stub",
        human_responder=responder,
        trace_dir=tmp_path or (ROOT / "results" / "test_traces"),
    )
    run.start()
    return run


@pytest.fixture(scope="module")
def risky_run():
    return _run("syn-003")


# --------------------------------------------------------------------------- #
# pipeline shape
# --------------------------------------------------------------------------- #
def test_every_agent_participates(risky_run):
    """A 'multi-agent' system where two agents never speak is a pipeline."""
    senders = {m.sender for m in risky_run.bus.messages}
    for role in (
        AgentRole.INTAKE,
        AgentRole.EXTRACTOR,
        AgentRole.POLICY,
        AgentRole.RISK,
        AgentRole.VERIFIER,
        AgentRole.DRAFTER,
        AgentRole.HUMAN,
    ):
        assert role in senders, f"{role.value} emitted nothing"


def test_messages_are_schema_valid_and_correlated(risky_run):
    from clauseguard.schemas.messages import AgentMessage

    ids = {m.msg_id for m in risky_run.bus.messages}
    for m in risky_run.bus.messages:
        AgentMessage.model_validate_json(m.model_dump_json())
        assert m.trace_id == risky_run.trace_id
        if m.parent_msg_id:
            assert m.parent_msg_id in ids, "dangling parent breaks the causal DAG"


def test_external_tool_is_actually_called(risky_run):
    """Track A requires at least one agent using an external tool."""
    tools = {
        m.payload.tool_name
        for m in risky_run.bus.messages
        if m.msg_type is MessageType.TOOL_CALL
    }
    assert "playbook_retrieval" in tools
    assert "section_retrieval" in tools


def test_trace_is_replayable_from_disk(risky_run):
    import json

    from clauseguard.schemas.messages import AgentMessage

    lines = risky_run.bus.sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(risky_run.bus.messages)
    for line in lines:
        AgentMessage.model_validate(json.loads(line))


# --------------------------------------------------------------------------- #
# the human gate
# --------------------------------------------------------------------------- #
def test_risky_contract_reaches_the_human_gate(risky_run):
    assert risky_run.report()["escalated"]
    decisions = [
        m for m in risky_run.bus.messages if m.msg_type is MessageType.HUMAN_DECISION
    ]
    assert decisions, "escalated without ever recording a human decision"


def test_review_packet_is_never_empty_when_escalating(risky_run):
    """Regression: escalating on aggregate risk once produced an empty packet,
    telling a lawyer something was wrong and leaving them to find it."""
    escalation = risky_run.state()["escalation"]
    assert escalation.reasons
    assert escalation.review_packet, "reviewer was escalated an empty packet"
    for item in escalation.review_packet:
        assert item["observed_position"]
        assert item["owner"]


def test_human_override_is_applied_and_recorded_separately():
    """The override must change the memo AND leave the model's own call visible."""

    def override(packet):
        target = packet["items"][0]["clause_type"]
        return {
            "action": "OVERRIDE",
            "reviewer": "counsel@northwind.example",
            "severity_overrides": {target: "MINOR"},
        }

    run = _run("syn-003", responder=override)
    memo = run.state()["memo"]
    changed = [d for d in memo.deviations if d["human_overridden"]]
    assert changed, "override was accepted but never reached the memo"
    row = changed[0]
    assert row["severity"] == "MINOR"
    assert row["model_severity"] != "MINOR", "model's original call was overwritten"
    assert memo.human_reviewed and memo.reviewer


def test_human_rejection_sends_work_back_for_re_extraction():
    calls = {"n": 0}

    def reject_then_approve(packet):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "action": "REJECT",
                "reviewer": "counsel@northwind.example",
                "recheck_clause_types": ["LIMITATION_OF_LIABILITY"],
            }
        return _approve(packet)

    run = _run("syn-007", responder=reject_then_approve)
    assert calls["n"] >= 2, "rejection did not route back through the pipeline"
    assert run.state()["repair_count"] >= 1


def test_rejection_consumes_exactly_one_repair_pass():
    """Regression: the gate and the repair node both incremented the budget, so
    a single rejection spent both available passes."""
    calls = {"n": 0}

    def reject_once(packet):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"action": "REJECT", "reviewer": "counsel@northwind.example"}
        return _approve(packet)

    run = _run("syn-007", responder=reject_once)
    assert run.state()["repair_count"] == 1


# --------------------------------------------------------------------------- #
# safety properties
# --------------------------------------------------------------------------- #
def test_repair_budget_is_bounded():
    """A rejecting reviewer must not be able to spin the graph forever."""
    calls = {"n": 0}

    def always_reject(packet):
        calls["n"] += 1
        return {"action": "REJECT", "reviewer": "counsel@northwind.example"}

    run = _run("syn-007", responder=always_reject)
    assert run.state()["repair_count"] <= MAX_REPAIRS
    assert calls["n"] <= MAX_REPAIRS + 1
    assert run.state()["memo"] is not None, "graph did not terminate"


def test_no_assessment_survives_with_an_ungrounded_citation(risky_run):
    """The core safety property: nothing reaches the memo asserting a quote that
    is not in the contract."""
    source = risky_run.state()["contract_text"]
    from clauseguard.tools.verification import check_span

    for row in risky_run.state()["memo"].deviations:
        cited = row.get("cited_span")
        if not cited:
            continue
        assert check_span(cited, source).grounded, (
            f"{row['clause_type']} reached the memo citing text absent from the source"
        )


def test_absent_required_clause_is_reported_not_skipped():
    """syn-007 buries the liability cap in an exhibit that is not attached."""
    run = _run("syn-007")
    finding = next(
        f for f in run.state()["findings"] if f.clause_type == "LIMITATION_OF_LIABILITY"
    )
    assert not finding.found
    assessment = next(
        a for a in run.state()["assessments"]
        if a.clause_type == "LIMITATION_OF_LIABILITY"
    )
    assert assessment.severity in {Severity.MATERIAL, Severity.UNACCEPTABLE}


def test_extractor_never_reports_guessed_offsets(risky_run):
    """Offsets are computed from the source, never taken from the model."""
    source = risky_run.state()["contract_text"]
    for finding in risky_run.state()["findings"]:
        if finding.found and finding.span:
            span = finding.span
            assert source[span.char_start : span.char_end] == span.text


def test_cost_report_accounts_for_every_llm_call(risky_run):
    report = risky_run.bus.cost_report()
    assert report["messages"] == len(risky_run.bus.messages)
    assert report["llm_calls"] >= 1
    assert set(report["by_agent"]) <= {r.value for r in AgentRole}
