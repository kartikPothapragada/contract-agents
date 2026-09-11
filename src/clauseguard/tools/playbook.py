"""Playbook store -- the retrieval tool the Policy agent calls.

Loads ``data/playbook/playbook.yaml`` into typed rules and indexes them for
hybrid retrieval. Also carries the scoring constants (weights, thresholds,
multipliers), because those are *policy*, owned by the legal team and versioned
with the playbook -- not constants buried in Python.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clauseguard.tools.retrieval import Document, Hit, HybridRetriever

DEFAULT_PLAYBOOK = Path(__file__).resolve().parents[3] / "data" / "playbook" / "playbook.yaml"


@dataclass(frozen=True)
class PlaybookRule:
    rule_id: str
    clause_type: str
    title: str
    criticality_weight: float
    owner: str
    standard_position: str
    fallback_position: str
    unacceptable_triggers: tuple[str, ...]
    keywords: tuple[str, ...]

    def index_text(self) -> str:
        """Text the retriever sees.

        Clause type and keywords are repeated into the indexed text: BM25 has no
        notion of a field, so the cheapest way to give the clause-type label
        lexical weight is to let it appear more than once.
        """
        return (
            f"{self.clause_type} {self.clause_type.replace('_', ' ')} {self.title}. "
            f"Keywords: {', '.join(self.keywords)}. "
            f"Standard position: {self.standard_position} "
            f"Fallback: {self.fallback_position} "
            f"Never acceptable: {'; '.join(self.unacceptable_triggers)}"
        )

    def as_prompt_block(self) -> str:
        triggers = "\n".join(f"  - {t}" for t in self.unacceptable_triggers)
        return (
            f"RULE {self.rule_id} ({self.clause_type}) -- {self.title}\n"
            f"STANDARD POSITION: {self.standard_position}\n"
            f"PRE-APPROVED FALLBACK: {self.fallback_position}\n"
            f"NEVER ACCEPTABLE:\n{triggers}"
        )


class PlaybookStore:
    def __init__(self, path: str | Path = DEFAULT_PLAYBOOK) -> None:
        self.path = Path(path)
        raw: dict[str, Any] = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.version: str = raw["playbook_version"]
        self.entity: str = raw["entity"]
        self.thresholds: dict[str, float] = raw["thresholds"]
        self.value_multipliers: dict[str, float] = raw["value_multipliers"]
        self.severity_points: dict[str, float] = raw["severity_points"]

        self.rules: dict[str, PlaybookRule] = {}
        for r in raw["rules"]:
            rule = PlaybookRule(
                rule_id=r["rule_id"],
                clause_type=r["clause_type"],
                title=r["title"],
                criticality_weight=float(r["criticality_weight"]),
                owner=r["owner"],
                standard_position=" ".join(r["standard_position"].split()),
                fallback_position=" ".join(r["fallback_position"].split()),
                unacceptable_triggers=tuple(r["unacceptable_triggers"]),
                keywords=tuple(r.get("keywords", [])),
            )
            self.rules[rule.rule_id] = rule

        self._by_clause_type = {r.clause_type: r for r in self.rules.values()}
        self._retriever = HybridRetriever(
            [Document(doc_id=r.rule_id, text=r.index_text()) for r in self.rules.values()]
        )

    # -- tool surface ------------------------------------------------------ #
    def clause_types(self) -> list[str]:
        return sorted(self._by_clause_type)

    def by_clause_type(self, clause_type: str) -> PlaybookRule | None:
        return self._by_clause_type.get(clause_type)

    def retrieve(self, query: str, k: int = 3, mode: str = "hybrid") -> list[tuple[PlaybookRule, Hit]]:
        """Hybrid lookup. Returns rules with their fusion hits for tracing."""
        return [(self.rules[h.doc_id], h) for h in self._retriever.search(query, k=k, mode=mode)]

    def criticality(self, clause_type: str) -> float:
        rule = self._by_clause_type.get(clause_type)
        # Unknown clause types default to mid criticality rather than zero:
        # scoring an unrecognised clause as harmless is the wrong failure
        # direction for a risk system.
        return rule.criticality_weight if rule else 0.5


@functools.lru_cache(maxsize=4)
def load_playbook(path: str | Path = DEFAULT_PLAYBOOK) -> PlaybookStore:
    """Cached loader. Embedding the playbook on every graph run is wasted latency."""
    return PlaybookStore(path)
