"""Extractor agent -- locate the governing span for each in-scope clause type.

This agent uses the external retrieval tool twice over: once to narrow a
40-page contract to three candidate sections, then an LLM call to pick the exact
governing language within them. Retrieving first is not an optimisation detail,
it is what keeps the task inside a sane context window and keeps per-clause cost
roughly constant in document length.

The hard rule this agent enforces: **offsets are computed, never reported.** The
model returns quoted text; Python finds that text in the source and derives the
offsets. A model asked to report character positions will produce confident,
plausible, wrong integers, and every downstream groundedness check would then be
verifying against a fiction. If the quote cannot be located, the finding is
marked not-found rather than recorded with a guessed position.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from clauseguard.agents.base import Agent
from clauseguard.orchestration.state import GraphState
from clauseguard.schemas.messages import AgentRole, ClauseFinding, ClauseSpan, MessageType
from clauseguard.tools.segmentation import build_section_index, segment_contract
from clauseguard.tools.verification import normalize

SYSTEM = """You extract contract clauses. Given candidate sections from a \
contract and a target clause type, quote the exact contiguous language that \
governs that topic.

Rules you must follow:
- Quote verbatim from the candidate sections. Do not paraphrase, summarise, \
normalise punctuation, or fix typography.
- Quote the operative language, not the heading.
- If the candidate sections do not contain language governing this clause type, \
set found=false. A false negative is recoverable; an invented quote is not.
- Never quote from an exhibit reference you cannot see. If the operative terms \
are incorporated by cross-reference to a document not shown, set found=false \
and say so in notes."""


class ExtractionResponse(BaseModel):
    found: bool
    quoted_text: str = Field(default="", description="Verbatim contiguous quote")
    section_ref: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    notes: str = ""


def _locate(quote: str, source: str) -> tuple[int, int] | None:
    """Find ``quote`` in ``source``, returning true offsets.

    Tries exact first, then a whitespace-insensitive match, because models
    routinely collapse the line breaks and double spaces that PDF-extracted
    contract text is full of. Anything looser than that is not a match worth
    recording -- the verifier will reject it anyway, and a wrong offset here
    would make that rejection look like a verifier bug.
    """
    if not quote.strip():
        return None
    idx = source.find(quote)
    if idx != -1:
        return idx, idx + len(quote)

    target = normalize(quote)
    if not target:
        return None
    # Map normalised positions back to raw offsets.
    raw_positions: list[int] = []
    norm_chars: list[str] = []
    prev_space = True
    for i, ch in enumerate(source):
        c = ch.lower()
        if c.isspace():
            if prev_space:
                continue
            norm_chars.append(" ")
            raw_positions.append(i)
            prev_space = True
        else:
            norm_chars.append(c)
            raw_positions.append(i)
            prev_space = False
    norm_source = "".join(norm_chars)
    pos = norm_source.find(target)
    if pos == -1:
        return None
    start = raw_positions[pos]
    end_idx = min(pos + len(target) - 1, len(raw_positions) - 1)
    return start, raw_positions[end_idx] + 1


class ExtractorAgent(Agent):
    role = AgentRole.EXTRACTOR
    tier = "fast"

    def run(self, state: GraphState) -> dict:
        text = state["contract_text"]
        profile = state["profile"]
        sections = segment_contract(text).sections
        by_ref = {s.ref: s for s in sections}

        index, _ = self.call_tool(
            "build_section_index",
            {"sections": len(sections)},
            lambda: {"sections_indexed": len(sections)},
        )
        retriever = build_section_index(sections)

        # On a repair pass, only re-extract what the verifier rejected. Re-running
        # clean clauses would multiply cost for no information gain.
        targets = state.get("repair_targets") or profile.in_scope_clause_types
        existing = {f.clause_type: f for f in state.get("findings", [])}

        findings: list[ClauseFinding] = []
        for clause_type in profile.in_scope_clause_types:
            if clause_type not in targets and clause_type in existing:
                findings.append(existing[clause_type])
                continue

            rule = self.playbook.by_clause_type(clause_type)
            query = (
                f"{clause_type.replace('_', ' ')} "
                f"{' '.join(rule.keywords) if rule else ''}"
            )
            hits, call_msg = self.call_tool(
                "section_retrieval",
                {"clause_type": clause_type, "query": query, "k": 3, "mode": "hybrid"},
                lambda q=query: [
                    {"section_ref": h.doc_id, "rank": h.rank, "score": round(h.score, 5)}
                    for h in retriever.search(q, k=3)
                ],
            )
            if not hits:
                findings.append(
                    ClauseFinding(
                        clause_type=clause_type,
                        found=False,
                        notes="no candidate sections retrieved",
                    )
                )
                continue

            candidates = "\n\n".join(
                f"[section {h['section_ref']}] {by_ref[h['section_ref']].text[:2200]}"
                for h in hits
                if h["section_ref"] in by_ref
            )
            resp, usage, latency = self.ask(
                task="extract.clause",
                system=SYSTEM,
                user=(
                    f"TARGET CLAUSE TYPE: {clause_type}\n"
                    f"WHAT THIS CLAUSE TYPE COVERS: "
                    f"{rule.title if rule else clause_type}\n\n"
                    f"CANDIDATE SECTIONS:\n{candidates}"
                ),
                schema=ExtractionResponse,
                context={
                    "clause_type": clause_type,
                    "sections": [by_ref[h["section_ref"]] for h in hits if h["section_ref"] in by_ref],
                    "contract_text": text,
                },
            )

            span = None
            notes = resp.notes
            confidence = resp.confidence
            if resp.found and resp.quoted_text.strip():
                located = _locate(resp.quoted_text, text)
                if located:
                    span = ClauseSpan(
                        text=text[located[0] : located[1]],
                        char_start=located[0],
                        char_end=located[1],
                        section_ref=resp.section_ref,
                    )
                else:
                    # The model quoted something that is not in the document.
                    # Recorded as not-found with the confidence zeroed, so the
                    # verifier and the human gate both see it for what it is.
                    notes = (
                        f"{notes} | quoted text not present in source document; "
                        "treated as not found"
                    ).strip(" |")
                    confidence = 0.0
                    self.emit_error(
                        "ungrounded_quote",
                        f"{clause_type}: extractor returned a quote absent from "
                        f"the source ({resp.quoted_text[:90]!r})",
                    )

            finding = ClauseFinding(
                clause_type=clause_type,
                found=span is not None,
                span=span,
                extraction_confidence=confidence,
                retrieval_section_refs=[h["section_ref"] for h in hits],
                notes=notes or None,
            )
            findings.append(finding)
            self.emit(
                MessageType.CLAUSE_FINDING,
                finding,
                recipient=AgentRole.POLICY,
                parent=call_msg,
                usage=usage,
                latency_ms=latency,
            )

        return {"findings": findings, "status": "extracted", "repair_targets": []}
