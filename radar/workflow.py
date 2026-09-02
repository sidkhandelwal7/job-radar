"""Operator workflow actions — the ONE implementation used by the CLI, the API, and Telegram buttons.
Never called by fetch/score; these columns are the operator's."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from radar import db
from radar.util import to_iso, utcnow, utcnow_iso


class ActionResult(dict):
    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


def apply_action(
    conn: sqlite3.Connection,
    pid: int,
    action: str,
    *,
    reason: str | None = None,
    days: int | None = None,
    contact: str | None = None,
    text: str | None = None,
    force: bool = False,
    tags: list[str] | None = None,
    via: str = "cli",
) -> ActionResult:
    from radar.applications import DuplicateApplication, mark_applied
    from radar.score.views import stamp_view_flags

    now = utcnow_iso()
    if not db.one(conn, "SELECT id FROM postings WHERE id = ?", (pid,)):
        return ActionResult(ok=False, error="posting not found")
    if action == "applied":
        try:
            app_id = mark_applied(
                conn,
                pid,
                referral_used=bool(contact),
                referral_contact=contact,
                notes=text,
                force=force,
                source_channel=via,
            )
        except DuplicateApplication as e:
            return ActionResult(
                ok=False,
                duplicate={
                    "reason": e.reason,
                    "applications": [
                        {
                            "id": x["id"],
                            "company_name": x["company_name"],
                            "title": x["title"],
                            "stage": x["stage"],
                        }
                        for x in e.existing
                    ],
                },
            )
        _engaged(conn, pid, "applied")
        return ActionResult(ok=True, application_id=app_id)
    with db.transaction(conn):
        if action == "dismiss":
            db.update(
                conn,
                "postings",
                pid,
                {
                    "status": "dismissed",
                    "dismiss_reason": reason or "no reason given",
                    "status_changed_at": now,
                    "priority": 0,
                    "apply_priority_rank": None,
                },
            )
            db.add_event(
                conn, pid, "status_changed", {"to": "dismissed", "reason": reason, "via": via}
            )
        elif action == "shortlist":
            db.update(
                conn,
                "postings",
                pid,
                {"status": "shortlisted", "starred": 1, "status_changed_at": now},
            )
            db.add_event(conn, pid, "status_changed", {"to": "shortlisted", "via": via})
        elif action == "unshortlist":
            db.update(
                conn, "postings", pid, {"status": "new", "starred": 0, "status_changed_at": now}
            )
            db.add_event(conn, pid, "status_changed", {"to": "new", "via": via})
        elif action == "snooze":
            until = to_iso(utcnow() + timedelta(days=days or 7))
            db.update(
                conn,
                "postings",
                pid,
                {
                    "status": "snoozed",
                    "snooze_until": until,
                    "status_changed_at": now,
                    "priority": 0,
                    "apply_priority_rank": None,
                },
            )
            db.add_event(conn, pid, "status_changed", {"to": "snoozed", "until": until, "via": via})
        elif action == "unsnooze":
            db.update(
                conn,
                "postings",
                pid,
                {"status": "new", "snooze_until": None, "status_changed_at": now},
            )
        elif action == "referral":
            db.update(conn, "postings", pid, {"referral_secured": 1})
            db.add_event(conn, pid, "referral_logged", {"contact": contact, "via": via})
        elif action == "note":
            r = db.one(conn, "SELECT notes_md FROM postings WHERE id = ?", (pid,))
            db.update(
                conn, "postings", pid, {"notes_md": text if text is not None else r["notes_md"]}
            )
            db.add_event(conn, pid, "note", {"text": (text or "")[:200]})
        elif action == "tag":
            db.update(conn, "postings", pid, {"tags_user_json": json.dumps(tags or [])})
        elif action == "override_floor":
            db.update(conn, "postings", pid, {"override_floor": 1})
            db.add_event(conn, pid, "note", {"text": "floor override set", "via": via})
        elif action == "restore_link":
            # "still live, restore": you opened it and it is open. Recorded as a manual verdict so the
            # next sweep can still overrule it with evidence; needs_rescore puts it back in its bucket.
            db.update(
                conn,
                "postings",
                pid,
                {
                    "url_status": "live",
                    "url_verify_method": "manual",
                    "url_last_verified_at": now,
                    "needs_rescore": 1,
                },
            )
            db.add_event(
                conn, pid, "link_restored", {"via": via, "note": text or "marked live by hand"}
            )
        else:
            return ActionResult(ok=False, error=f"unknown action {action}")
    stamp_view_flags(conn, [pid])
    if action in ("shortlist", "dismiss", "snooze"):
        _engaged(conn, pid, action)
    return ActionResult(ok=True)


def _engaged(conn: sqlite3.Connection, pid: int, engagement: str) -> None:
    try:
        from radar.notify.engine import record_engagement

        record_engagement(conn, pid, engagement)
    except Exception:
        pass
