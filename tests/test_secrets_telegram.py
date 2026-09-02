"""Secrets file semantics and the Telegram callback path with a fake transport."""

import os
import stat

import pytest

from radar import db
from radar.notify.channels import Telegram
from radar.notify.telegram_actions import drain_and_apply, listen
from radar.secrets import list_keys, load_secrets, set_secret, unset_secret
from radar.util import utcnow_iso


def test_secrets_file_is_0600_and_never_in_git(tmp_project, monkeypatch):
    path = tmp_project.data_dir / "secrets.env"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    set_secret("TELEGRAM_BOT_TOKEN", "123:abc", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list_keys(path) == ["TELEGRAM_BOT_TOKEN"]
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:abc"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert (
        load_secrets(path) == ["TELEGRAM_BOT_TOKEN"]
        and os.environ["TELEGRAM_BOT_TOKEN"] == "123:abc"
    )
    # a loose file is refused, not silently used
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_secrets(path)
    path.chmod(0o600)
    assert unset_secret("TELEGRAM_BOT_TOKEN", path) and "TELEGRAM_BOT_TOKEN" not in os.environ
    # data/ is git-ignored, so the file can never be committed
    assert "data/" in (tmp_project.root / ".gitignore").read_text().splitlines()


class FakeTG(Telegram):
    def __init__(self, updates):
        self.token, self.chat_id, self.base = "t", "c", "https://fake"
        self._updates = updates
        self.answers, self.edits = [], []

    def available(self):
        return True

    def get_updates(self, offset, *, timeout=0, allowed=("callback_query",)):
        out = [u for u in self._updates if offset is None or u["update_id"] >= offset]
        return out

    def answer_callback(self, callback_id, text):
        self.answers.append((callback_id, text))

    def edit_markup_done(self, chat_id, message_id, note):
        self.edits.append((message_id, note))


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


def test_inline_buttons_mutate_state_exactly_once(conn, tmp_project):
    a, b, c = _post(conn, "1"), _post(conn, "2"), _post(conn, "3")

    def cq(uid, data):
        return {
            "update_id": uid,
            "callback_query": {
                "id": f"cb{uid}",
                "data": data,
                "message": {"message_id": uid, "chat": {"id": 1}},
            },
        }

    tg = FakeTG(
        [
            cq(10, f"act:shortlist:{a}"),
            cq(11, f"act:applied:{b}"),
            cq(12, f"act:dismiss:{c}"),
            cq(13, "noop"),
        ]
    )
    st = drain_and_apply(conn, tg)
    assert st["applied"] == 3
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (a,)) == "shortlisted"
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (b,)) == "applied"
    assert db.scalar(conn, "SELECT COUNT(*) FROM applications WHERE posting_id = ?", (b,)) == 1
    row = db.one(conn, "SELECT status, dismiss_reason FROM postings WHERE id = ?", (c,))
    assert row["status"] == "dismissed" and "Telegram" in row["dismiss_reason"]
    assert len(tg.answers) == 4 and len(tg.edits) == 3
    assert db.kv_get(conn, "telegram_update_offset") == 14
    # the same updates arriving again (Actions drained them too, or a replayed poll) apply nothing twice
    st2 = drain_and_apply(conn, FakeTG(tg._updates))
    assert st2["applied"] == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM applications WHERE posting_id = ?", (b,)) == 1
    # the listener loop is the same function under long-poll (one round, then exit)
    d = _post(conn, "4")
    tg3 = FakeTG([cq(20, f"act:shortlist:{d}")])
    import radar.notify.telegram_actions as ta

    orig = ta.Telegram
    ta.Telegram = lambda: tg3  # type: ignore[assignment]
    try:
        listen(conn, once=True)
    finally:
        ta.Telegram = orig
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (d,)) == "shortlisted"


def test_drain_persists_chat_id_from_any_message(conn, tmp_project, monkeypatch):
    """The queue is destructive on read: whichever path reads a message from you stores the chat id
    immediately, so a drain can never lose it."""
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    path = tmp_project.data_dir / "secrets.env"
    monkeypatch.setattr("radar.secrets.secrets_path", lambda: path)
    tg = FakeTG(
        [
            {
                "update_id": 30,
                "message": {
                    "chat": {"id": 4242, "type": "private", "username": "operator"},
                    "text": "hi",
                },
            }
        ]
    )
    tg.chat_id = "pending"
    drain_and_apply(conn, tg)
    assert os.environ.get("TELEGRAM_CHAT_ID") == "4242"
    assert (
        "TELEGRAM_CHAT_ID=4242" in path.read_text() and stat.S_IMODE(path.stat().st_mode) == 0o600
    )
    # never overwritten by a later message from someone else
    drain_and_apply(
        conn,
        FakeTG([{"update_id": 31, "message": {"chat": {"id": 9, "type": "private"}, "text": "x"}}]),
    )
    assert os.environ["TELEGRAM_CHAT_ID"] == "4242"


def test_scheduled_drain_steps_aside_for_the_listener(tmp_project):
    from radar.notify.telegram_actions import listener_owns_queue
    from radar.ops.launchd import single_instance

    assert listener_owns_queue(tmp_project) is False
    with single_instance(tmp_project, "telegram-listen"):
        assert listener_owns_queue(tmp_project) is True
