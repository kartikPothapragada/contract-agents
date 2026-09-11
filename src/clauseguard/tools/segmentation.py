"""Deterministic contract segmentation.

Splitting a contract into numbered sections is a parsing problem, not a
reasoning problem. Doing it with a regex rather than an LLM call buys three
things that matter more than flexibility here:

* **Exact character offsets.** Every downstream span claim is checked against
  the source by offset. An LLM that "returns" a section boundary cannot be
  trusted to preserve offsets, and without offsets the verifier has nothing to
  verify against.
* **Zero tokens.** A 40-page MSA would cost more to segment by LLM than to
  analyse.
* **Determinism.** The same contract segments identically on every run, so a
  regression in a downstream agent cannot be blamed on segmentation drift.

The trade-off, stated honestly: this handles the numbered/headed structure that
covers most commercial contracts and degrades on scanned PDFs, tables, and
exhibit-only documents. Those cases fall through to the paragraph fallback and
are flagged with low confidence rather than silently mis-segmented.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Matches: "1.", "1.2", "12.3.4", "ARTICLE IV", "Section 7", optionally followed
# by a title on the same line. Anchored at line start to avoid firing on
# in-sentence cross-references like "as set out in Section 7.2".
_HEADING = re.compile(
    r"^[ \t]*(?:"
    r"(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?"
    r"|(?:ARTICLE|Article)\s+(?P<art>[IVXLC]+|\d{1,2})"
    r"|(?:SECTION|Section)\s+(?P<sec>\d{1,2}(?:\.\d{1,2})*)"
    r")"
    r"[ \t]+(?P<title>[A-Z][^\n]{0,90})$",
    re.MULTILINE,
)


@dataclass
class Section:
    ref: str
    heading: str
    text: str
    char_start: int
    char_end: int

    def snippet(self, limit: int = 400) -> str:
        t = " ".join(self.text.split())
        return t if len(t) <= limit else t[:limit] + " ..."


@dataclass
class SegmentationResult:
    sections: list[Section]
    method: str
    confidence: float

    def as_tool_result(self) -> dict:
        return {
            "method": self.method,
            "confidence": self.confidence,
            "section_count": len(self.sections),
            "sections": [
                {"ref": s.ref, "heading": s.heading, "chars": [s.char_start, s.char_end]}
                for s in self.sections
            ],
        }


def segment_contract(text: str, min_sections: int = 3) -> SegmentationResult:
    """Segment into numbered sections, falling back to paragraph blocks."""
    matches = list(_HEADING.finditer(text))

    if len(matches) >= min_sections:
        sections: list[Section] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            ref = m.group("num") or m.group("art") or m.group("sec") or str(i + 1)
            sections.append(
                Section(
                    ref=str(ref).rstrip("."),
                    heading=(m.group("title") or "").strip(),
                    text=text[start:end].strip(),
                    char_start=start,
                    char_end=end,
                )
            )
        # Confidence tracks how much of the document the headings actually
        # cover. A document where headings capture only the first page is
        # probably an exhibit-heavy contract, and should say so.
        covered = sum(s.char_end - s.char_start for s in sections)
        confidence = min(1.0, 0.55 + 0.45 * (covered / max(len(text), 1)))
        return SegmentationResult(sections, "numbered-heading", round(confidence, 3))

    # Fallback: blank-line separated blocks.
    sections = []
    cursor = 0
    for i, block in enumerate(re.split(r"\n\s*\n", text)):
        start = text.find(block, cursor)
        if start == -1 or not block.strip():
            continue
        end = start + len(block)
        cursor = end
        first_line = block.strip().splitlines()[0][:80]
        sections.append(
            Section(str(i + 1), first_line, block.strip(), start, end)
        )
    return SegmentationResult(sections, "paragraph-fallback", 0.4)


_INDEX_CACHE: dict[str, object] = {}


def build_section_index(sections: list[Section]):
    """Index sections for retrieval so the Extractor can find a clause by meaning.

    Cached on the section content. The extractor is re-entered on every repair
    pass, and re-embedding an unchanged document each time was the single
    largest avoidable latency cost in the pipeline -- on a 16-section contract a
    rebuild is ~1.5 s of embedding work that produces a byte-identical index.
    The repair loop only changes *which* clauses are re-examined, never the
    document, so the index is safe to reuse.

    Keyed on a hash of the section text rather than the document id: two runs of
    the same contract share the index, and a genuinely edited document misses.
    """
    from clauseguard.tools.retrieval import Document, HybridRetriever

    key = hashlib.sha256(
        "\n".join(f"{s.ref}|{s.char_start}|{s.text}" for s in sections).encode()
    ).hexdigest()
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    index = HybridRetriever(
        [
            Document(
                doc_id=s.ref,
                text=f"{s.heading}. {s.text}",
                metadata={"char_start": s.char_start, "char_end": s.char_end},
            )
            for s in sections
        ]
    )
    # Bounded: a long-running worker reviewing thousands of contracts must not
    # accumulate an embedding matrix per document.
    if len(_INDEX_CACHE) > 32:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[key] = index
    return index
