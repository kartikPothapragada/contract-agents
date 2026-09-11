"""Retrieval ablation: BM25 vs dense vs RRF hybrid.

Answers the question the hybrid design owes evidence for -- does fusing actually
beat either leg, and where? Results are broken out by query kind, because an
aggregate number would hide the interesting part: the two retrievers fail on
disjoint query types, which is the entire argument for fusing them.

    python scripts/eval_retrieval.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clauseguard.tools.playbook import load_playbook  # noqa: E402

MODES = ("bm25", "dense", "hybrid")


def evaluate() -> dict:
    pb = load_playbook()
    spec = yaml.safe_load((ROOT / "eval" / "retrieval_queries.yaml").read_text(encoding="utf-8"))
    queries = spec["queries"]

    per_mode: dict[str, dict] = {}
    per_kind: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"n": 0, "r1": 0, "r3": 0}))
    failures: dict[str, list] = defaultdict(list)

    for mode in MODES:
        r1 = r3 = 0
        rr_sum = 0.0
        for item in queries:
            expected = item["expected"]
            acceptable = set(item.get("acceptable", [])) | {expected}
            kind = item.get("kind", "mixed")

            hits = pb.retrieve(item["q"], k=3, mode=mode)
            ranked = [rule.rule_id for rule, _ in hits]

            hit1 = bool(ranked) and ranked[0] == expected
            hit3 = any(r in acceptable for r in ranked[:3])
            r1 += hit1
            r3 += hit3
            # MRR over the strict expected rule only: crediting an "acceptable"
            # alternative here would make the metric agree with itself.
            rank = next((i + 1 for i, r in enumerate(ranked) if r == expected), None)
            rr_sum += 1.0 / rank if rank else 0.0

            bucket = per_kind[kind][mode]
            bucket["n"] += 1
            bucket["r1"] += hit1
            bucket["r3"] += hit3
            if not hit1:
                failures[mode].append(
                    {"q": item["q"], "kind": kind, "expected": expected, "got": ranked}
                )

        n = len(queries)
        per_mode[mode] = {
            "n": n,
            "recall@1": round(r1 / n, 4),
            "recall@3": round(r3 / n, 4),
            "mrr@3": round(rr_sum / n, 4),
        }

    return {
        "dense_backend": pb._retriever.dense.backend,
        "playbook_version": pb.version,
        "rules_indexed": len(pb.rules),
        "overall": per_mode,
        "by_kind": {k: dict(v) for k, v in per_kind.items()},
        "hybrid_failures": failures["hybrid"],
    }


def main() -> None:
    res = evaluate()
    out = ROOT / "results" / "retrieval_ablation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2), encoding="utf-8")

    print(f"playbook v{res['playbook_version']}  |  {res['rules_indexed']} rules  |  "
          f"dense backend: {res['dense_backend']}")
    print(f"\n{'mode':<10}{'recall@1':>10}{'recall@3':>10}{'MRR@3':>10}")
    print("-" * 40)
    for mode, m in res["overall"].items():
        print(f"{mode:<10}{m['recall@1']:>10.3f}{m['recall@3']:>10.3f}{m['mrr@3']:>10.3f}")

    print(f"\nrecall@1 by query kind (n = queries of that kind)")
    print(f"{'kind':<14}{'n':>4}{'bm25':>10}{'dense':>10}{'hybrid':>10}")
    print("-" * 48)
    for kind in sorted(res["by_kind"]):
        row = res["by_kind"][kind]
        n = row["hybrid"]["n"]
        cells = "".join(f"{row[m]['r1'] / max(row[m]['n'], 1):>10.3f}" for m in MODES)
        print(f"{kind:<14}{n:>4}{cells}")

    if res["hybrid_failures"]:
        print(f"\nhybrid recall@1 misses ({len(res['hybrid_failures'])}):")
        for f in res["hybrid_failures"]:
            print(f"  [{f['kind']}] {f['q'][:66]}")
            print(f"      expected {f['expected']}, ranked {f['got']}")

    print(f"\nwritten to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
