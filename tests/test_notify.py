"""§20 notification engine: triggers, anti-noise gates, engagement, digests, Telegram callbacks."""

import json
from datetime import UTC, datetime

from radar import db
from radar.notify.channels import Channel, Payload
from radar.notify.digest import daily_digest, ics_feed, weekly_digest
from radar.notify.engine import (
    in_quiet_hours,
    mark_ignored_and_escalate,
    precision_report,
    record_engagement,
    send_alerts,
)
from radar.notify.telegram_actions import parse_callback
from radar.util import utcnow_iso


class Fake(Channel):
    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[Payload] = []

    def available(self) -> bool:
        return True

    def send(self, p: Payload) -> str | None:
        self.sent.append(p)
        return str(len(self.sent))


def _post(conn, **over):
    now = utcnow_iso()
    v = {
        "source": "company_direct",
        "source_provider": "greenhouse",
        "source_slug": "x",
        "source_job_id": over.pop("jid", "1"),
        "apply_url": "https://x/1",
        "first_seen_at": now,
        "last_seen_at": now,
        "company_name": "Google",
        "company_id": 5,
        "title": "Software Engineer, New Grad",
        "role_family": "software_engineering",
        "seniority": "new_grad",
        "is_new_grad": 1,
        "is_dream_list": 1,
        "company_tier": 1,
        "in_scope": 1,
        "has_requirements": 1,
        "floor_result": "pass",
        "is_cluster_canonical": 1,
        "priority": 0.5,
        "beats_baseline": "clearly_better",
        "beats_baseline_reason": "dream list",
        "primary_metro": "new_york",
        "est_days_to_close": 30,
        "cluster_id": None,
    }
    v.update(over)
    return db.insert_posting(conn, v)


def test_p0_dream_list_and_cluster_dedupe(conn, tmp_project, monkeypatch):
    monkeypatch.setattr("radar.notify.engine.in_quiet_hours", lambda cfg, now=None: False)
    a = _post(conn, jid="1", cluster_id=10)
    _post(conn, jid="2", cluster_id=10, source_provider="github")  # same cluster via aggregator
    ch = Fake()
    st = send_alerts(conn, tmp_project, since="2000-01-01", channels=[ch])
    assert st.sent == 1 and st.by_tier == {"p0": 1}
    assert st.suppressed.get("already alerted on this cluster") == 1
    p = ch.sent[0]
    assert (
        "Google" in p.title
        and p.url == "https://x/1"
        and any("act:applied" in d for _, d in p.buttons)
    )
    assert "clearly better than baseline" in p.body_lines[0]
    # second run: nothing new
    st2 = send_alerts(conn, tmp_project, since="2000-01-01", channels=[ch])
    assert st2.sent == 0
    # engagement resets
    record_engagement(conn, a, "shortlisted")
    assert db.scalar(conn, "SELECT engaged FROM notifications WHERE posting_id = ?", (a,)) == 1


def test_p1_quiet_hours_caps_and_escalation(conn, tmp_project, monkeypatch):
    ch = Fake()
    # P1 (clearly better, not dream list) held during quiet hours
    monkeypatch.setattr("radar.notify.engine.in_quiet_hours", lambda cfg, now=None: True)
    _post(conn, jid="q1", is_dream_list=0, company_id=None, company_name="Acme", company_tier=2)
    st = send_alerts(conn, tmp_project, since="2000-01-01", channels=[ch])
    assert st.sent == 0 and st.suppressed.get("quiet hours (P1 held for the digest)") == 1
    monkeypatch.setattr("radar.notify.engine.in_quiet_hours", lambda cfg, now=None: False)
    # per-company daily cap: 5 clearly-better reqs at Acme → 3 sent
    for i in range(2, 7):
        _post(
            conn, jid=f"q{i}", is_dream_list=0, company_id=None, company_name="Acme", company_tier=2
        )
    st = send_alerts(conn, tmp_project, since="2000-01-01", channels=[ch])
    assert st.sent == 3 and st.suppressed.get("per-company daily cap") == 3
    # escalation: three ignored P1s → Acme demoted to digest
    conn.execute("UPDATE notifications SET sent_at = '2020-01-01T00:00:00Z'")
    assert mark_ignored_and_escalate(conn) == 1
    _post(conn, jid="q9", is_dream_list=0, company_id=None, company_name="Acme", company_tier=2)
    st = send_alerts(conn, tmp_project, since="2000-01-01", channels=[ch])
    assert st.sent == 0 and st.suppressed.get("company demoted to digest (ignored P1s)", 0) >= 1
    pr = precision_report(conn, days=36500)
    assert pr["demotions"] and "Acme" in pr["demotions"][0]


def test_reposts_and_seniors_dont_fire(conn, tmp_project, monkeypatch):
    monkeypatch.setattr("radar.notify.engine.in_quiet_hours", lambda cfg, now=None: False)
    orig = _post(conn, jid="r1", delisted_at="2026-07-01T00:00:00Z")
    _post(conn, jid="r2", repost_of_id=orig, changed_since_first_seen=0)
    _post(conn, jid="s1", in_scope=0, seniority="senior")
    ch = Fake()
    st = send_alerts(conn, tmp_project, since="2000-01-01", channels=[ch])
    assert st.suppressed.get("repost") == 1
    assert all("Senior" not in p.title for p in ch.sent)


def test_quiet_hours_window(tmp_project):
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    assert in_quiet_hours(tmp_project, datetime(2026, 9, 1, 23, 30, tzinfo=et))
    assert in_quiet_hours(tmp_project, datetime(2026, 9, 1, 3, 0, tzinfo=et))
    assert not in_quiet_hours(tmp_project, datetime(2026, 9, 1, 12, 0, tzinfo=et))


def test_digests_and_ics(conn, tmp_project):
    _post(
        conn,
        jid="d1",
        queue_action="apply_today",
        apply_priority_rank=1,
        application_deadline="2099-01-15",
    )
    d = daily_digest(conn, tmp_project)
    assert "1 new postings cleared the floor" in d.body_lines[0] and "TODAY'S QUEUE" in "\n".join(
        d.body_lines
    )
    w = weekly_digest(conn, tmp_project)
    txt = "\n".join(w.body_lines)
    assert (
        "MARKET:" in txt and "SEASON:" in txt and "NOTIFICATIONS" in txt and "SOURCE HEALTH" in txt
    )
    cal = ics_feed(conn, tmp_project)
    assert (
        "BEGIN:VCALENDAR" in cal
        and "Baseline decision deadline" in cal
        and "Deadline: Google" in cal
    )


def test_parse_callback():
    assert parse_callback("act:applied:42") == ("applied", 42)
    assert parse_callback("noop") is None
    assert parse_callback("act:dismiss:x") is None


def test_workflow_actions_shared(conn):
    from radar.workflow import apply_action

    pid = _post(conn, jid="w1")
    assert apply_action(conn, pid, "shortlist", via="test").ok
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (pid,)) == "shortlisted"
    r = apply_action(conn, pid, "applied", via="test")
    assert r.ok and r["application_id"]
    assert db.scalar(conn, "SELECT in_default_view FROM postings WHERE id = ?", (pid,)) == 0
    assert not apply_action(conn, 999999, "shortlist").ok
    assert (
        json.loads(
            db.one(
                conn,
                "SELECT data_json FROM posting_events WHERE posting_id = ? AND event_type='status_changed'",
                (pid,),
            )["data_json"]
        )["via"]
        == "test"
    )
    _ = UTC
