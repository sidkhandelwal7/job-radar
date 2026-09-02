"""Append-only raw payload store: every fetched body, gzipped, addressed by sha256, forever.

Layout: data/raw/<provider>/<slug-safe>/<YYYYMMDD>/<HHMMSS>_<sha8>.json.gz
A payload identical to the previous one for the same source is recorded in raw_payloads with
unchanged=1 and points at the same file (no duplicate bytes).
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path
from typing import Any

from radar import db
from radar.util import sha256_bytes, slugify, utcnow

MAX_FILE_BYTES = 200 * 1024 * 1024


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, provider: str, slug: str, sha: str, kind: str) -> Path:
        now = utcnow()
        d = self.root / provider / slugify(slug)[:80] / now.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{now.strftime('%H%M%S')}_{kind}_{sha[:8]}.json.gz"

    def store(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        slug: str,
        url: str,
        content: bytes,
        http_status: int | None,
        run_id: int | None,
        source_id: int | None,
        kind: str = "list",
        row_count: int | None = None,
    ) -> tuple[int, bool]:
        """Persist a payload. Returns (raw_payload_id, unchanged)."""
        sha = sha256_bytes(content)
        prev = db.one(
            conn,
            "SELECT id, sha256, path FROM raw_payloads WHERE provider = ? AND slug = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (provider, slug, kind),
        )
        unchanged = bool(prev and prev["sha256"] == sha)
        if unchanged:
            path = prev["path"]
        else:
            p = self._path_for(provider, slug, sha, kind)
            with gzip.open(p, "wb", compresslevel=6) as f:
                f.write(content)
            path = str(p.relative_to(self.root))
        rid = db.insert(
            conn,
            "raw_payloads",
            {
                "run_id": run_id,
                "source_id": source_id,
                "provider": provider,
                "slug": slug,
                "url": url,
                "fetched_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "http_status": http_status,
                "sha256": sha,
                "byte_size": len(content),
                "path": path,
                "row_count": row_count,
                "unchanged": int(unchanged),
                "kind": kind,
            },
        )
        return rid, unchanged

    def read(self, path: str) -> bytes:
        with gzip.open(self.root / path, "rb") as f:
            return f.read()

    def read_json(self, path: str) -> Any:
        return json.loads(self.read(path).decode("utf-8", errors="replace"))

    def read_payload(self, conn: sqlite3.Connection, payload_id: int) -> bytes | None:
        row = db.one(conn, "SELECT path FROM raw_payloads WHERE id = ?", (payload_id,))
        return self.read(row["path"]) if row else None
