"""Webhook mode: one consumer at a time (Telegram 409s a second getUpdates consumer), the laptop
drains the Worker's KV queue and applies taps idempotently, and the Actions backstop's buttons
(`actk:<action>:<hash>`) resolve to posting rows through actions/notify_state.json."""

import hashlib
import json

import respx
from httpx import Response

from radar import db
from radar.notify import telegram_webhook as tw
from radar.notify.channels import Telegram
from radar.notify.telegram_actions import apply_updates, parse_callback
from radar.util import utcnow_iso

WORKER = "https://job-radar-telegram.example.workers.dev"
API = "https://api.telegram.org/bot123:abc"


def _env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", WORKER)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("TELEGRAM_DRAIN_TOKEN", "dr41n")


def _post(conn, jid):
    now = utcnow_iso()
    return db.insert_posting(
        conn,
        {
            "source": "company_direct",
            "source_provider": "greenhouse",
            "source_slug": "x",
            "source_job_id": jid,
            "apply_url": f"https://x/{jid}",
            "first_seen_at": now,
            "last_seen_at": now,
            "company_name": "Acme",
            "title": "SWE",
            "in_scope": 1,
            "floor_result": "pass",
            "is_cluster_canonical": 1,
            "priority": 0.5,
            "queue_action": "apply_today",
            "status": "new",
        },
    )


@respx.mock
def test_reconcile_sets_webhook_when_worker_healthy_and_falls_back_when_not(
    conn, tmp_project, monkeypatch
):
    _env(monkeypatch)
    health = respx.get(f"{WORKER}/health").mock(return_value=Response(200, json={"ok": True}))
    respx.get(f"{API}/getWebhookInfo").mock(
        return_value=Response(
            200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}}
        )
    )
    setw = respx.post(f"{API}/setWebhook").mock(return_value=Response(200, json={"ok": True}))
    delw = respx.post(f"{API}/deleteWebhook").mock(return_value=Response(200, json={"ok": True}))
    r = tw.reconcile(conn, Telegram())
    assert r["mode"] == "webhook" and setw.called
    body = json.loads(setw.calls[0].request.content)
    assert body["url"] == f"{WORKER}/telegram" and body["secret_token"] == "s3cret"
    assert db.kv_get(conn, "telegram_mode") == "webhook"
    # Worker goes down → webhook deleted, polling resumes
    health.mock(return_value=Response(503))
    respx.get(f"{API}/getWebhookInfo").mock(
        return_value=Response(200, json={"ok": True, "result": {"url": f"{WORKER}/telegram"}})
    )
    r2 = tw.reconcile(conn, Telegram())
    assert r2["mode"] == "polling" and delw.called
    assert db.kv_get(conn, "telegram_mode") == "polling"


@respx.mock
def test_reconcile_without_webhook_config_is_polling(conn, tmp_project, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    for k in ("TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_DRAIN_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert tw.reconcile(conn, Telegram())["mode"] == "polling"


@respx.mock
def test_drain_webhook_applies_and_acks_once(conn, tmp_project, monkeypatch):
    _env(monkeypatch)
    pid = _post(conn, "w1")
    item = {
        "key": "tap:000000000077",
        "received_at": utcnow_iso(),
        "update": {
            "update_id": 77,
            "callback_query": {
                "id": "cb77",
                "data": f"act:shortlist:{pid}",
                "message": {"message_id": 5, "chat": {"id": 42}},
            },
        },
    }
    respx.get(f"{WORKER}/drain").mock(
        return_value=Response(200, json={"items": [item], "more": False})
    )
    ack = respx.post(f"{WORKER}/ack").mock(return_value=Response(200, json={"deleted": 1}))
    respx.post(f"{API}/answerCallbackQuery").mock(return_value=Response(200, json={"ok": True}))
    respx.post(f"{API}/editMessageReplyMarkup").mock(return_value=Response(200, json={"ok": True}))
    st = tw.drain_webhook(conn, Telegram())
    assert st["applied"] == 1 and st["queued"] == 1
    assert json.loads(ack.calls[0].request.content)["keys"] == ["tap:000000000077"]
    assert ack.calls[0].request.headers["authorization"] == "Bearer dr41n"
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (pid,)) == "shortlisted"
    # the same item again (ack lost) applies nothing twice
    st2 = tw.drain_webhook(conn, Telegram())
    assert st2["applied"] == 0
    assert (
        db.kv_get(conn, "telegram_update_offset") is None
    )  # webhook drains never touch the polling offset


def test_actions_callback_hash_resolves_to_the_posting(conn, tmp_project, monkeypatch):
    from radar import actions_runner

    pid = _post(conn, "gh-900")
    key = "greenhouse:x:gh-900"
    h = hashlib.sha1(key.encode()).hexdigest()[:12]
    adir = tmp_project.data_dir / "actions"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "notify_state.json").write_text(json.dumps({"keys": {h: "greenhouse|x|gh-900"}}))
    monkeypatch.setattr(actions_runner, "ACTIONS_DIR", adir)
    assert parse_callback(f"actk:applied:{h}", conn=conn) == ("applied", pid)
    assert parse_callback("actk:applied:deadbeef0000", conn=conn) is None
    assert parse_callback(f"act:dismiss:{pid}") == ("dismiss", pid)

    class Quiet(Telegram):
        def __init__(self):
            self.token, self.chat_id, self.base = "t", "c", "https://fake"

        def answer_callback(self, *a):
            pass

        def edit_markup_done(self, *a):
            pass

    st = apply_updates(
        conn,
        Quiet(),
        [
            {
                "update_id": 900,
                "callback_query": {
                    "id": "c",
                    "data": f"actk:applied:{h}",
                    "message": {"message_id": 1, "chat": {"id": 1}},
                },
            }
        ],
        advance_offset=False,
    )
    assert st["applied"] == 1
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (pid,)) == "applied"
    assert db.scalar(conn, "SELECT COUNT(*) FROM applications WHERE posting_id = ?", (pid,)) == 1
