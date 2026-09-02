"""Approved retention policy (D62): R1 + R3. R2 (deleting posting rows) was explicitly held —
postings are never deleted here, only redundant *copies* of text and payloads are.

R1 — descriptions of out-of-scope rows are dropped from posting_docs (the raw store keeps every
     byte for `radar rescore --replay`; a rule change that brings a row into scope repopulates it).
     Rows that are applied/shortlisted or have an application record are never touched.
R3 — raw payload files older than RAW_KEEP_DAYS are deleted unless referenced by an in-scope or
     applied posting. The append-only story is preserved where it matters: everything recent, and
     everything that feeds a row you could act on.
Registry audit — sources whose entire history produced (almost) no in-scope rows are disabled with
     `disabled_reason='low_yield'`; the nightly re-enables them after REPROBE_DAYS for a fresh look,
     and the next audit re-disables persistent duds. sync_registry respects disabled_reason.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from radar import db
from radar.config import Config
from radar.util import utcnow, utcnow_iso

RAW_KEEP_DAYS = 90
REPROBE_DAYS = 14
MIN_SCANS_BEFORE_JUDGING = 2
LOW_YIELD_MAX_INSCOPE = 3  # fewer in-scope rows than this AND
LOW_YIELD_MAX_RATE = 0.005  # a lower in-scope rate than this = low yield


def prune_out_of_scope_descriptions(conn: sqlite3.Connection, *, limit: int | None = None) -> int:
    """R1. The UPDATE fires the FTS triggers, so the search index stays consistent."""
    sql = """
        UPDATE posting_docs SET description_md = NULL WHERE posting_id IN (
            SELECT d.posting_id FROM posting_docs d JOIN postings p ON p.id = d.posting_id
            WHERE d.description_md IS NOT NULL AND p.in_scope = 0
              AND p.status NOT IN ('applied', 'shortlisted')
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id)
    """
    sql += f" LIMIT {int(limit)})" if limit else ")"
    with db.transaction(conn):
        cur = conn.execute(sql)
        n = cur.rowcount
    return n


def prune_raw_payloads(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """R3. Delete payload files (and their index rows) older than RAW_KEEP_DAYS that no in-scope or
    applied posting references."""
    cutoff = (utcnow() - timedelta(days=RAW_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.all_rows(
        conn,
        """SELECT id, path FROM raw_payloads WHERE fetched_at < ?
           AND id NOT IN (
             SELECT DISTINCT raw_payload_ref FROM postings
             WHERE raw_payload_ref IS NOT NULL
               AND (in_scope = 1 OR status = 'applied'
                    OR EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = postings.id))
           )""",
        (cutoff,),
    )
    deleted = missing = freed = 0
    with db.transaction(conn):
        for r in rows:
            p = Path(r["path"])
            if not p.is_absolute():
                p = cfg.root / p
            try:
                freed += p.stat().st_size
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                missing += 1
            conn.execute("DELETE FROM raw_payloads WHERE id = ?", (r["id"],))
    return {
        "deleted": deleted,
        "missing": missing,
        "freed_mb": round(freed / 1e6, 1),
        "keep_days": RAW_KEEP_DAYS,
    }


def registry_audit(
    conn: sqlite3.Connection, *, apply: bool = False, extra_disable: list[str] | None = None
) -> dict[str, Any]:
    """Disable low-yield sources (with reason + timestamp), never anything that ever fed the queue,
    never curated tier-1/2/dream companies. Idempotent; `apply=False` only reports."""
    cands = db.all_rows(
        conn,
        """SELECT cs.id, c.name, cs.provider, cs.slug,
                  COALESCE(SUM(p.in_scope), 0) sc, COUNT(p.id) n,
                  MAX(CASE WHEN p.apply_priority_rank IS NOT NULL THEN 1 ELSE 0 END) fed_queue
           FROM company_sources cs
           JOIN companies c ON c.id = cs.company_id
           LEFT JOIN postings p ON p.source_provider = cs.provider AND p.source_slug = cs.slug
           WHERE cs.enabled = 1 AND cs.disabled_reason IS NULL
             AND c.is_dream_list = 0 AND COALESCE(c.tier, 3) >= 3
             AND COALESCE(c.target_category, 'other') = 'other'  -- a quiet company in a target category stays: its next req is the point
           GROUP BY cs.id
           HAVING n > 0 AND fed_queue = 0
              AND (sc = 0 OR (sc < ? AND sc * 1.0 / n < ?))""",
        (LOW_YIELD_MAX_INSCOPE, LOW_YIELD_MAX_RATE),
    )
    by_name = {(r["provider"], r["slug"]): r for r in cands}
    now = utcnow_iso()
    disabled: list[str] = []
    if apply:
        with db.transaction(conn):
            for r in cands:
                conn.execute(
                    "UPDATE company_sources SET enabled = 0, disabled_reason = 'low_yield', disabled_at = ? WHERE id = ?",
                    (now, r["id"]),
                )
                disabled.append(
                    f"{r['name']} ({r['provider']}:{r['slug']}) {r['sc']}/{r['n']} in scope"
                )
            for name in extra_disable or []:
                conn.execute(
                    "UPDATE company_sources SET enabled = 0, disabled_reason = 'broken_low_yield', disabled_at = ? "
                    "WHERE enabled = 1 AND company_id IN (SELECT id FROM companies WHERE name = ?)",
                    (now, name),
                )
                disabled.append(f"{name} (broken_low_yield)")
    return {
        "candidates": len(by_name),
        "postings_carried": sum(r["n"] for r in cands),
        "applied": apply,
        "disabled": disabled
        if apply
        else [f"{r['name']} ({r['provider']}:{r['slug']}) {r['sc']}/{r['n']}" for r in cands[:20]],
    }


def reprobe_disabled(conn: sqlite3.Connection) -> int:
    """The undo: after REPROBE_DAYS, give low-yield sources one fresh look. The next audit
    re-disables the ones that still yield nothing."""
    cutoff = (utcnow() - timedelta(days=REPROBE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE company_sources SET enabled = 1, disabled_reason = NULL, disabled_at = NULL "
            "WHERE enabled = 0 AND disabled_reason = 'low_yield' AND disabled_at < ?",
            (cutoff,),
        )
    return cur.rowcount
