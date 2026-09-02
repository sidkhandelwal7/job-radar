import asyncio

import respx
from httpx import Response

from radar.fetch.http import PoliteClient
from radar.links import verify_http

UA = "test"


def _v(url, ids, title="Software Engineer"):
    async def go():
        async with PoliteClient(UA) as c:
            return await verify_http(c, url, ids, title)

    return asyncio.run(go())


@respx.mock
def test_live_when_id_in_page():
    respx.get("https://co.example/jobs/123").mock(
        return_value=Response(200, text="<html><title>Software Engineer</title>req 123</html>")
    )
    v = _v("https://co.example/jobs/123", ["123"])
    assert v.status == "live" and v.method == "http"


@respx.mock
def test_redirect_to_generic_careers_page_is_dead():
    respx.get("https://co.example/jobs/123").mock(
        return_value=Response(302, headers={"location": "https://co.example/careers"})
    )
    respx.get("https://co.example/careers").mock(
        return_value=Response(200, text="<html>Join us! Open roles</html>")
    )
    v = _v("https://co.example/jobs/123", ["123"])
    assert v.status == "dead"
    assert "generic" in v.reason


@respx.mock
def test_redirect_that_keeps_req_is_redirected_not_dead():
    respx.get("https://co.example/jobs/123").mock(
        return_value=Response(
            301, headers={"location": "https://jobs.co.example/positions/123-software-engineer"}
        )
    )
    respx.get("https://jobs.co.example/positions/123-software-engineer").mock(
        return_value=Response(200, text="<html>Software Engineer</html>")
    )
    v = _v("https://co.example/jobs/123", ["123"])
    assert v.status == "redirected"


@respx.mock
def test_404_and_soft_404():
    respx.get("https://co.example/jobs/1").mock(return_value=Response(404))
    assert _v("https://co.example/jobs/1", ["1"]).status == "dead"
    respx.get("https://co.example/jobs/2").mock(
        return_value=Response(200, text="<html>This job is no longer available.</html>")
    )
    assert _v("https://co.example/jobs/2", ["2"]).status == "dead"


@respx.mock
def test_title_alone_never_makes_a_page_live():
    """Review finding 4: a 200 at the same URL whose body mentions the title but not the req id is
    unverified — every 'Software Engineer' page says 'Software Engineer'."""
    respx.get("https://co.example/apply/abc").mock(
        return_value=Response(
            200, text="<html><h1>Software Engineer</h1><p>Join our team</p></html>"
        )
    )
    v = _v("https://co.example/apply/abc", ["R99887"], title="Software Engineer")
    assert v.status == "unverified"
    assert "req id is not visible" in v.reason and "proves nothing" in v.reason


@respx.mock
def test_200_without_id_or_title_is_unverified_not_live():
    respx.get("https://co.example/apply/xyz").mock(
        return_value=Response(
            200, text="<html><div id=root></div><script src=app.js></script></html>"
        )
    )
    v = _v("https://co.example/apply/xyz", ["R12345"], title="Data Engineer")
    assert v.status == "unverified" and v.http_status == 200


@respx.mock
def test_id_in_body_is_live_even_without_title():
    respx.get("https://co.example/apply/q").mock(
        return_value=Response(200, text="<html>Requisition R12345 — apply below</html>")
    )
    v = _v("https://co.example/apply/q", ["R12345"], title="Totally Different Title")
    assert v.status == "live" and "req id" in v.reason


def test_sweep_selection_is_priority_ordered_not_fifo(conn, tmp_project):
    """D61: applications → Today → shortlisted → queue rank → dream/clearly_better; rows nobody
    would act on are NOT selected just because their HTTP check is old (source presence covers
    them), except possible ghosts the source hasn't confirmed in >3 days."""
    from radar import db
    from radar.links import select_for_sweep
    from radar.util import utcnow_iso

    now = utcnow_iso()

    def mk(jid, **over):
        v = {
            "source": "company_direct",
            "source_provider": "greenhouse",
            "source_slug": "x",
            "source_job_id": jid,
            "apply_url": f"https://h{jid}.example/jobs/{jid}",
            "first_seen_at": now,
            "last_seen_at": now,
            "company_name": "Acme",
            "title": "SWE",
            "in_scope": 1,
            "floor_result": "pass",
            "is_cluster_canonical": 1,
            "url_verify_method": "source_presence",
            "url_last_verified_at": "2026-08-01T00:00:00Z",
        }
        v.update(over)
        return db.insert_posting(conn, v)

    applied = mk("a", apply_priority_rank=400)
    db.insert(
        conn,
        "applications",
        {
            "posting_id": applied,
            "company_name": "Acme",
            "title": "SWE",
            "apply_url": "https://x/a",
            "applied_at": now,
            "stage": "applied",
            "stage_changed_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    today = mk("t", queue_action="apply_today", apply_priority_rank=3)
    shortlisted = mk("s", status="shortlisted", apply_priority_rank=200)
    ranked_hi = mk("r1", apply_priority_rank=10)
    ranked_dream = mk("r2", apply_priority_rank=50, is_dream_list=1)
    ranked_lo = mk("r3", apply_priority_rank=5000)
    bystander = mk("b", in_scope=0)  # stale HTTP check but fresh at source, no rank: NOT selected
    ghost = mk(
        "g", in_scope=0, last_seen_at="2026-08-20T00:00:00Z"
    )  # source hasn't confirmed in >3d
    rows = select_for_sweep(conn, tmp_project, limit=10)
    ids = [r["id"] for r in rows]
    assert ids[0] == applied and ids[1] == today and ids[2] == shortlisted
    assert set(ids[3:5]) >= {ranked_hi} and ranked_dream in ids and ranked_lo in ids
    assert bystander not in ids
    assert ghost in ids and ids.index(ghost) > ids.index(ranked_lo)
