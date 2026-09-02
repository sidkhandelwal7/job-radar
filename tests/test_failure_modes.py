"""No silent failures (review findings 1, 3, 11): malformed HTTP-200 payloads raise, unexpected
zero-row scans alarm and never delist, a partially failed Actions cycle exits non-zero, and the
scheduler reports partial/failed status instead of success notes."""

import asyncio
import json

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

from radar import db
from radar.fetch.adapters import ashby, greenhouse, lever, oracle, smartrecruiters, workday
from radar.fetch.adapters.base import AdapterError, expect_list
from radar.fetch.pipeline import fetch_all
from radar.fetch.registry import source_specs
from radar.models import SourceSpec

GH_URL = "https://boards-api.greenhouse.io/v1/boards/brex/jobs?content=true"
DEPT_URL = "https://boards-api.greenhouse.io/v1/boards/brex/departments"


def _spec(provider="greenhouse", slug="brex"):
    return SourceSpec(
        provider=provider, slug=slug, company_slug=slug, company_name=slug.title(), cadence="15min"
    )


# --- adapters: a 200 that isn't the list we expect is an AdapterError, never [] -----------------


@pytest.mark.parametrize(
    "adapter, body",
    [
        (greenhouse.GreenhouseAdapter, b"<html><title>Maintenance</title></html>"),
        (greenhouse.GreenhouseAdapter, b'{"error": "board not found"}'),
        (greenhouse.GreenhouseAdapter, b'{"jobs": {"oops": 1}}'),
        (ashby.AshbyAdapter, b'{"jobPostings": []}'),
        (lever.LeverAdapter, b'{"ok": true}'),
        (workday.WorkdayAdapter, b'{"total": 0}'),
        (oracle.OracleAdapter, b"not json at all"),
        (smartrecruiters.SmartRecruitersAdapter, b'{"totalFound": 0}'),
    ],
)
def test_malformed_200_payload_raises(adapter, body):
    a = adapter(None)
    with pytest.raises(AdapterError):
        a.parse_page(_spec(a.provider), body, "https://example.test/list")


def test_expect_list_accepts_legitimate_empty_and_null():
    assert expect_list(b'{"jobs": []}', "jobs", provider="x") == []
    assert expect_list(b'{"jobs": null}', "jobs", provider="x") == []
    assert expect_list(b"[]", None, provider="x") == []
    with pytest.raises(AdapterError):
        expect_list(b"{}", None, provider="x")


@respx.mock
def test_malformed_200_is_a_source_failure_not_a_delist(
    conn, tmp_project, gh_brex_jobs, gh_brex_departments
):
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    route = respx.get(GH_URL).mock(
        return_value=Response(200, content=gh_brex_jobs, headers={"ETag": '"v1"'})
    )
    specs = source_specs(conn, providers={"greenhouse"}, company="brex")
    s1 = asyncio.run(fetch_all(conn, tmp_project, specs, budget_seconds=-1))
    n = db.scalar(conn, "SELECT COUNT(*) FROM postings")
    assert s1.outcomes[0].ok and n > 0
    # board answers 200 with an HTML maintenance page
    route.mock(
        return_value=Response(200, content=b"<html>be right back</html>", headers={"ETag": '"v2"'})
    )
    s2 = asyncio.run(fetch_all(conn, tmp_project, specs, budget_seconds=-1))
    o = s2.outcomes[0]
    assert not o.ok and "AdapterError" in (o.error or "")
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE delisted_at IS NOT NULL") == 0
    src = db.one(
        conn, "SELECT consecutive_failures, last_error FROM company_sources WHERE slug = 'brex'"
    )
    assert src["consecutive_failures"] == 1 and "not JSON" in src["last_error"]


@respx.mock
def test_unexpected_zero_rows_alarms_and_suppresses_delists(
    conn, tmp_project, gh_brex_jobs, gh_brex_departments
):
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    route = respx.get(GH_URL).mock(
        return_value=Response(200, content=gh_brex_jobs, headers={"ETag": '"v1"'})
    )
    specs = source_specs(conn, providers={"greenhouse"}, company="brex")
    asyncio.run(fetch_all(conn, tmp_project, specs, budget_seconds=-1))
    before = db.scalar(conn, "SELECT typical_row_count FROM company_sources WHERE slug = 'brex'")
    # a legitimate-looking but empty list on a board that had rows: drift, not a quiet market
    route.mock(return_value=Response(200, content=b'{"jobs": []}', headers={"ETag": '"v2"'}))
    s2 = asyncio.run(fetch_all(conn, tmp_project, specs, budget_seconds=-1))
    o = s2.outcomes[0]
    assert o.ok and o.drift and o.delisted == 0
    src = db.one(
        conn, "SELECT drift_note, typical_row_count FROM company_sources WHERE slug = 'brex'"
    )
    assert src["drift_note"] and src["typical_row_count"] == before  # baseline not eroded
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE delisted_at IS NOT NULL") == 0
    from radar.ops import alarms

    assert any(a.key == "drift" for a in alarms.evaluate(conn, tmp_project))


@respx.mock
def test_zero_rows_with_no_baseline_still_alarms_once(conn, tmp_project, gh_brex_departments):
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    respx.get(GH_URL).mock(return_value=Response(200, content=b'{"jobs": []}'))
    specs = source_specs(conn, providers={"greenhouse"}, company="brex")
    s1 = asyncio.run(fetch_all(conn, tmp_project, specs, budget_seconds=-1))
    assert s1.outcomes[0].drift  # first-ever scan returning nothing is unexpected
    s2 = asyncio.run(fetch_all(conn, tmp_project, specs, budget_seconds=-1))
    assert not s2.outcomes[0].drift  # now a known-empty source: no repeat alarm


# --- Actions: any failed source fails the job --------------------------------------------------


@respx.mock
def test_actions_cycle_partial_failure_exits_nonzero(
    tmp_project, tmp_path, gh_brex_jobs, gh_brex_departments, monkeypatch
):
    from radar import cli

    adir = tmp_path / "actions"
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    respx.get(GH_URL).mock(return_value=Response(200, content=gh_brex_jobs))
    respx.get("https://boards-api.greenhouse.io/v1/boards/broken/jobs?content=true").mock(
        return_value=Response(500)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/broken/departments").mock(
        return_value=Response(500)
    )
    good = SourceSpec(
        provider="greenhouse",
        slug="brex",
        company_slug="brex",
        company_name="Brex",
        cadence="15min",
        extra={"company_tier": 1},
    )
    bad = SourceSpec(
        provider="greenhouse",
        slug="broken",
        company_slug="broken",
        company_name="Broken",
        cadence="15min",
        extra={"company_tier": 1},
    )
    monkeypatch.setattr("radar.actions_runner.tier1_specs", lambda cfg: [good, bad], raising=False)
    monkeypatch.setattr(cli, "_actions_specs", lambda cfg: [good, bad], raising=False)
    from radar.actions_runner import run_actions_cycle

    st = asyncio.run(run_actions_cycle(tmp_project, actions_dir=adir, specs=[good, bad]))
    assert st.ok == 1 and st.failed == 1
    # the CLI turns one failure into a non-zero exit (it used to need ALL sources to fail)
    runner = CliRunner()
    monkeypatch.setattr(
        "radar.actions_runner.run_actions_cycle", lambda cfg, **kw: asyncio.sleep(0, result=st)
    )
    r = runner.invoke(cli.app, ["actions-cycle", "--dir", str(adir)])
    assert r.exit_code == 1, r.output


# --- scheduler: failures change status; watermarks don't advance -------------------------------


def test_cycle_reports_partial_and_failed(conn, tmp_project, monkeypatch):
    from radar import scheduler

    async def boom(*a, **k):
        raise RuntimeError("score exploded")

    monkeypatch.setattr("radar.fetch.registry.source_specs", lambda *a, **k: [])
    monkeypatch.setattr(
        "radar.score.engine.score_all",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("score exploded")),
    )
    monkeypatch.setattr("radar.fetch.html_detail.fetch_missing", boom)
    rep = scheduler.run_cycle_sync(
        conn, tmp_project, skip_verify=True, skip_discover=True, skip_enrich=True
    )
    names = dict(rep.failures)
    assert "score" in names and "descriptions" in names
    assert rep.status == "failed"
    assert db.kv_get(conn, "last_cycle_at") is None  # a failed cycle does not advance the watermark
    run = db.one(
        conn, "SELECT status, error FROM runs WHERE kind = 'cycle' ORDER BY id DESC LIMIT 1"
    )
    assert run["status"] == "failed" and "score exploded" in run["error"]
    # a best-effort failure alone is 'partial' and the watermark advances
    monkeypatch.setattr("radar.score.engine.score_all", lambda *a, **k: {"scored": 0})
    rep2 = scheduler.run_cycle_sync(
        conn, tmp_project, skip_verify=True, skip_discover=True, skip_enrich=True
    )
    assert rep2.status == "partial" and dict(rep2.failures).keys() == {"descriptions"}
    assert db.kv_get(conn, "last_cycle_at") is not None
    _ = json
