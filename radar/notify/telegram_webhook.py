"""Telegram webhook mode (Cloudflare Worker queue) with long-polling fallback — ONE consumer.

Telegram delivers updates to exactly one consumer: while a webhook is set, getUpdates answers 409.
So the laptop arbitrates every cycle (`reconcile`):
  Worker healthy  → make sure the webhook is set to it → mode = webhook; the listener idles
  Worker down     → delete the webhook → mode = polling; the listener long-polls
The decision is persisted in kv `telegram_mode`, which the listener reads each round. Taps queued
by the Worker are drained here (`drain_webhook`) and applied through the same code path as a
dashboard click, then acknowledged (deleted from KV). Secrets: TELEGRAM_WEBHOOK_URL,
TELEGRAM_WEBHOOK_SECRET (what Telegram echoes in X-Telegram-Bot-Api-Secret-Token), TELEGRAM_DRAIN_TOKEN.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

import httpx

from radar import db
from radar.notify.channels import Telegram

log = logging.getLogger("radar.telegram")
MODE_KEY = "telegram_mode"  # webhook | polling


def configured() -> bool:
    return bool(
        os.environ.get("TELEGRAM_WEBHOOK_URL")
        and os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        and os.environ.get("TELEGRAM_DRAIN_TOKEN")
    )


def _worker() -> str:
    return os.environ["TELEGRAM_WEBHOOK_URL"].rstrip("/")


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['TELEGRAM_DRAIN_TOKEN']}"}


def worker_healthy(timeout: float = 8.0) -> bool:
    try:
        r = httpx.get(f"{_worker()}/health", timeout=timeout)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception as e:
        log.info("telegram webhook worker unreachable: %s", type(e).__name__)
        return False


def webhook_info(tg: Telegram) -> dict[str, Any]:
    r = httpx.get(f"{tg.base}/getWebhookInfo", timeout=15)
    r.raise_for_status()
    return r.json().get("result", {})


def set_webhook(tg: Telegram) -> bool:
    r = httpx.post(
        f"{tg.base}/setWebhook",
        json={
            "url": f"{_worker()}/telegram",
            "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"],
            "allowed_updates": ["callback_query", "message"],
            "drop_pending_updates": False,
        },
        timeout=15,
    )
    ok = r.status_code == 200 and r.json().get("ok")
    if not ok:
        log.warning("setWebhook failed: %s %s", r.status_code, r.text[:120])
    return bool(ok)


def delete_webhook(tg: Telegram) -> bool:
    r = httpx.post(f"{tg.base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=15)
    return r.status_code == 200 and bool(r.json().get("ok"))


def current_mode(conn: sqlite3.Connection) -> str:
    return db.kv_get(conn, MODE_KEY) or "polling"


def reconcile(conn: sqlite3.Connection, tg: Telegram | None = None) -> dict[str, Any]:
    """Decide the single consumer for this moment and make Telegram agree. Idempotent, cheap
    (one health GET, one getWebhookInfo), safe to run every cycle."""
    tg = tg or Telegram()
    if not tg.token:
        return {"mode": "off", "note": "no bot token"}
    if not configured():
        if current_mode(conn) != "polling":
            delete_webhook(tg)
            db.kv_set(conn, MODE_KEY, "polling")
        return {"mode": "polling", "note": "webhook not configured"}
    want = "webhook" if worker_healthy() else "polling"
    info = webhook_info(tg)
    have_url = info.get("url") or ""
    ours = f"{_worker()}/telegram"
    changed = False
    if want == "webhook" and have_url != ours:
        changed = set_webhook(tg)
    elif want == "polling" and have_url:
        changed = delete_webhook(tg)
    db.kv_set(conn, MODE_KEY, want)
    return {
        "mode": want,
        "changed": changed,
        "pending_at_telegram": info.get("pending_update_count", 0),
        "last_error": info.get("last_error_message"),
    }


def drain_webhook(conn: sqlite3.Connection, tg: Telegram | None = None) -> dict[str, Any]:
    """Pull queued taps from the Worker, apply them (same code as a live tap), acknowledge."""
    from radar.notify.telegram_actions import apply_updates

    tg = tg or Telegram()
    try:
        r = httpx.get(f"{_worker()}/drain", headers=_auth(), timeout=20)
        r.raise_for_status()
    except Exception as e:
        return {"applied": 0, "error": f"{type(e).__name__}: {e}"}
    items = r.json().get("items", [])
    updates = [it["update"] for it in items if isinstance(it.get("update"), dict)]
    st = apply_updates(conn, tg, updates, advance_offset=False)
    keys = [it["key"] for it in items]
    if keys:
        try:
            httpx.post(f"{_worker()}/ack", headers=_auth(), json={"keys": keys}, timeout=20)
        except Exception as e:
            log.warning("webhook ack failed (taps will be re-applied idempotently): %s", e)
    return {**st, "queued": len(items)}
