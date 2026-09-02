"""Laptop side of the Actions split: fold `actions/deltas/*.jsonl` into the database.

For each delta line:
  * posting exists (same provider/slug/job_id) → first_seen_at = min(existing, seen_at); event `seen_by_actions`
  * posting missing → provisional insert from the delta fields (the next laptop fetch of that
    source fills in description/comp via the normal upsert path, since the natural key matches)
Idempotent: a kv cursor remembers the last (file, line) ingested.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from radar import db
from radar.config import Config
from radar.fetch.pipeline import upsert_jobs
from radar.fetch.registry import CompanyMatcher
from radar.models import RawJob, SourceSpec
from radar.util import utcnow_iso


def _spec_for(line: dict[str, Any], conn: sqlite3.Connection) -> SourceSpec:
    row = db.one(
        conn,
        "SELECT cs.company_id, c.slug, c.name, c.tier, c.is_dream_list, c.target_category, c.is_quant_trading_firm "
        "FROM company_sources cs JOIN companies c ON c.id = cs.company_id WHERE cs.provider = ? AND cs.slug = ?",
        (line["provider"], line["slug"]),
    )
    extra = {}
    if row:
        extra = {
            "company_id": row["company_id"],
            "company_tier": row["tier"],
            "is_dream_list": row["is_dream_list"],
            "target_category": row["target_category"],
            "is_quant_trading_firm": row["is_quant_trading_firm"],
        }
    return SourceSpec(
        provider=line["provider"],
        slug=line["slug"],
        company_slug=(row["slug"] if row else line.get("company_slug") or "unknown"),
        company_name=(row["name"] if row else line["company"]),
        extra=extra,
    )


def ingest(
    conn: sqlite3.Connection, cfg: Config, *, actions_dir: Path, days: int = 14
) -> dict[str, int]:
    files = sorted((actions_dir / "deltas").glob("*.jsonl"))[-days:]
    cursor = db.kv_get(conn, "actions_delta_cursor", {})
    res = {"files": 0, "lines": 0, "backdated": 0, "inserted": 0, "already": 0}
    matcher = CompanyMatcher(conn)
    for f in files:
        done = int(cursor.get(f.name, 0))
        lines = f.read_text(encoding="utf-8").splitlines()
        if len(lines) <= done:
            continue
        res["files"] += 1
        for raw in lines[done:]:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            res["lines"] += 1
            row = db.one(
                conn,
                "SELECT id, first_seen_at FROM postings WHERE source_provider = ? AND source_slug = ? AND source_job_id = ?",
                (line["provider"], line["slug"], line["job_id"]),
            )
            seen_at = line["seen_at"]
            if row:
                if seen_at < row["first_seen_at"]:
                    with db.transaction(conn):
                        db.update(conn, "postings", row["id"], {"first_seen_at": seen_at})
                        db.add_event(
                            conn,
                            row["id"],
                            "seen_by_actions",
                            {"seen_at": seen_at, "alert": line.get("alert")},
                        )
                    res["backdated"] += 1
                else:
                    res["already"] += 1
                continue
            spec = _spec_for(line, conn)
            job = RawJob(
                source_job_id=line["job_id"],
                title=line["title"],
                apply_url=line["apply_url"],
                canonical_url=line.get("canonical_url"),
                company_name=line["company"],
                locations=line.get("locations") or [],
                posted_at=line.get("posted_at"),
            )
            company = None
            if line["provider"] == "github":
                hit = matcher.match(line["company"])
                if hit:
                    c = db.one(
                        conn,
                        "SELECT id, name, tier, is_dream_list, target_category, is_quant_trading_firm FROM companies WHERE id = ?",
                        (hit[0],),
                    )
                    company = {
                        "id": c["id"],
                        "name": c["name"],
                        "tier": c["tier"],
                        "is_dream_list": c["is_dream_list"],
                        "target_category": c["target_category"],
                        "is_quant_trading_firm": c["is_quant_trading_firm"],
                    }
                else:
                    company = {
                        "id": None,
                        "name": line["company"],
                        "tier": None,
                        "is_dream_list": 0,
                        "target_category": None,
                        "is_quant_trading_firm": 0,
                    }
            r = upsert_jobs(
                conn,
                spec,
                None,
                [job],
                {},
                company_id=spec.extra.get("company_id"),
                payload_id=None,
                run_id=None,
                suppress_delist=True,
                company_resolver=_Static(company) if company else None,
            )
            if r["new_ids"]:
                with db.transaction(conn):
                    db.update(conn, "postings", r["new_ids"][0], {"first_seen_at": seen_at})
                    db.add_event(
                        conn,
                        r["new_ids"][0],
                        "seen_by_actions",
                        {"seen_at": seen_at, "alert": line.get("alert"), "provisional": True},
                    )
                res["inserted"] += 1
        cursor[f.name] = len(lines)
    db.kv_set(conn, "actions_delta_cursor", cursor)
    # Telegram button taps drained by the cloud job (raw updates; legacy lines carried posting_id)
    apath = actions_dir / "actions.jsonl"
    res["actions_applied"] = 0
    if apath.exists():
        from radar.notify.channels import Telegram
        from radar.notify.telegram_actions import apply_updates
        from radar.workflow import apply_action

        done = int(db.kv_get(conn, "actions_actions_cursor", 0))
        alines = apath.read_text(encoding="utf-8").splitlines()
        updates = []
        for raw in alines[done:]:
            try:
                a = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(a.get("update"), dict):
                updates.append(a["update"])
            elif "posting_id" in a:  # pre-webhook format
                r = apply_action(
                    conn,
                    int(a["posting_id"]),
                    a["action"],
                    reason="dismissed from Telegram (cloud)",
                    days=7,
                    via="telegram/actions",
                )
                res["actions_applied"] += int(bool(r.get("ok")))
        if updates:
            tg = Telegram()
            if not tg.available():
                tg.chat_id = tg.chat_id or "pending"
            st = apply_updates(conn, tg, updates, advance_offset=False)
            res["actions_applied"] += st["applied"]
        db.kv_set(conn, "actions_actions_cursor", len(alines))
    _ = utcnow_iso()
    return res


class _Static:
    def __init__(self, company: dict[str, Any]) -> None:
        self.company = company

    def resolve(self, job: RawJob) -> dict[str, Any]:
        return self.company

    def flush_unknown(self) -> int:
        return 0
