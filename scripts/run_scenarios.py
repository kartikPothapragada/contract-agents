"""Produce the two required end-to-end agent interaction traces.

Scenario A -- success path with human override
    A strategic-tier MSA carrying several real deviations. Every clause is
    located, every assessment grounds against the source, the aggregate crosses
    the escalation threshold, counsel reviews and *overrides* one severity
    downward, and the memo is drafted respecting the override.

Scenario B -- edge case: operative terms outside the four corners
    The liability cap is incorporated by reference to "Exhibit B", which is not
    attached. This is the failure mode that matters: there is text under the
    heading, so a naive pipeline extracts it, assesses it, and reports a clean
    liability position for a contract whose cap nobody has read.

    What should happen instead is what the trace shows: extraction declines to
    quote terms it cannot see, the repair budget is spent and exhausted, the
    clause is escalated as *unverifiable* rather than reported as compliant, and
    the human gate receives it with the reason attached.

Both traces are written as JSONL (machine-readable, replayable) and Markdown
(reviewable).

    python scripts/run_scenarios.py
    python scripts/run_scenarios.py --backend anthropic
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clauseguard.orchestration.runner import ReviewRun  # noqa: E402

OUT = ROOT / "results" / "traces"


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _print_gate(packet: dict) -> None:
    print(f"\n  --- HUMAN-IN-THE-LOOP GATE ---")
    print(f"  document        : {packet['doc_id']}")
    print(f"  aggregate risk  : {packet['aggregate_risk']}")
    print(f"  recommendation  : {packet['recommended_action']}")
    print(f"  escalated because:")
    for r in packet["reasons"]:
        print(f"     - {r}")
    print(f"  review packet ({len(packet['items'])} clause(s)):")
    for item in packet["items"]:
        flag = "  [UNVERIFIED]" if item["unverified"] else ""
        print(f"     * {item['clause_type']} [{item['severity']}] "
              f"score={item['risk_score']} owner={item['owner']}{flag}")
        print(f"       observed : {item['observed_position'][:110]}")
        if item.get("verification_issues"):
            for issue in item["verification_issues"][:2]:
                print(f"       issue    : {issue[:110]}")


def scenario_a(backend: str) -> dict:
    _banner("SCENARIO A - success path, counsel overrides one severity")
    text = (ROOT / "data/contracts/synthetic/syn-003.txt").read_text(encoding="utf-8")

    captured: dict = {}

    def reviewer(packet: dict) -> dict:
        captured.update(packet)
        _print_gate(packet)
        # The reviewer overrides whatever this run actually flagged, rather than a
        # clause hard-coded into the demo. A scripted decision that assumes an
        # outcome quietly stops being true the moment a prompt or backend changes,
        # and a demo that lies about its own output is worse than no demo.
        target = next(
            (i for i in packet["items"] if i["severity"] == "MATERIAL"),
            packet["items"][0] if packet["items"] else None,
        )
        decision = {
            "action": "OVERRIDE",
            "reviewer": "j.okafor@northwind.example (Senior Counsel)",
            "note": (
                f"{target['clause_type']} sits below the standard position, but "
                "this vendor has a dedicated-capacity commitment and the "
                "commercial team has accepted the exposure. Downgrading to "
                "MINOR. All other findings stand - do not sign until payment "
                "terms and termination are renegotiated."
            ),
            "severity_overrides": {target["clause_type"]: "MINOR"},
        }
        print(f"\n  reviewer decision: {decision['action']} by {decision['reviewer']}")
        print(
            f"  override        : {target['clause_type']} "
            f"{target['severity']} -> MINOR"
        )
        return decision

    run = ReviewRun(
        "syn-003", text, backend=backend, human_responder=reviewer, trace_dir=OUT
    )
    run.start()
    report = run.report()

    memo = report["memo"]
    print(f"\n  FINAL MEMO")
    print(f"  status          : {report['status']}")
    print(f"  aggregate risk  : {memo['aggregate_risk']}")
    print(f"  human reviewed  : {memo['human_reviewed']} ({memo['reviewer']})")
    print(f"  summary         : {memo['executive_summary'][:300]}")
    print(f"  negotiate in order: {', '.join(memo['negotiation_priority'][:5])}")
    overridden = [d for d in memo["deviations"] if d["human_overridden"]]
    for d in overridden:
        print(f"  override applied: {d['clause_type']} "
              f"{d['model_severity']} -> {d['severity']}")

    (OUT / "scenario_a_success.md").write_text(
        run.bus.render_markdown("Scenario A - success path with human override"),
        encoding="utf-8",
    )
    return report


def scenario_b(backend: str) -> dict:
    _banner("SCENARIO B - edge case, liability cap hidden in an absent exhibit")
    text = (ROOT / "data/contracts/synthetic/syn-007.txt").read_text(encoding="utf-8")
    print("\n  the liability section of this contract reads, in full:")
    start = text.find("Limitation of Liability")
    print("   ", " ".join(text[start : start + 330].split())[:320], "...")

    rejected_once = {"done": False}

    def reviewer(packet: dict) -> dict:
        _print_gate(packet)
        if not rejected_once["done"]:
            rejected_once["done"] = True
            decision = {
                "action": "REJECT",
                "reviewer": "a.lindqvist@northwind.example (Counsel)",
                "note": (
                    "Exhibit B was never provided by the vendor. Re-check the "
                    "liability position against the four corners of the "
                    "document before this comes back to me."
                ),
                "recheck_clause_types": ["LIMITATION_OF_LIABILITY"],
            }
            print(f"\n  reviewer decision: REJECT - sent back for re-extraction")
            print(f"  note            : {decision['note'][:120]}")
            return decision
        decision = {
            "action": "APPROVE",
            "reviewer": "a.lindqvist@northwind.example (Counsel)",
            "note": (
                "Confirmed: the cap genuinely is not in the document. Escalating "
                "to procurement to obtain Exhibit B before signature. Do not "
                "counter-sign."
            ),
        }
        print(f"\n  reviewer decision: APPROVE the escalation as unverifiable")
        return decision

    run = ReviewRun(
        "syn-007", text, backend=backend, human_responder=reviewer, trace_dir=OUT
    )
    run.start()
    report = run.report()

    from clauseguard.orchestration.state import MAX_REPAIRS

    liability = next(
        (d for d in report["memo"]["deviations"]
         if d["clause_type"] == "LIMITATION_OF_LIABILITY"),
        None,
    )
    print(f"\n  FINAL OUTCOME")
    print(f"  status          : {report['status']}")
    print(f"  repair passes   : {report['repair_passes']} of {MAX_REPAIRS} budgeted "
          f"(one triggered by the reviewer's rejection)")
    print(f"  unverified      : {report['unverified'] or 'none'}")
    if liability:
        print(f"  liability cap   : {liability['severity']} - "
              f"{liability['observed_position']}")
    print(f"  summary         : {report['memo']['executive_summary'][:300]}")
    print(
        "\n  The point of this edge case: there IS text under the liability "
        "heading, so a naive pipeline extracts it, finds nothing alarming, and "
        "reports a clean cap for a contract whose cap nobody has read. This "
        "system instead declines to quote terms outside the four corners of the "
        "document, records the cap as absent, and escalates it."
    )

    (OUT / "scenario_b_edge_case.md").write_text(
        run.bus.render_markdown("Scenario B - edge case, unverifiable liability cap"),
        encoding="utf-8",
    )
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="stub", help="stub | anthropic | openai | ollama | auto")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    a = scenario_a(args.backend)
    b = scenario_b(args.backend)

    summary = {
        "backend": args.backend,
        "scenario_a": {
            "doc_id": a["doc_id"], "status": a["status"],
            "escalated": a["escalated"], "aggregate_risk": a["aggregate_risk"],
            "repair_passes": a["repair_passes"], "cost": a["cost"],
        },
        "scenario_b": {
            "doc_id": b["doc_id"], "status": b["status"],
            "escalated": b["escalated"], "unverified": b["unverified"],
            "repair_passes": b["repair_passes"], "cost": b["cost"],
        },
    }
    (ROOT / "results" / "scenarios_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    _banner("ARTEFACTS")
    for p in [
        OUT / "scenario_a_success.md",
        OUT / "scenario_b_edge_case.md",
        ROOT / "results" / "scenarios_summary.json",
    ]:
        print(f"  {p.relative_to(ROOT)}")
    for name, rep in (("A", a), ("B", b)):
        c = rep["cost"]
        print(
            f"  scenario {name}: {c['messages']} messages, {c['llm_calls']} LLM calls, "
            f"${c['total_usd']:.4f}"
        )


if __name__ == "__main__":
    main()
