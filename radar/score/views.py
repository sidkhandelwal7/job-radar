"""Default-view suppression rules (§3.2 "filters are views, never deletions").

One list, used by the scorer (to stamp in_default_view / suppressed_reason), the CLI, and the API.
Order matters: the first matching rule is the reason shown.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from radar import db
from radar.util import utcnow_iso

SUPPRESSIONS: list[tuple[str, str]] = [
    ("duplicate", "duplicate listing (another row is canonical)"),
    ("delisted", "delisted at source"),
    ("out_of_scope", "out of scope (seniority / role family / hard blocker)"),
    ("floor", "failed the comp floor"),
    ("dismissed", "dismissed by you"),
    ("applied", "already applied"),
    ("snoozed", "snoozed"),
]
LABELS = dict(SUPPRESSIONS)


def suppression_reason(p: dict[str, Any], now: str | None = None) -> str | None:
    """Return the first suppression key that applies to a posting row (dict), else None."""
    now = now or utcnow_iso()
    if not p.get("is_cluster_canonical", 1):
        return "duplicate"
    if p.get("delisted_at"):
        return "delisted"
    if p.get("in_scope") == 0:
        return "out_of_scope"
    if p.get("is_stretch"):
        return "stretch"
    if p.get("floor_result") == "fail":
        return "floor"
    if p.get("status") == "dismissed":
        return "dismissed"
    if p.get("status") == "applied":
        return "applied"
    if p.get("snooze_until") and p["snooze_until"] > now:
        return "snoozed"
    return None


def stamp_view_flags(conn: sqlite3.Connection, ids: list[int] | None = None) -> int:
    """Recompute in_default_view / suppressed_reason in SQL for all rows (or the given ids)."""
    now = utcnow_iso()
    where = ""
    params: list[Any] = [now]
    if ids:
        where = f" WHERE id IN ({','.join('?' * len(ids))})"
        params += list(ids)
    sql = (
        "UPDATE postings SET suppressed_reason = CASE "
        "WHEN is_cluster_canonical = 0 THEN 'duplicate' "
        "WHEN delisted_at IS NOT NULL THEN 'delisted' "
        "WHEN in_scope = 0 THEN 'out_of_scope' "
        "WHEN floor_result = 'fail' THEN 'floor' "
        "WHEN status = 'dismissed' THEN 'dismissed' "
        "WHEN status = 'applied' THEN 'applied' "
        "WHEN snooze_until IS NOT NULL AND snooze_until > ? THEN 'snoozed' "
        "ELSE NULL END" + where
    )
    with db.transaction(conn):
        conn.execute(sql, params)
        conn.execute(
            "UPDATE postings SET in_default_view = CASE WHEN suppressed_reason IS NULL THEN 1 ELSE 0 END"
            + (where.replace("WHERE", "WHERE") if ids else ""),
            params[1:] if ids else [],
        )
    return len(ids) if ids else int(db.scalar(conn, "SELECT COUNT(*) FROM postings") or 0)
