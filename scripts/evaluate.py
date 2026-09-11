"""End-to-end evaluation against the labelled corpus.

Metric choices, and why these rather than a single accuracy number
------------------------------------------------------------------
Contract triage has a deeply asymmetric loss function. Telling a lawyer to look
at a clause that turns out to be fine costs ten minutes. Telling them a clause
is fine when it is not can cost the liability cap. So the headline number here
is not accuracy, it is the **dangerous-miss rate**: gold MATERIAL/UNACCEPTABLE
that the system reported as COMPLIANT/MINOR. A system can be 90% accurate and
useless if its 10% sits entirely in that cell.

Reported:

* ``severity_accuracy``       exact match, for reference only
* ``dangerous_miss_rate``     real risk graded as safe -- the metric that matters
* ``false_alarm_rate``        safe graded as risky -- the cost of trusting it
* ``adjacent_rate``           off by one severity band; a triage system that is
                              never worse than adjacent is still usable
* ``escalation_recall``       of contracts that *should* reach a human, how many did
* ``escalation_precision``    of contracts that reached a human, how many needed to
* ``absence_detection``       clauses deliberately omitted, correctly reported absent
* ``groundedness_rate``       assessments whose citation verifies against the source
* cost and latency per contract

Gold escalation is computed by running the *same deterministic scorer* over the
gold severities. That keeps the comparison honest: the system is not being
graded against a threshold different from the one it was given.

    python scripts/evaluate.py --backend stub
    python scripts/evaluate.py --backend anthropic --limit 8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clauseguard.orchestration.runner import ReviewRun  # noqa: E402
from clauseguard.schemas.messages import Severity, ValueTier  # noqa: E402
from clauseguard.tools.playbook import load_playbook  # noqa: E402
from clauseguard.tools.scoring import aggregate, score_clause  # noqa: E402

RISKY = {Severity.MATERIAL, Severity.UNACCEPTABLE}
SAFE = {Severity.COMPLIANT, Severity.MINOR}
# Gold labels that are not severities: they assert a structural fact about the
# document instead, and are scored separately.
STRUCTURAL = {"ABSENT", "UNVERIFIABLE"}


def gold_should_escalate(meta: dict) -> bool:
    """Run the production scorer over the gold labels."""
    pb = load_playbook()
    tier = ValueTier(meta["value_tier"])
    scores = []
    for clause_type, label in meta["gold_severity"].items():
        if label in STRUCTURAL:
            # A missing or unreadable required clause is, by policy, a thing a
            # human must see. Encoded as UNACCEPTABLE so the gold side uses the
            # same arithmetic as the system side.
            label = "UNACCEPTABLE"
        scores.append(score_clause(pb, clause_type, Severity(label), tier))
    return aggregate(pb, scores).escalate


def evaluate(backend: str, limit: int | None, corpus: str) -> dict:
    manifest = json.loads((ROOT / corpus / "manifest.json").read_text(encoding="utf-8"))
    if limit:
        manifest = manifest[:limit]

    confusion: Counter = Counter()
    dangerous: list[dict] = []
    false_alarms: list[dict] = []
    absence_total = absence_found = 0
    grounded_total = grounded_ok = 0
    esc_tp = esc_fp = esc_fn = esc_tn = 0
    per_doc: list[dict] = []
    total_cost = 0.0
    total_seconds = 0.0

    for meta in manifest:
        doc_id = meta["doc_id"]
        text = (ROOT / corpus / f"{doc_id}.txt").read_text(encoding="utf-8")

        started = time.perf_counter()
        run = ReviewRun(
            doc_id,
            text,
            backend=backend,
            value_tier=meta["value_tier"],
            human_responder=lambda packet: {
                "action": "APPROVE",
                "reviewer": "eval-harness (auto-approve)",
            },
            trace_dir=ROOT / "results" / "eval_traces",
        )
        run.start()
        elapsed = time.perf_counter() - started
        state = run.state()
        report = run.report()

        predicted = {a.clause_type: a.severity for a in state.get("assessments", [])}
        findings = {f.clause_type: f for f in state.get("findings", [])}
        verdicts = {v.target_clause_type: v for v in state.get("verdicts", [])}

        doc_dangerous = 0
        for clause_type, gold_label in meta["gold_severity"].items():
            if gold_label in STRUCTURAL:
                # Scored as a detection problem, not a grading problem.
                absence_total += 1
                finding = findings.get(clause_type)
                if finding is not None and not finding.found:
                    absence_found += 1
                continue

            gold = Severity(gold_label)
            pred = predicted.get(clause_type)
            if pred is None:
                # Never assessed at all. Counted as the worst case for a risky
                # clause -- silence about a dangerous term is a dangerous miss,
                # not a missing data point to be dropped from the denominator.
                confusion[(gold.value, "NOT_ASSESSED")] += 1
                if gold in RISKY:
                    dangerous.append(
                        {"doc": doc_id, "clause": clause_type,
                         "gold": gold.value, "pred": "NOT_ASSESSED"}
                    )
                    doc_dangerous += 1
                continue

            confusion[(gold.value, pred.value)] += 1
            if gold in RISKY and pred in SAFE:
                dangerous.append(
                    {"doc": doc_id, "clause": clause_type,
                     "gold": gold.value, "pred": pred.value}
                )
                doc_dangerous += 1
            elif gold is Severity.COMPLIANT and pred in RISKY:
                false_alarms.append(
                    {"doc": doc_id, "clause": clause_type,
                     "gold": gold.value, "pred": pred.value}
                )

        for clause_type, verdict in verdicts.items():
            if "absence finding" in " ".join(verdict.issues):
                continue
            grounded_total += 1
            grounded_ok += bool(verdict.span_exact_match or verdict.span_fuzzy_ratio >= 0.92)

        want = gold_should_escalate(meta)
        got = report["escalated"]
        if want and got:
            esc_tp += 1
        elif want and not got:
            esc_fn += 1
        elif not want and got:
            esc_fp += 1
        else:
            esc_tn += 1

        total_cost += report["cost"]["total_usd"]
        total_seconds += elapsed
        per_doc.append(
            {
                "doc_id": doc_id,
                "value_tier": meta["value_tier"],
                "escalated": got,
                "should_escalate": want,
                "dangerous_misses": doc_dangerous,
                "repair_passes": report["repair_passes"],
                "aggregate_risk": report["aggregate_risk"],
                "llm_calls": report["cost"]["llm_calls"],
                "usd": report["cost"]["total_usd"],
                "seconds": round(elapsed, 2),
            }
        )
        print(
            f"  {doc_id}  escalated={str(got):<5} (gold {str(want):<5})  "
            f"misses={doc_dangerous}  {elapsed:5.1f}s  ${report['cost']['total_usd']:.4f}"
        )

    graded = sum(confusion.values())
    exact = sum(v for (g, p), v in confusion.items() if g == p)
    order = ["COMPLIANT", "MINOR", "MATERIAL", "UNACCEPTABLE"]
    adjacent = sum(
        v for (g, p), v in confusion.items()
        if p in order and g in order and abs(order.index(g) - order.index(p)) == 1
    )
    risky_total = sum(v for (g, _), v in confusion.items() if g in {"MATERIAL", "UNACCEPTABLE"})
    compliant_total = sum(v for (g, _), v in confusion.items() if g == "COMPLIANT")

    return {
        "backend": backend,
        "corpus": corpus,
        "documents": len(manifest),
        "clause_judgements": graded,
        "severity_accuracy": round(exact / graded, 4) if graded else None,
        "adjacent_rate": round(adjacent / graded, 4) if graded else None,
        "dangerous_miss_rate": round(len(dangerous) / risky_total, 4) if risky_total else None,
        "dangerous_misses": len(dangerous),
        "risky_clauses_total": risky_total,
        "false_alarm_rate": round(len(false_alarms) / compliant_total, 4) if compliant_total else None,
        "false_alarms": len(false_alarms),
        "compliant_clauses_total": compliant_total,
        "absence_detection_recall": round(absence_found / absence_total, 4) if absence_total else None,
        "absence_cases": absence_total,
        "groundedness_rate": round(grounded_ok / grounded_total, 4) if grounded_total else None,
        "grounded_checked": grounded_total,
        "escalation": {
            "recall": round(esc_tp / (esc_tp + esc_fn), 4) if (esc_tp + esc_fn) else None,
            "precision": round(esc_tp / (esc_tp + esc_fp), 4) if (esc_tp + esc_fp) else None,
            "tp": esc_tp, "fp": esc_fp, "fn": esc_fn, "tn": esc_tn,
        },
        "cost": {
            "total_usd": round(total_cost, 4),
            "usd_per_contract": round(total_cost / len(manifest), 5) if manifest else None,
            "seconds_per_contract": round(total_seconds / len(manifest), 2) if manifest else None,
        },
        "confusion": {f"{g}->{p}": v for (g, p), v in sorted(confusion.items())},
        "dangerous_miss_detail": dangerous,
        "false_alarm_detail": false_alarms[:20],
        "per_document": per_doc,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="stub")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--corpus", default="data/contracts/synthetic")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    label = "adversarial" if "adversarial" in args.corpus else "synthetic"
    print(f"evaluating {label} corpus with backend={args.backend}\n")
    res = evaluate(args.backend, args.limit, args.corpus)

    out = ROOT / (args.out or f"results/evaluation_{label}_{args.backend}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2), encoding="utf-8")

    print(f"\n{'=' * 62}")
    print(f"RESULTS  corpus={label}  backend={res['backend']}  "
          f"docs={res['documents']}  clause judgements={res['clause_judgements']}")
    print("=" * 62)
    print(f"  severity accuracy (exact)     {res['severity_accuracy']}")
    print(f"  adjacent-band rate            {res['adjacent_rate']}")
    print(f"  DANGEROUS MISS RATE           {res['dangerous_miss_rate']}   "
          f"({res['dangerous_misses']}/{res['risky_clauses_total']} risky clauses graded safe)")
    print(f"  false alarm rate              {res['false_alarm_rate']}   "
          f"({res['false_alarms']}/{res['compliant_clauses_total']} compliant clauses graded risky)")
    print(f"  absence detection recall      {res['absence_detection_recall']}   "
          f"({res['absence_cases']} cases)")
    print(f"  groundedness rate             {res['groundedness_rate']}   "
          f"({res['grounded_checked']} citations checked)")
    e = res["escalation"]
    print(f"  escalation recall / precision {e['recall']} / {e['precision']}   "
          f"(tp={e['tp']} fp={e['fp']} fn={e['fn']} tn={e['tn']})")
    c = res["cost"]
    print(f"  cost per contract             ${c['usd_per_contract']}  "
          f"({c['seconds_per_contract']}s)")

    if res["dangerous_miss_detail"]:
        print(f"\n  dangerous misses (the ones that matter):")
        for m in res["dangerous_miss_detail"][:12]:
            print(f"    {m['doc']:<9} {m['clause']:<28} gold {m['gold']:<13} -> {m['pred']}")

    print(f"\nwritten to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
