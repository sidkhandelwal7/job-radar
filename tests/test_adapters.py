"""Adapters parse recorded real payloads (never hand-written fakes)."""

import json

from radar.fetch.adapters.greenhouse import GreenhouseAdapter
from radar.fetch.adapters.oracle import OracleAdapter
from radar.fetch.adapters.workday import WorkdayAdapter, posted_on_to_date
from radar.models import SourceSpec


def _combined(provider, slug, pages):
    return json.dumps(
        {
            "provider": provider,
            "slug": slug,
            "mode": "full",
            "pages": [{"url": u, "status": 200, "body": b.decode()} for u, b in pages],
        }
    ).encode()


def test_greenhouse_parse(gh_brex_jobs, gh_brex_departments):
    spec = SourceSpec(provider="greenhouse", slug="brex", company_slug="brex", company_name="Brex")
    payload = _combined(
        "greenhouse",
        "brex",
        [
            ("https://boards-api.greenhouse.io/v1/boards/brex/jobs?content=true", gh_brex_jobs),
            ("https://boards-api.greenhouse.io/v1/boards/brex/departments", gh_brex_departments),
        ],
    )
    jobs = GreenhouseAdapter(None).parse_payload(spec, payload)
    assert len(jobs) > 100
    j = jobs[0]
    assert j.source_job_id.isdigit()
    assert j.apply_url.startswith("http") and j.source_job_id in j.apply_url
    assert j.canonical_url == f"https://job-boards.greenhouse.io/brex/jobs/{j.source_job_id}"
    assert j.locations and j.title
    assert j.description_md  # content=true gives descriptions inline
    assert any(job.department for job in jobs)
    # no duplicates
    assert len({job.source_job_id for job in jobs}) == len(jobs)


def test_workday_parse_and_detail(wd_mastercard_page, wd_mastercard_detail):
    spec = SourceSpec(
        provider="workday",
        slug="mastercard/wd1/CorporateCareers",
        company_slug="mastercard",
        company_name="Mastercard",
    )
    adapter = WorkdayAdapter(None)
    jobs = adapter.parse_page(spec, wd_mastercard_page)
    assert len(jobs) == 20
    j = jobs[0]
    assert j.apply_url.startswith(
        "https://mastercard.wd1.myworkdayjobs.com/en-US/CorporateCareers/job/"
    )
    assert j.detail_needed and j.detail_ref.startswith("/job/")
    assert j.source_job_id.startswith("R-")
    adapter.apply_detail(j, wd_mastercard_detail)
    assert j.description_md and len(j.description_md) > 200
    assert j.detail_needed is False
    assert j.employment_type == "Full time"
    assert j.locations


def test_workday_posted_on():
    assert posted_on_to_date("Posted Today")
    assert posted_on_to_date("Posted 30+ Days Ago") < posted_on_to_date("Posted 2 Days Ago")
    assert posted_on_to_date(None) is None


def test_oracle_parse_and_detail(ora_jpmc_page, ora_jpmc_detail):
    spec = SourceSpec(
        provider="oracle",
        slug="jpmc.fa.oraclecloud.com/CX_1001",
        company_slug="jpmorgan",
        company_name="JPMorgan Chase",
    )
    adapter = OracleAdapter(None)
    jobs = adapter.parse_page(spec, ora_jpmc_page)
    assert len(jobs) == 25
    j = jobs[0]
    assert (
        j.apply_url
        == f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{j.source_job_id}"
    )
    assert j.locations and j.posted_at
    adapter.apply_detail(j, ora_jpmc_detail)
    assert j.description_md and len(j.description_md) > 500
    assert j.employment_type == "Full time"


def test_detect_urls():
    assert (
        GreenhouseAdapter.detect("https://job-boards.greenhouse.io/stripe/jobs/123").slug
        == "stripe"
    )
    assert (
        WorkdayAdapter.detect(
            "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/job/McLean-VA/x_R1"
        ).slug
        == "capitalone/wd12/Capital_One"
    )
    assert (
        OracleAdapter.detect(
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210773795"
        ).slug
        == "jpmc.fa.oraclecloud.com/CX_1001"
    )
    assert GreenhouseAdapter.detect("https://example.com/careers") is None
