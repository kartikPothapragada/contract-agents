"""Groundedness verification -- the system's hallucination trap.

Every :class:`DeviationAssessment` must carry ``cited_span_text``: the verbatim
contract language the assessment rests on. This module checks that the citation
is real before anything downstream is allowed to treat the assessment as fact.

The check is staged cheapest-first, which is the whole point:

1. **Exact substring** on normalised whitespace. Costs microseconds. Catches the
   dominant failure -- a model paraphrasing the contract into language that
   sounds right and does not exist. No tokens spent.
2. **Fuzzy ratio** via ``difflib``. Distinguishes "the model invented this
   clause" (ratio near 0) from "the model dropped a comma or re-cased a word"
   (ratio above 0.92). Only the first deserves escalation.
3. **LLM entailment**, run by the Verifier agent *only on spans that survived
   step 1 or 2*. Semantic checking is the expensive one, so it never runs on a
   citation already proven fabricated.

The ordering matters for cost: on a contract where the extractor is behaving,
step 1 resolves nearly every span and step 3 runs on a handful.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")
# Models normalise typographic punctuation; the source document may not.
_PUNCT_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})

EXACT_FUZZY_FLOOR = 0.92
"""Above this, treat as a formatting variance rather than a fabrication.

Chosen rather than tuned: at 0.92 a ~40-word quote tolerates roughly three
characters of drift (punctuation, casing, a collapsed line break) but not a
substituted number or a negation. Substituting "shall not" for "shall" or
"$500,000" for "$5,000,000" lands well below it, which is the property that
actually matters here.
"""


def normalize(text: str) -> str:
    return _WS.sub(" ", text.translate(_PUNCT_MAP).strip().lower())


@dataclass
class SpanCheck:
    exact: bool
    fuzzy_ratio: float
    best_window: str | None
    issues: list[str]

    @property
    def grounded(self) -> bool:
        return self.exact or self.fuzzy_ratio >= EXACT_FUZZY_FLOOR


def check_span(citation: str, source: str) -> SpanCheck:
    """Is ``citation`` genuinely present in ``source``?"""
    issues: list[str] = []
    if not citation or not citation.strip():
        return SpanCheck(False, 0.0, None, ["empty citation"])

    cite_n, src_n = normalize(citation), normalize(source)
    if len(cite_n) < 15:
        issues.append(
            "citation shorter than 15 chars -- too short to be evidence of anything"
        )

    if cite_n in src_n:
        return SpanCheck(True, 1.0, citation, issues)

    # Slide a window the length of the citation across the source and keep the
    # closest match. quick_ratio() first as a cheap filter; real_quick_ratio is
    # too coarse and full ratio() on every window is too slow on long contracts.
    best_ratio, best_window = 0.0, None
    step = max(1, len(cite_n) // 4)
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(cite_n)
    for start in range(0, max(1, len(src_n) - len(cite_n) + 1), step):
        window = src_n[start : start + len(cite_n)]
        matcher.set_seq1(window)
        if matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio, best_window = ratio, window

    if best_ratio < EXACT_FUZZY_FLOOR:
        issues.append(
            f"citation not found in source (best window similarity "
            f"{best_ratio:.2f} < {EXACT_FUZZY_FLOOR})"
        )
    return SpanCheck(False, round(best_ratio, 4), best_window, issues)


def check_numeric_consistency(citation: str, claim: str) -> list[str]:
    """Flag numbers asserted in the claim that do not appear in the citation.

    This catches the most damaging quiet failure in contract review: the span is
    real, the reasoning reads well, and the figure is wrong -- a cap reported as
    USD 500,000 when the contract says USD 50,000. The span check passes because
    the quote is genuine; only comparing the numerals catches it.

    Percentages, currency amounts and day-counts are all reduced to bare
    numerals so "$500,000", "500000" and "500,000" compare equal.
    """
    num = re.compile(r"\d[\d,]*(?:\.\d+)?")

    def norm_nums(t: str) -> set[str]:
        out = set()
        for m in num.finditer(t):
            v = m.group(0).replace(",", "").rstrip(".")
            if v:
                out.add(v.rstrip("0").rstrip(".") if "." in v else v)
        return out

    cited, claimed = norm_nums(citation), norm_nums(claim)
    # Small integers (1-12) are usually ordinals or list markers in prose, not
    # contractual figures; flagging them produces noise with no signal.
    orphans = {
        n for n in claimed - cited if not (n.isdigit() and int(n) <= 12)
    }
    if orphans:
        return [
            f"figure(s) {sorted(orphans)} asserted in the assessment do not "
            "appear in the cited span"
        ]
    return []
