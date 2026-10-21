"""BM25 field search for progressive disclosure — find fields without
reading a whole domain fragment.

Field names come from endpoint function names (developer vocabulary,
keyword-dense), and descriptions come from docstrings — exactly the text
shape lexical search handles well (no embeddings needed). The index is
Okapi BM25 with standard k1/b constants, self-contained (no dependencies),
lazily built and cached per domain (the schema is immutable after build).
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    """NFKC-normalized alphanumeric tokens, lowercase, length >= 2."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"[^\W_]{2,}", normalized)


@dataclass(frozen=True)
class FieldDoc:
    """One searchable field: the brief progressive tools already build."""

    name: str
    type: str
    description: str = ""
    args: list[dict[str, Any]] = field(default_factory=list)
    operation: str = "query"  # or "mutation"

    @property
    def searchable_text(self) -> str:
        """Name (split on underscores) + description + arg names/descriptions."""
        parts = [self.name.replace("_", " ")]
        if self.description:
            parts.append(self.description)
        for arg in self.args:
            parts.append(str(arg.get("name", "")))
            parts.append(str(arg.get("description") or ""))
        return " ".join(parts)


@dataclass
class _ScoredDoc:
    doc: FieldDoc
    score: float


class FieldSearchIndex:
    """Okapi BM25 index over one domain's fields.

    Build once (lazily), search many times. Fields with zero matching
    tokens score 0 and are excluded from results.
    """

    def __init__(self, docs: list[FieldDoc]) -> None:
        self._docs = docs
        self._doc_tokens = [_tokenize(d.searchable_text) for d in docs]
        self._doc_lengths = [len(t) for t in self._doc_tokens]
        self._n = len(docs)
        self._avg_dl = sum(self._doc_lengths) / self._n if self._n else 0.0
        self._df: dict[str, int] = {}
        self._tf: list[dict[str, int]] = []
        for tokens in self._doc_tokens:
            tf: dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
            self._tf.append(tf)
            for token in set(tokens):
                self._df[token] = self._df.get(token, 0) + 1

    def search(self, query: str, top_k: int = 5) -> list[tuple[FieldDoc, float]]:
        """Top-k (doc, score) pairs sorted by BM25 score, zero-scores excluded."""
        query_tokens = _tokenize(query)
        if not query_tokens or not self._n:
            return []
        scores: list[float] = [0.0] * self._n
        for token in query_tokens:
            df = self._df.get(token)
            if df is None:
                continue  # token appears in no field
            idf = math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)
            for i in range(self._n):
                tf = self._tf[i].get(token, 0)
                if tf == 0:
                    continue
                dl = self._doc_lengths[i]
                numerator = tf * (_K1 + 1)
                denominator = tf + _K1 * (1 - _B + _B * dl / self._avg_dl)
                scores[i] += idf * numerator / denominator
        ranked = sorted(
            (_ScoredDoc(self._docs[i], scores[i]) for i in range(self._n)),
            key=lambda sd: sd.score,
            reverse=True,
        )
        return [(sd.doc, sd.score) for sd in ranked[:top_k] if sd.score > 0]
