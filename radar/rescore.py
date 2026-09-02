"""`radar rescore`: recompute everything downstream of the raw payloads with zero re-fetching.

Phase 1 scope: re-parse the latest list + detail payloads per source through the current adapters
and normalization rules, and upsert the derived columns. Workflow columns and the applications
table are never touched. Phase 3 adds classification + scoring on top (see radar.score).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from radar import db
from radar.config import Config
from radar.fetch.adapters import ADAPTERS
from radar.fetch.pipeline import upsert_jobs
from radar.fetch.raw_store import RawStore
from radar.fetch.registry import source_specs
from radar.models import SourceSpec

log = logging.getLogger("radar.rescore")


@dataclass
class RescoreStats:
    sources: int = 0
    replayed: int = 0
    changed: int = 0
    skipped: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    scored: int = 0


def _latest_list_payload(conn: sqlite3.Connection, spec: SourceSpec) -> sqlite3.Row | None:
    return db.one(
        conn,
        "SELECT id, path FROM raw_payloads WHERE provider = ? AND slug = ? AND kind = 'list' AND http_status = 200 ORDER BY id DESC LIMIT 1",
        (spec.provider, spec.slug),
    )


def _detail_bodies(conn: sqlite3.Connection, store: RawStore, spec: SourceSpec) -> dict[str, str]:
    """url → body for every stored detail page of this source (latest wins)."""
    out: dict[str, str] = {}
    rows = db.all_rows(
        conn,
        "SELECT path FROM raw_payloads WHERE provider = ? AND slug = ? AND kind = 'detail' ORDER BY id ASC",
        (spec.provider, spec.slug),
    )
    for r in rows:
        try:
            doc = store.read_json(r["path"])
        except (OSError, json.JSONDecodeError):
            continue
        for d in doc.get("details", []):
            if d.get("status") == 200:
                out[d["url"]] = d["body"]
    return out


def replay_sources(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    company: str | None = None,
    providers: set[str] | None = None,
) -> RescoreStats:
    t0 = time.monotonic()
    stats = RescoreStats()
    store = RawStore(cfg.raw_dir)
    specs = source_specs(conn, providers=providers, company=company, only_enabled=False)
    for spec in specs:
        stats.sources += 1
        cls = ADAPTERS.get(spec.provider)
        if cls is None:
            stats.skipped.append(f"{spec.key}: no adapter")
            continue
        payload = _latest_list_payload(conn, spec)
        if payload is None:
            stats.skipped.append(f"{spec.key}: no stored payload")
            continue
        adapter = cls(None)  # offline: replay never performs network I/O
        try:
            jobs = adapter.parse_payload(spec, store.read(payload["path"]))
        except Exception as e:
            stats.skipped.append(f"{spec.key}: parse failed: {e}")
            continue
        details = _detail_bodies(conn, store, spec)
        if details:
            for job in jobs:
                url = adapter.detail_request_url(spec, job)
                if url and url in details:
                    adapter.apply_detail(job, details[url].encode("utf-8"))
        existing = {
            r["source_job_id"]: r
            for r in db.all_rows(
                conn,
                "SELECT id, source_job_id, content_hash, raw_hash, delisted_at, description_fetched FROM postings WHERE source_provider = ? AND source_slug = ?",
                (spec.provider, spec.slug),
            )
        }
        from radar.fetch.pipeline import CompanyResolver

        resolver = CompanyResolver(conn) if spec.provider == "github" else None
        res = upsert_jobs(
            conn,
            spec,
            None,
            jobs,
            existing,
            company_id=spec.extra.get("company_id"),
            payload_id=payload["id"],
            run_id=None,
            suppress_delist=True,  # replay is not evidence of delisting
            force_derived=True,  # rule edits must propagate
            company_resolver=resolver,
        )
        if resolver:
            resolver.flush_unknown()
        stats.replayed += len(jobs)
        stats.changed += res["changed"] + res["new"]
    stats.elapsed_s = round(time.monotonic() - t0, 2)
    return stats


def snapshot_config(conn: sqlite3.Connection, cfg: Config, note: str | None = None) -> None:
    """Record the config used for this rescore (config_versions is append-only)."""
    from radar.config import config_path
    from radar.util import sha256_text, utcnow_iso

    text = config_path().read_text()
    sha = sha256_text(text)
    last = db.one(conn, "SELECT sha256 FROM config_versions ORDER BY id DESC LIMIT 1")
    if last and last["sha256"] == sha:
        return
    db.insert(
        conn,
        "config_versions",
        {"sha256": sha, "yaml_text": text, "note": note, "created_at": utcnow_iso()},
    )


def rescore(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    replay: bool = False,
    company: str | None = None,
    providers: set[str] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    run_id = db.start_run(conn, "rescore")
    out: dict[str, Any] = {}
    try:
        snapshot_config(conn, cfg)
        if replay:
            st = replay_sources(conn, cfg, company=company, providers=providers)
            out["replay"] = st.__dict__
        from radar.dedupe.cluster import run_clustering

        cs = run_clustering(conn, run_id=run_id)
        out["clustering"] = {k: v for k, v in cs.__dict__.items() if k != "examples"}
        # Phase 3 hooks in here:
        try:
            from radar.score.engine import score_all

            out["score"] = score_all(
                conn, cfg, run_id=run_id, full=full or (replay and not company and not providers)
            )
        except ImportError:
            out["score"] = {"status": "scoring engine not built yet (Phase 3)"}
        db.finish_run(conn, run_id, stats=out)
    except Exception as e:
        db.finish_run(conn, run_id, status="failed", error=str(e))
        raise
    return out
