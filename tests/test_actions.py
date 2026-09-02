"""GitHub Actions runner (no DB) + laptop delta ingestion."""

import asyncio
import json

import respx
from httpx import Response

from radar import db
from radar.actions_ingest import ingest
from radar.actions_runner import invocations_this_month, load_state, prescore, run_actions_cycle
from radar.models import SourceSpec

GH_URL = "https://boards-api.greenhouse.io/v1/boards/brex/jobs?content=true"
DEPT_URL = "https://boards-api.greenhouse.io/v1/boards/brex/departments"


def _spec():
    return SourceSpec(
        provider="greenhouse",
        slug="brex",
        company_slug="brex",
        company_name="Brex",
        cadence="15min",
        extra={
            "company_tier": 1,
            "is_dream_list": 1,
            "target_category": "fintech_infrastructure",
            "is_quant_trading_firm": 0,
        },
    )


@respx.mock
def test_actions_cycle_seed_then_incremental(
    tmp_project, tmp_path, gh_brex_jobs, gh_brex_departments
):
    adir = tmp_path / "actions"
    doc = json.loads(gh_brex_jobs)
    route = respx.get(GH_URL).mock(
        return_value=Response(200, content=gh_brex_jobs, headers={"ETag": '"v1"'})
    )
    respx.get(DEPT_URL).mock(return_value=Response(200, content=gh_brex_departments))
    st = asyncio.run(run_actions_cycle(tmp_project, actions_dir=adir, specs=[_spec()]))
    assert st.seed and st.new == len(doc["jobs"]) and st.ok == 1
    state = load_state(adir / "state.json.gz")
    assert state["sources"]["greenhouse:brex"]["etag"] == '"v1"'
    assert len(state["sources"]["greenhouse:brex"]["seen"]) == len(doc["jobs"])
    delta_files = list((adir / "deltas").glob("*.jsonl"))
    assert len(delta_files) == 1
    lines = [json.loads(x) for x in delta_files[0].read_text().splitlines()]
    assert all(line["seed"] for line in lines) and all(
        line["apply_url"].startswith("http") for line in lines
    )
    # 2. nothing changed → 304 → zero new, zero deltas
    route.mock(return_value=Response(304))
    st2 = asyncio.run(run_actions_cycle(tmp_project, actions_dir=adir, specs=[_spec()]))
    assert not st2.seed and st2.new == 0 and st2.not_modified == 1 and st2.requests == 1
    # 3. one new dream-list new-grad SWE req appears → exactly one delta line, pre-scored p0
    new_job = dict(doc["jobs"][0])
    new_job.update(
        {
            "id": 999999,
            "title": "Software Engineer, New Grad (2027)",
            "absolute_url": "https://www.brex.com/careers/999999?gh_jid=999999",
            "location": {"name": "New York, NY"},
            "content": "<p>We are hiring new grads.</p>",
        }
    )
    doc["jobs"].insert(0, new_job)
    route.mock(
        return_value=Response(200, content=json.dumps(doc).encode(), headers={"ETag": '"v2"'})
    )
    st3 = asyncio.run(run_actions_cycle(tmp_project, actions_dir=adir, specs=[_spec()]))
    assert st3.new == 1 and st3.p0 == 1
    lines = [json.loads(x) for x in delta_files[0].read_text().splitlines()]
    last = lines[-1]
    assert last["job_id"] == "999999" and last["alert"] == "p0" and not last.get("seed")
    assert invocations_this_month(adir) >= 3


def test_prescore_rules():
    base = {
        "role_family": "software_engineering",
        "is_new_grad": 1,
        "seniority": "new_grad",
        "employment_type": "full_time",
        "is_dream_list": 1,
        "company_tier": 1,
    }
    assert prescore(base)[0] == "p0"
    assert prescore({**base, "is_dream_list": 0})[0] == "p1"
    assert prescore({**base, "is_dream_list": 0, "company_tier": 2})[0] == "none"
    assert prescore({**base, "employment_type": "internship"})[0] == "none"
    assert prescore({**base, "is_international_only": 1})[0] == "none"
    assert prescore({**base, "role_family": "product"})[0] == "none"
    assert prescore({**base, "seniority": "senior", "is_new_grad": 0})[0] == "none"


def test_ingest_deltas_backdates_and_inserts(conn, tmp_project, tmp_path):
    adir = tmp_path / "actions" / "deltas"
    adir.mkdir(parents=True)
    # an existing posting first seen "now"; the delta says Actions saw it earlier
    from radar.util import utcnow_iso

    now = utcnow_iso()
    pid = db.insert(
        conn,
        "postings",
        {
            "source": "company_direct",
            "source_provider": "greenhouse",
            "source_slug": "brex",
            "source_job_id": "1",
            "apply_url": "https://x/1",
            "first_seen_at": now,
            "last_seen_at": now,
            "company_name": "Brex",
            "title": "SWE",
        },
    )
    rows = [
        {
            "seen_at": "2026-08-20T10:00:00Z",
            "provider": "greenhouse",
            "slug": "brex",
            "job_id": "1",
            "company": "Brex",
            "company_slug": "brex",
            "title": "SWE",
            "apply_url": "https://x/1",
            "locations": ["New York, NY"],
            "alert": "p1",
        },
        {
            "seen_at": "2026-08-20T10:00:00Z",
            "provider": "greenhouse",
            "slug": "brex",
            "job_id": "2",
            "company": "Brex",
            "company_slug": "brex",
            "title": "Software Engineer, New Grad",
            "apply_url": "https://x/2",
            "locations": ["New York, NY"],
            "posted_at": "2026-08-19",
            "alert": "p0",
        },
    ]
    (adir / "2026-08-20.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    res = ingest(conn, tmp_project, actions_dir=tmp_path / "actions")
    assert res["backdated"] == 1 and res["inserted"] == 1
    assert (
        db.scalar(conn, "SELECT first_seen_at FROM postings WHERE id = ?", (pid,))
        == "2026-08-20T10:00:00Z"
    )
    new = db.one(conn, "SELECT * FROM postings WHERE source_job_id = '2'")
    assert (
        new
        and new["company_id"] is not None
        and new["is_dream_list"] == 0
        and new["primary_metro"] == "new_york"
    )
    # idempotent
    res2 = ingest(conn, tmp_project, actions_dir=tmp_path / "actions")
    assert res2["lines"] == 0
