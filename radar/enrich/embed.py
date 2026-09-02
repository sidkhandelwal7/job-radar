"""Local sentence embeddings (sentence-transformers, CPU). No embedding API, ever (§18).

Model: all-MiniLM-L6-v2 (22M params; ~1k short texts/s on Apple silicon CPU). Vectors are cached on
disk by content hash so re-scoring never re-embeds. If the `ml` extra isn't installed the module
degrades gracefully: `available()` is False and callers fall back to lexical similarity.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any

from radar.config import Config

log = logging.getLogger("radar.embed")

_MODEL: Any = None
_MODEL_NAME: str | None = None


def available() -> bool:
    try:
        import sentence_transformers  # noqa: F401

        return True
    except ImportError:
        return False


def _load(name: str) -> Any:
    global _MODEL, _MODEL_NAME
    if _MODEL is None or name != _MODEL_NAME:
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer(name, device="cpu")
        _MODEL_NAME = name
    return _MODEL


class EmbeddingCache:
    """Tiny content-addressed store: data/cache/embeddings/<model>/<sha>.npy (float32)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.name = cfg.embeddings.model
        self.dir = cfg._abs(cfg.embeddings.cache_dir) / self.name.replace("/", "__")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, Any] = {}

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.npy"

    def get_many(self, texts: list[str]) -> list[Any]:
        import numpy as np

        out: list[Any] = [None] * len(texts)
        missing: list[int] = []
        for i, t in enumerate(texts):
            k = self.key(t)
            v = self._mem.get(k)
            if v is None:
                p = self._path(k)
                if p.exists():
                    try:
                        v = np.load(p)
                    except (OSError, ValueError):
                        v = None
            if v is None:
                missing.append(i)
            else:
                self._mem[k] = v
                out[i] = v
        if missing:
            model = _load(self.name)
            vecs = model.encode(
                [texts[i] for i in missing],
                batch_size=64,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            for i, v in zip(missing, vecs, strict=True):
                v = v.astype("float32")
                k = self.key(texts[i])
                self._mem[k] = v
                out[i] = v
                with contextlib.suppress(OSError):
                    np.save(self._path(k), v)
        return out

    def get(self, text: str) -> Any:
        return self.get_many([text])[0]


def cosine(a: Any, b: Any) -> float:
    import numpy as np

    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def posting_text(title: str, description: str | None, company: str | None = None) -> str:
    body = (description or "")[:1800]
    return f"{title}. {company or ''}\n{body}".strip()
