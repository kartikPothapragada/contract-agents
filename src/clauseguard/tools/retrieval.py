"""Hybrid retrieval over the contracting playbook.

The Policy agent does not get the playbook pasted into its prompt. It *queries*
the playbook through this tool and reasons over the rules it gets back.

Why that matters for this design
--------------------------------
Stuffing 14 rules into a prompt would work today and break at 300 rules, which
is the size a real playbook reaches. Making retrieval a tool boundary from the
start means the scaling story is "swap the index", not "rewrite the agent".

Why hybrid rather than dense-only
---------------------------------
Playbook lookup is a mixed query type. Some signals are semantic ("vendor is not
on the hook if they leak our data" -> indemnification), and some are lexical and
unforgiving: "net 45", "72 hours", "99.5%", "SOC 2 Type II". Dense retrieval
blurs exactly those numerals and proper nouns; BM25 nails them and is helpless
on paraphrase. Reciprocal Rank Fusion is used rather than score interpolation
because BM25 scores and cosine similarities are not on a comparable scale, and
normalising them introduces a tuning parameter that has to be re-fit whenever
the corpus changes. RRF needs no calibration.

``scripts/eval_retrieval.py`` reports dense-only vs BM25-only vs RRF on a
labelled query set so the choice is evidenced, not asserted.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")

_STOP = {
    "the", "a", "an", "of", "to", "and", "or", "in", "for", "is", "are", "be",
    "by", "on", "at", "with", "that", "this", "it", "as", "any", "all", "such",
    "shall", "will", "may", "must", "not", "no", "which", "from", "under",
}


def tokenize(text: str) -> list[str]:
    """Lowercase word/number tokens, stopwords dropped.

    Numerals are deliberately kept whole ("99.5", "45", "72") -- they carry most
    of the discriminative signal in contract policy text.
    """
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass
class Document:
    doc_id: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Hit:
    doc_id: str
    score: float
    rank: int
    retriever: str


# --------------------------------------------------------------------------- #
# BM25 (Okapi), implemented directly -- ~40 lines, one fewer dependency, and the
# parameters stay visible instead of hiding in a library default.
# --------------------------------------------------------------------------- #
class BM25:
    def __init__(self, docs: Sequence[Document], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = list(docs)
        self._tokens = [tokenize(d.text) for d in self.docs]
        self._lens = [len(t) for t in self._tokens]
        self._avg_len = (sum(self._lens) / len(self._lens)) if self._lens else 0.0
        self._tfs = [Counter(t) for t in self._tokens]
        n_docs = len(self.docs)
        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        # Robertson/Sparck-Jones IDF with the +0.5 smoothing that keeps common
        # terms from going negative on small corpora (a playbook is small).
        self._idf = {
            term: math.log(1 + (n_docs - n + 0.5) / (n + 0.5)) for term, n in df.items()
        }

    def search(self, query: str, k: int = 5) -> list[Hit]:
        q = tokenize(query)
        scored: list[tuple[str, float]] = []
        for i, doc in enumerate(self.docs):
            tf, dl = self._tfs[i], self._lens[i]
            s = 0.0
            for term in q:
                if term not in tf:
                    continue
                f = tf[term]
                denom = f + self.k1 * (1 - self.b + self.b * dl / max(self._avg_len, 1e-9))
                s += self._idf.get(term, 0.0) * f * (self.k1 + 1) / denom
            if s > 0:
                scored.append((doc.doc_id, s))
        scored.sort(key=lambda x: -x[1])
        return [Hit(d, s, r + 1, "bm25") for r, (d, s) in enumerate(scored[:k])]


# --------------------------------------------------------------------------- #
# Dense retriever. sentence-transformers when available; TF-IDF + truncated SVD
# otherwise, so the system has no hard dependency on a model download.
# --------------------------------------------------------------------------- #
class DenseRetriever:
    def __init__(
        self,
        docs: Sequence[Document],
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> None:
        self.docs = list(docs)
        self.backend = "none"
        self._matrix = None
        self._model = None
        self._vectorizer = None
        texts = [d.text for d in self.docs]
        if not texts:
            return
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name)
            self._matrix = self._model.encode(texts, normalize_embeddings=True)
            self.backend = f"sentence-transformers:{model_name}"
        except Exception:
            from sklearn.decomposition import TruncatedSVD
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import Normalizer

            n_comp = max(2, min(64, len(texts) - 1))
            self._vectorizer = make_pipeline(
                TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), stop_words="english"),
                TruncatedSVD(n_components=n_comp, random_state=42),
                Normalizer(copy=False),
            )
            self._matrix = self._vectorizer.fit_transform(texts)
            self.backend = f"tfidf-svd:{n_comp}"

    def search(self, query: str, k: int = 5) -> list[Hit]:
        if self._matrix is None:
            return []
        import numpy as np

        if self._model is not None:
            q = self._model.encode([query], normalize_embeddings=True)
        else:
            q = self._vectorizer.transform([query])
        sims = np.asarray(self._matrix @ np.asarray(q).T).ravel()
        order = sims.argsort()[::-1][:k]
        return [
            Hit(self.docs[i].doc_id, float(sims[i]), r + 1, "dense")
            for r, i in enumerate(order)
        ]


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def reciprocal_rank_fusion(
    runs: Iterable[Sequence[Hit]], k: int = 5, rrf_k: int = 60
) -> list[Hit]:
    """Standard RRF: score(d) = sum over runs of 1 / (rrf_k + rank(d)).

    rrf_k=60 is the value from Cormack et al. (2009); it damps the influence of
    a single retriever's top hit enough that one confidently-wrong retriever
    cannot dominate the fused list. Left unturned deliberately -- tuning it on a
    20-query set would be fitting noise.
    """
    agg: dict[str, float] = {}
    for run in runs:
        for hit in run:
            agg[hit.doc_id] = agg.get(hit.doc_id, 0.0) + 1.0 / (rrf_k + hit.rank)
    ranked = sorted(agg.items(), key=lambda x: -x[1])
    return [Hit(d, s, r + 1, "rrf") for r, (d, s) in enumerate(ranked[:k])]


class HybridRetriever:
    """BM25 + dense, fused with RRF. Exposes each leg for ablation."""

    def __init__(self, docs: Sequence[Document]) -> None:
        self.docs = {d.doc_id: d for d in docs}
        self.bm25 = BM25(docs)
        self.dense = DenseRetriever(docs)

    def search(self, query: str, k: int = 3, mode: str = "hybrid") -> list[Hit]:
        if mode == "bm25":
            return self.bm25.search(query, k)
        if mode == "dense":
            return self.dense.search(query, k)
        # Over-fetch each leg before fusing: RRF can only reorder what it sees.
        return reciprocal_rank_fusion(
            [self.bm25.search(query, k * 3), self.dense.search(query, k * 3)], k=k
        )
