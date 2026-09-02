"""Telegram inline buttons → workflow actions. Callback data: `act:<action>:<posting_id>`.

Laptop: `radar notify --poll` (or every `radar cycle`) drains getUpdates and applies actions.
Actions runner: drains too, but has no DB — it appends the callbacks to actions/actions.jsonl and
the laptop applies them on `radar ingest-deltas`. Telegram keeps undelivered updates ~24 h, so a
tap from your phone takes effect within one Actions cycle even when the laptop sleeps (PLAN §2.5).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from radar import db
from radar.notify.channels import Telegram, chat_from_update
from radar.util import utcnow_iso

log = logging.getLogger("radar.telegram")

LABELS = {
    "shortlist": "☆ shortlisted",
    "applied": "✓ marked applied",
    "dismiss": "✕ dismissed",
    "snooze": "zz snoozed 7 days",
}


def parse_callback(data: str, *, conn: sqlite3.Connection | None = None) -> tuple[str, int] | None:
    """`act:<action>:<posting_id>` from the laptop, or `actk:<action>:<key_hash>` from the Actions
    backstop (which has no posting ids); the hash is resolved through actions/notify_state.json →
    natural key → posting row."""
    parts = (data or "").split(":")
    if len(parts) == 3 and parts[0] == "act" and parts[2].isdigit():
        return parts[1], int(parts[2])
    if len(parts) == 3 and parts[0] == "actk" and conn is not None:
        pid = resolve_key_hash(conn, parts[2])
        if pid is not None:
            return parts[1], pid
    return None


def resolve_key_hash(conn: sqlite3.Connection, h: str) -> int | None:
    from radar.actions_runner import ACTIONS_DIR

    state_path = ACTIONS_DIR / "notify_state.json"
    if not state_path.exists():
        return None
    try:
        keys = json.loads(state_path.read_text()).get("keys", {})
    except (OSError, json.JSONDecodeError):
        return None
    key = keys.get(h)
    if not key:
        return None
    provider, slug, job_id = key.split("|", 2)
    row = db.one(
        conn,
        "SELECT id FROM postings WHERE source_provider = ? AND source_slug = ? AND source_job_id = ?",
        (provider, slug, job_id),
    )
    return row["id"] if row else None


def drain_and_apply(
    conn: sqlite3.Connection, tg: Telegram | None = None, *, long_poll_s: int = 0
) -> dict[str, Any]:
    """Laptop side, polling mode: fetch from getUpdates and apply. long_poll_s > 0 blocks until a
    tap arrives (the `radar telegram-listen` agent); 0 drains whatever is queued."""
    tg = tg or Telegram()
    if not tg.available():
        return {"applied": 0, "note": "telegram not configured"}
    offset = db.kv_get(conn, "telegram_update_offset")
    # getUpdates is destructive on read (an offset confirms everything before it), so ask for
    # messages too and persist a chat id the instant one is seen — nothing is ever lost to a drain.
    updates = tg.get_updates(offset, timeout=long_poll_s, allowed=("callback_query", "message"))
    return apply_updates(conn, tg, updates, advance_offset=True)


def apply_updates(
    conn: sqlite3.Connection, tg: Telegram, updates: list[dict[str, Any]], *, advance_offset: bool
) -> dict[str, Any]:
    """Apply a batch of Telegram updates from ANY source (getUpdates, the webhook queue, or the
    Actions file) exactly once each — telegram_updates is keyed by update_id."""
    from radar.workflow import apply_action

    applied = 0
    last_id = db.kv_get(conn, "telegram_update_offset") if advance_offset else None
    for u in updates:
        if advance_offset:
            last_id = u["update_id"] + 1
        seen = chat_from_update(u)
        if seen:
            _remember_chat(seen)
        cq = u.get("callback_query")
        if not cq:
            continue
        if db.one(conn, "SELECT 1 FROM telegram_updates WHERE update_id = ?", (u["update_id"],)):
            continue
        db.insert(
            conn,
            "telegram_updates",
            {
                "update_id": u["update_id"],
                "received_at": utcnow_iso(),
                "payload_json": json.dumps(cq),
                "applied": 0,
            },
        )
        parsed = parse_callback(cq.get("data", ""), conn=conn)
        if not parsed:
            tg.answer_callback(cq["id"], "noted")
            continue
        action, pid = parsed
        res = apply_action(
            conn,
            pid,
            action,
            reason="dismissed from Telegram" if action == "dismiss" else None,
            days=7,
            via="telegram",
        )
        if res.ok and action in ("applied", "dismiss", "shortlist", "snooze"):
            try:  # re-bucket immediately so the dashboard/queue reflect the tap without waiting a cycle
                from radar.config import get_config
                from radar.score.engine import score_all

                score_all(conn, get_config(), ids=[pid])
            except Exception:
                log.exception("rescore after telegram action failed")
        msg = (
            LABELS.get(action, action)
            if res.ok
            else (
                f"duplicate: {res['duplicate']['reason']}"
                if res.get("duplicate")
                else res.get("error", "failed")
            )
        )
        tg.answer_callback(cq["id"], msg)  # harmless if the Worker already acknowledged
        m = cq.get("message") or {}
        if res.ok and m.get("message_id"):
            tg.edit_markup_done(m["chat"]["id"], m["message_id"], msg)
        conn.execute(
            "UPDATE telegram_updates SET applied = ? WHERE update_id = ?",
            (int(res.ok), u["update_id"]),
        )
        applied += int(res.ok)
    if advance_offset and last_id is not None:
        db.kv_set(conn, "telegram_update_offset", last_id)
    return {"updates": len(updates), "applied": applied}


def drain_to_file(actions_dir: Path, state: dict[str, Any], tg: Telegram | None = None) -> int:
    """Actions-runner side: no DB. Append every callback update verbatim to actions/actions.jsonl
    (the laptop applies them through apply_updates, which understands both `act:` and `actk:`
    data and dedupes by update_id); acknowledge on Telegram so the tap doesn't spin."""
    tg = tg or Telegram()
    if not tg.available():
        return 0
    offset = state.get("telegram_update_offset")
    updates = tg.get_updates(offset, allowed=("callback_query", "message"))
    n = 0
    path = actions_dir / "actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for u in updates:
            state["telegram_update_offset"] = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq:
                continue
            f.write(json.dumps({"at": utcnow_iso(), "update": u}, separators=(",", ":")) + "\n")
            tg.answer_callback(cq["id"], "queued — applied when the laptop wakes")
            n += 1
    return n


def _remember_chat(found: tuple[str, str]) -> None:
    """Persist TELEGRAM_CHAT_ID immediately if it is not set yet (idempotent; never overwrites)."""
    import os

    chat_id, who = found
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return
    try:
        from radar.secrets import set_secret

        set_secret("TELEGRAM_CHAT_ID", chat_id)
        log.info("telegram: chat id captured from a message by @%s and stored", who)
    except Exception:
        log.exception("telegram: could not persist chat id")


def listener_owns_queue(cfg) -> bool:
    """True when `radar telegram-listen` is alive. Telegram allows ONE getUpdates consumer per bot
    (a second one gets 409 and the first is terminated), and the queue is destructive on read, so
    scheduled drains step aside while the listener runs."""
    from radar.ops.launchd import single_instance

    with single_instance(cfg, "telegram-listen") as mine:
        return not mine


def listen(conn: sqlite3.Connection, *, long_poll_s: int = 50, once: bool = False) -> None:
    """Long-poll loop for `radar telegram-listen` (launchd KeepAlive). One open HTTPS request at a
    time, ~0 CPU while idle; a tap is applied within a second. On wake the loop just continues —
    Telegram keeps undelivered updates for 24 h and the offset is persisted, so nothing is lost;
    taps made while the laptop slept were also drained by the Actions backstop into
    actions/actions.jsonl and are applied by the next cycle, whichever path sees them first
    (telegram_updates is keyed by update_id, so a tap is applied once)."""

    from radar.config import get_config
    from radar.ops.launchd import single_instance

    tg = Telegram()
    if not tg.token:
        log.error("telegram-listen: TELEGRAM_BOT_TOKEN not configured")
        return
    if not tg.chat_id:
        tg.chat_id = "pending"  # keep polling: your first message sets the chat id (_remember_chat)
    with single_instance(get_config(), "telegram-listen") as mine:
        if not mine:
            log.error("telegram-listen: another listener is already running")
            return
        _loop(conn, tg, long_poll_s, once)


def _loop(conn: sqlite3.Connection, tg: Telegram, long_poll_s: int, once: bool) -> None:
    import time

    backoff = 2.0
    while True:
        if (db.kv_get(conn, "telegram_mode") or "polling") == "webhook":
            # the Worker is the consumer right now (getUpdates would 409); check back shortly
            if once:
                return
            time.sleep(30)
            continue
        try:
            st = drain_and_apply(conn, tg, long_poll_s=long_poll_s)
            if st.get("applied"):
                log.info("telegram-listen: applied %s action(s)", st["applied"])
            backoff = 2.0
        except Exception as e:  # network blips, sleep/wake: back off, never die
            log.warning("telegram-listen: %s: %s — retrying in %.0fs", type(e).__name__, e, backoff)
            time.sleep(backoff)
            backoff = min(120.0, backoff * 2)
        if once:
            return
