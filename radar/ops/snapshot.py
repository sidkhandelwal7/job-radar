"""Weekly velocity snapshot (§4 side jobs, §16 cadences) and the learned time-to-close (§4).

The delist watcher is how the system *learns* each company's true time-to-close: a posting that
vanished closed. Every snapshot recomputes, per company:
  median_days_to_close  median(delisted_at − posted_at) over postings closed in the last 180 days
                        (only rows with a real posted_at — first_seen_at understates age on seed scans)
  hiring_velocity       new reqs per week over the last 4 weeks
  repost_rate           share of this company's postings that are reposts of an earlier req
and stores one row per company (plus a market-wide row) in velocity_snapshots. The scorer's urgency
reads companies.median_days_to_close, so after a snapshot the "est. days to close" is measured, not
the 45-day default.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta
from typing import Any

from radar import db
from radar.util import utcnow, utcnow_iso

MIN_CLOSED_FOR_MEDIAN = 5
LOOKBACK_DAYS = 180


def week_start(d: date | None = None) -> date:
    d = d or utcnow().date()
    return d - timedelta(days=d.weekday())


def learn_time_to_close(conn: sqlite3.Connection) -> dict[str, Any]:
    since = (utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.all_rows(
        conn,
        "SELECT company_id, (julianday(delisted_at) - julianday(posted_at)) AS days FROM postings "
        "WHERE company_id IS NOT NULL AND delisted_at IS NOT NULL AND delisted_at >= ? AND posted_at IS NOT NULL "
        "AND julianday(delisted_at) - julianday(posted_at) BETWEEN 0 AND 400",
        (since,),
    )
    by: dict[int, list[float]] = {}
    for r in rows:
        by.setdefault(int(r["company_id"]), []).append(float(r["days"]))
    updated = 0
    with db.transaction(conn):
        for cid, days in by.items():
            if len(days) >= MIN_CLOSED_FOR_MEDIAN:
                conn.execute(
                    "UPDATE companies SET median_days_to_close = ?, updated_at = ? WHERE id = ?",
                    (round(statistics.median(days), 1), utcnow_iso(), cid),
                )
                updated += 1
    all_days = [d for v in by.values() for d in v]
    market = (
        round(statistics.median(all_days), 1) if len(all_days) >= MIN_CLOSED_FOR_MEDIAN else None
    )
    return {
        "companies_with_learned_ttc": updated,
        "closed_samples": len(all_days),
        "market_median_days": market,
    }


def take_snapshot(conn: sqlite3.Connection, *, when: date | None = None) -> dict[str, Any]:
    ws = week_start(when)
    ws_iso = ws.isoformat()
    prev_iso = (ws - timedelta(days=7)).isoformat()
    four_weeks = (ws - timedelta(days=28)).isoformat()
    learned = learn_time_to_close(conn)
    per_company = db.all_rows(
        conn,
        "SELECT c.id AS company_id, c.name, c.median_days_to_close, "
        "SUM(CASE WHEN p.delisted_at IS NULL THEN 1 ELSE 0 END) AS open_reqs, "
        "SUM(CASE WHEN p.delisted_at IS NULL AND p.in_scope = 1 THEN 1 ELSE 0 END) AS in_scope_open, "
        "SUM(CASE WHEN COALESCE(p.posted_at, p.first_seen_at) >= ? THEN 1 ELSE 0 END) AS new_reqs, "
        "SUM(CASE WHEN p.delisted_at >= ? THEN 1 ELSE 0 END) AS closed_reqs, "
        "SUM(CASE WHEN COALESCE(p.posted_at, p.first_seen_at) >= ? THEN 1 ELSE 0 END) / 4.0 AS velocity, "
        "AVG(CASE WHEN p.repost_of_id IS NOT NULL THEN 1.0 ELSE 0.0 END) AS repost_rate "
        "FROM companies c JOIN postings p ON p.company_id = c.id AND p.is_cluster_canonical = 1 GROUP BY c.id",
        (prev_iso, prev_iso, four_weeks),
    )
    now = utcnow_iso()
    with db.transaction(conn):
        for r in per_company:
            conn.execute(
                "INSERT INTO velocity_snapshots (week_start, company_id, company_name, open_reqs, new_reqs, closed_reqs, in_scope_open, median_days_to_close, repost_rate, taken_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(week_start, company_id) DO UPDATE SET open_reqs=excluded.open_reqs, new_reqs=excluded.new_reqs, "
                "closed_reqs=excluded.closed_reqs, in_scope_open=excluded.in_scope_open, median_days_to_close=excluded.median_days_to_close, repost_rate=excluded.repost_rate, taken_at=excluded.taken_at",
                (
                    ws_iso,
                    r["company_id"],
                    r["name"],
                    r["open_reqs"],
                    r["new_reqs"],
                    r["closed_reqs"],
                    r["in_scope_open"],
                    r["median_days_to_close"],
                    round(r["repost_rate"] or 0, 3),
                    now,
                ),
            )
            conn.execute(
                "UPDATE companies SET hiring_velocity = ?, repost_rate = ?, updated_at = ? WHERE id = ?",
                (
                    round(r["velocity"] or 0, 2),
                    round(r["repost_rate"] or 0, 3),
                    now,
                    r["company_id"],
                ),
            )
        market = db.one(
            conn,
            "SELECT SUM(CASE WHEN delisted_at IS NULL THEN 1 ELSE 0 END) AS open_reqs, "
            "SUM(CASE WHEN delisted_at IS NULL AND in_scope = 1 THEN 1 ELSE 0 END) AS in_scope_open, "
            "SUM(CASE WHEN COALESCE(posted_at, first_seen_at) >= ? THEN 1 ELSE 0 END) AS new_reqs, "
            "SUM(CASE WHEN delisted_at >= ? THEN 1 ELSE 0 END) AS closed_reqs FROM postings WHERE is_cluster_canonical = 1",
            (prev_iso, prev_iso),
        )
        conn.execute(
            "INSERT INTO velocity_snapshots (week_start, company_id, company_name, open_reqs, new_reqs, closed_reqs, in_scope_open, median_days_to_close, taken_at) "
            "VALUES (?,NULL,'__market__',?,?,?,?,?,?) ON CONFLICT(week_start, company_id) DO UPDATE SET open_reqs=excluded.open_reqs, new_reqs=excluded.new_reqs, "
            "closed_reqs=excluded.closed_reqs, in_scope_open=excluded.in_scope_open, median_days_to_close=excluded.median_days_to_close, taken_at=excluded.taken_at",
            (
                ws_iso,
                market["open_reqs"],
                market["new_reqs"],
                market["closed_reqs"],
                market["in_scope_open"],
                learned["market_median_days"],
                now,
            ),
        )
    db.kv_set(conn, "last_snapshot_at", now)
    return {"week_start": ws_iso, "companies": len(per_company), **learned, "market": dict(market)}


def season_tracker(
    conn: sqlite3.Connection, *, season_start: date, season_end: date
) -> dict[str, Any]:
    """Where are we in the recruiting season, and how much of the in-scope supply has already posted?"""
    today = utcnow().date()
    elapsed = max(
        0.0, min(1.0, (today - season_start).days / max(1, (season_end - season_start).days))
    )
    rows = db.all_rows(
        conn,
        "SELECT week_start, new_reqs, closed_reqs, in_scope_open FROM velocity_snapshots WHERE company_id IS NULL ORDER BY week_start",
    )
    return {
        "season_elapsed": round(elapsed, 2),
        "weeks": [dict(r) for r in rows][-12:],
        "note": "share of the season elapsed by calendar; the weekly rows show whether supply is still rising",
    }
