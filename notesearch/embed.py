"""Embedding back ends.

FakeEmbedder: deterministic, no network, for tests.
LocalEmbedder: runs a small CPU model inside your own container (fastembed /
ONNX). Default model nomic-ai/nomic-embed-text-v1.5, with the task prefixes
that model expects. No outside AI service is called.
"""
from __future__ import annotations

import hashlib
import os
import re

import numpy as np

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


def _normalize(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (m / n).astype(np.float32)


class FakeEmbedder:
    """Bag-of-words hashed into a fixed vector. Good enough to test ranking."""

    name = "fake-hash-256"
    dim = 256

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            if len(tok) < 3:
                continue
            h = int.from_bytes(hashlib.sha1(tok.encode()).digest()[:4], "big")
            v[h % self.dim] += 1.0
        return v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.stack([self._vec(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


class LocalEmbedder:
    def __init__(self, model: str | None = None, cache_dir: str | None = None):
        from fastembed import TextEmbedding  # imported here so tests don't need it

        self.name = model or os.environ.get("NS_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
        self._model = TextEmbedding(model_name=self.name, cache_dir=cache_dir)
        self._prefix = "nomic" in self.name

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        if self._prefix:
            texts = [DOC_PREFIX + t for t in texts]
        return _normalize(np.array(list(self._model.embed(texts, batch_size=16))))

    def embed_query(self, text: str) -> np.ndarray:
        if self._prefix:
            text = QUERY_PREFIX + text
        return _normalize(np.array(list(self._model.embed([text]))))[0]
