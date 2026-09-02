"""End-to-end fetch pipeline against a mocked Greenhouse API serving the recorded payload."""

import asyncio
import json

import pytest
import respx
from httpx import Response

from radar import db
from radar.fetch.pipeline import fetch_all
from radar.fetch.registry import source_specs

GH_URL = "https://boards-api.greenhouse.io/v1/boards/brex/jobs?content=true"
DEPT_URL = "https://boards-api.greenhouse.io/v1/boards/brex/departments"


def _run(conn, cfg, **kw):
    specs = source_specs(conn, providers={"greenhouse"}, company="brex")
    assert len(specs) == 1
    return asyncio.run(fetch_all(conn, cfg, specs, **kw))


@respx.mock
def test_fetch_rerun_delist_relist(conn, tmp_project, gh_brex_jobs, gh_brex_departments):
    jobs_doc = json.loads(gh_brex_jobs)
    route = respx.get(GH_URL).mock(
        return_value=Response(200, content=gh_brex_jobs, headers={"ETag": '"v1"'})
    )
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))

    s1 = _run(conn, tmp_project)
    o = s1.outcomes[0]
    assert o.ok and o.new == len(jobs_doc["jobs"]) and o.delisted == 0
    n = db.scalar(conn, "SELECT COUNT(*) FROM postings")
    assert n == len(jobs_doc["jobs"])
    # every row has a direct link and is source-confirmed live
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE apply_url NOT LIKE 'http%'") == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE url_status = 'live'") == n
    # raw payload stored, gzipped, append-only
    assert db.scalar(conn, "SELECT COUNT(*) FROM raw_payloads WHERE kind='list'") == 1
    assert list(tmp_project.raw_dir.rglob("*.json.gz"))

    # 2. identical re-run → zero new, zero changes, zero delists (conditional GET → 304)
    route.mock(return_value=Response(304))
    s2 = _run(conn, tmp_project)
    assert s2.outcomes[0].not_modified and s2.outcomes[0].new == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings") == n

    # 2b. same content, new ETag (a real 200 full scan): unchanged jobs take the cheap path —
    #     no re-parse, no 'changed' events, but presence/last_seen refreshed
    import radar.fetch.pipeline as pl

    calls = {"n": 0}
    real_build = pl.build_posting_values

    def counting(*a, **kw):
        calls["n"] += 1
        return real_build(*a, **kw)

    route.mock(
        return_value=Response(200, content=json.dumps(jobs_doc).encode(), headers={"ETag": '"v1b"'})
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pl, "build_posting_values", counting)
        s2b = _run(conn, tmp_project)
    assert s2b.outcomes[0].new == 0 and s2b.outcomes[0].changed == 0 and calls["n"] == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE raw_hash IS NULL") == 0

    # 3. upstream removes one job → delisted within one cycle; change another's title → 'changed'
    removed = jobs_doc["jobs"][0]
    changed = jobs_doc["jobs"][1]
    changed["title"] = changed["title"] + " (Updated)"
    jobs_doc["jobs"] = jobs_doc["jobs"][1:]
    route.mock(
        return_value=Response(200, content=json.dumps(jobs_doc).encode(), headers={"ETag": '"v2"'})
    )
    s3 = _run(conn, tmp_project)
    o3 = s3.outcomes[0]
    assert o3.delisted == 1 and o3.changed == 1 and o3.new == 0
    row = db.one(
        conn,
        "SELECT delisted_at, url_status FROM postings WHERE source_job_id = ?",
        (str(removed["id"]),),
    )
    assert row["delisted_at"] is not None
    assert db.scalar(conn, "SELECT COUNT(*) FROM posting_events WHERE event_type='delisted'") == 1
    assert db.scalar(conn, "SELECT COUNT(*) FROM posting_events WHERE event_type='changed'") == 1
    assert (
        db.scalar(
            conn,
            "SELECT changed_since_first_seen FROM postings WHERE source_job_id = ?",
            (str(changed["id"]),),
        )
        == 1
    )

    # 4. it comes back → relisted, delisted_at cleared, still the same row (no duplicate)
    jobs_doc["jobs"].insert(0, removed)
    route.mock(
        return_value=Response(200, content=json.dumps(jobs_doc).encode(), headers={"ETag": '"v3"'})
    )
    s4 = _run(conn, tmp_project)
    assert s4.outcomes[0].relisted == 1 and s4.outcomes[0].new == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings") == n
    assert (
        db.scalar(
            conn, "SELECT delisted_at FROM postings WHERE source_job_id = ?", (str(removed["id"]),)
        )
        is None
    )


@respx.mock
def test_drift_guard_suppresses_delists(conn, tmp_project, gh_brex_jobs, gh_brex_departments):
    jobs_doc = json.loads(gh_brex_jobs)
    route = respx.get(GH_URL).mock(return_value=Response(200, content=gh_brex_jobs))
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    _run(conn, tmp_project)
    n = db.scalar(conn, "SELECT COUNT(*) FROM postings")
    # a broken adapter / half-broken source returns 3 rows: must NOT delist the other n-3
    jobs_doc["jobs"] = jobs_doc["jobs"][:3]
    route.mock(return_value=Response(200, content=json.dumps(jobs_doc).encode()))
    s = _run(conn, tmp_project)
    assert s.outcomes[0].drift is True
    assert s.outcomes[0].delisted == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE delisted_at IS NULL") == n


@respx.mock
def test_source_failure_is_isolated(conn, tmp_project):
    respx.get(GH_URL).mock(return_value=Response(500))
    s = _run(conn, tmp_project)
    assert s.outcomes[0].ok is False
    src = db.one(
        conn, "SELECT consecutive_failures, last_error FROM company_sources WHERE slug='brex'"
    )
    assert src["consecutive_failures"] == 1 and src["last_error"]


@respx.mock
def test_huge_retry_after_fails_fast_and_budget_defers(conn, tmp_project):
    import time

    # a server asking for an hour must not park the cycle for an hour (Retry-After capped)
    respx.get(GH_URL).mock(return_value=Response(429, headers={"Retry-After": "3600"}))
    t = time.monotonic()
    s = _run(conn, tmp_project)
    assert time.monotonic() - t < 10
    assert s.outcomes[0].ok is False and "Retry-After" in (s.outcomes[0].error or "")
    # zero budget: every source is deferred, nothing recorded as a failure
    s2 = _run(conn, tmp_project, budget_seconds=0.0)
    assert s2.outcomes[0].mode == "deferred" and s2.stats["sources_deferred"] == 1
    assert s2.stats["sources_failed"] == 0


@respx.mock
def test_rescore_replay_preserves_workflow_and_applications(
    conn, tmp_project, gh_brex_jobs, gh_brex_departments
):
    from radar.applications import add_manual, mark_applied
    from radar.rescore import rescore

    respx.get(GH_URL).mock(return_value=Response(200, content=gh_brex_jobs))
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    _run(conn, tmp_project)
    pid = db.scalar(conn, "SELECT id FROM postings ORDER BY id LIMIT 1")
    app_id = mark_applied(conn, pid, notes="hello")
    manual_id = add_manual(
        conn,
        url="https://example.com/jobs/1",
        company_name="Acme",
        title="SWE New Grad",
        location="Pittsburgh, PA",
    )
    db.update(
        conn,
        "postings",
        pid + 1,
        {"status": "dismissed", "dismiss_reason": "too senior", "notes_md": "keep"},
    )
    respx.reset()  # replay must not touch the network
    out = rescore(conn, tmp_project, replay=True)
    assert out["replay"]["replayed"] > 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM applications") == 2
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (pid,)) == "applied"
    row = db.one(
        conn, "SELECT status, dismiss_reason, notes_md FROM postings WHERE id = ?", (pid + 1,)
    )
    assert (row["status"], row["dismiss_reason"], row["notes_md"]) == (
        "dismissed",
        "too senior",
        "keep",
    )
    assert (
        db.one(conn, "SELECT posting_id FROM applications WHERE id = ?", (app_id,))["posting_id"]
        == pid
    )
    assert (
        db.one(conn, "SELECT created_manually FROM applications WHERE id = ?", (manual_id,))[
            "created_manually"
        ]
        == 1
    )
    assert not respx.calls  # zero re-fetching
