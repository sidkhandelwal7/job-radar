import pytest

from radar import db
from radar.applications import (
    DuplicateApplication,
    add_manual,
    funnel_stats,
    mark_applied,
    set_stage,
    suggestions,
)


def _insert_posting(conn, **over):
    from radar.util import utcnow_iso

    now = utcnow_iso()
    vals = {
        "source": "company_direct",
        "source_provider": "greenhouse",
        "source_slug": "x",
        "source_job_id": over.pop("jid", "1"),
        "apply_url": over.pop("apply_url", "https://x.example/jobs/1"),
        "first_seen_at": now,
        "last_seen_at": now,
        "company_name": "X Corp",
        "title": "Software Engineer, New Grad",
        "locations_json": '[{"raw":"New York, NY","metro_name":"New York, NY"}]',
    }
    vals.update(over)
    return db.insert(conn, "postings", vals)


def test_mark_applied_creates_record_and_removes_from_queue(conn):
    pid = _insert_posting(conn)
    app_id = mark_applied(conn, pid, referral_used=True, referral_contact="A. Friend")
    a = db.one(conn, "SELECT * FROM applications WHERE id = ?", (app_id,))
    assert a["company_name"] == "X Corp" and a["stage"] == "applied" and a["referral_used"] == 1
    assert a["follow_up_due"] > a["applied_at"]
    assert a["location"] == "New York, NY"
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (pid,)) == "applied"
    assert (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM posting_events WHERE posting_id = ? AND event_type='applied'",
            (pid,),
        )
        == 1
    )


def test_duplicate_guard_same_posting_cluster_and_repost(conn):
    pid = _insert_posting(conn, jid="1")
    mark_applied(conn, pid)
    with pytest.raises(DuplicateApplication):
        mark_applied(conn, pid)
    # same cluster, different source
    sib = _insert_posting(conn, jid="2", apply_url="https://x.example/jobs/2")
    db.update(conn, "postings", pid, {"cluster_id": 7})
    db.update(conn, "postings", sib, {"cluster_id": 7})
    with pytest.raises(DuplicateApplication) as e:
        mark_applied(conn, sib)
    assert "cluster" in e.value.reason
    # repost chain
    rep = _insert_posting(conn, jid="3", apply_url="https://x.example/jobs/3", repost_of_id=pid)
    with pytest.raises(DuplicateApplication) as e:
        mark_applied(conn, rep)
    assert "repost" in e.value.reason
    # force works
    assert mark_applied(conn, rep, force=True)


def test_manual_entry_and_url_dup(conn):
    a = add_manual(
        conn,
        url="https://co.example/jobs/42?utm_source=x",
        company_name="Co",
        title="SWE",
        location="Remote",
    )
    assert (
        db.one(conn, "SELECT apply_url FROM applications WHERE id = ?", (a,))["apply_url"]
        == "https://co.example/jobs/42"
    )
    with pytest.raises(DuplicateApplication):
        add_manual(conn, url="https://co.example/jobs/42", company_name="Co", title="SWE")
    with pytest.raises(ValueError):
        add_manual(conn, url=None, company_name=None, title="SWE")


def test_stages_completion_and_suggestions(conn):
    pid = _insert_posting(conn)
    a = mark_applied(conn, pid, applied_at="2026-06-01")
    sug = suggestions(conn)
    assert [x["id"] for x in sug["follow_ups_due"]] == [a]
    assert [x["id"] for x in sug["ghosted_candidates"]] == [a]  # >30 days, no response
    set_stage(conn, a, "screen", note="recruiter call")
    row = db.one(
        conn, "SELECT first_response_at, completed, notes_md FROM applications WHERE id = ?", (a,)
    )
    assert (
        row["first_response_at"] and row["completed"] == 0 and "recruiter call" in row["notes_md"]
    )
    set_stage(conn, a, "rejected")
    row = db.one(
        conn, "SELECT completed, outcome, follow_up_due FROM applications WHERE id = ?", (a,)
    )
    assert row["completed"] == 1 and row["outcome"] == "rejected" and row["follow_up_due"] is None
    st = funnel_stats(conn)
    assert st["total"] == 1 and st["response_rate"] == 1.0 and st["by_stage"] == {"rejected": 1}
    with pytest.raises(ValueError):
        set_stage(conn, a, "bogus")


def test_applications_never_deleted_by_posting_delist(conn):
    pid = _insert_posting(conn)
    a = mark_applied(conn, pid)
    db.update(conn, "postings", pid, {"delisted_at": "2026-09-01T00:00:00Z", "url_status": "dead"})
    assert db.one(conn, "SELECT id FROM applications WHERE id = ?", (a,))
