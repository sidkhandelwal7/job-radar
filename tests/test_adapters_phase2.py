"""Phase 2 adapters parse recorded real payloads."""

from radar.fetch.adapters.ashby import AshbyAdapter
from radar.fetch.adapters.github_aggregators import GitHubAggregatorAdapter, _parse_date
from radar.fetch.adapters.lever import LeverAdapter
from radar.fetch.adapters.recruitee import RecruiteeAdapter
from radar.fetch.adapters.smartrecruiters import SmartRecruitersAdapter
from radar.fetch.adapters.workable import WorkableAdapter
from radar.models import SourceSpec
from tests.conftest import load_fixture


def test_lever():
    spec = SourceSpec(
        provider="lever", slug="palantir", company_slug="palantir", company_name="Palantir"
    )
    jobs = LeverAdapter(None).parse_page(spec, load_fixture("lever/palantir.json.gz"))
    assert len(jobs) > 200
    j = jobs[0]
    assert (
        j.apply_url.startswith("https://jobs.lever.co/palantir/") and j.source_job_id in j.apply_url
    )
    assert j.locations and j.description_md and j.posted_at
    assert LeverAdapter.detect(j.apply_url).slug == "palantir"


def test_ashby_with_compensation():
    spec = SourceSpec(provider="ashby", slug="ramp", company_slug="ramp", company_name="Ramp")
    jobs = AshbyAdapter(None).parse_page(spec, load_fixture("ashby/ramp.json.gz"))
    assert len(jobs) > 50
    with_comp = [j for j in jobs if j.comp]
    assert with_comp, "Ashby should expose structured comp"
    c = with_comp[0].comp
    assert (
        c.source == "ashby_posted" and c.min and c.max and c.max >= c.min and c.interval == "year"
    )
    assert all(j.apply_url.startswith("https://jobs.ashbyhq.com/ramp/") for j in jobs)
    assert any("New York" in " ".join(j.locations) for j in jobs)


def test_smartrecruiters_list_and_detail():
    spec = SourceSpec(
        provider="smartrecruiters", slug="Visa", company_slug="visa", company_name="Visa"
    )
    a = SmartRecruitersAdapter(None)
    jobs = a.parse_page(spec, load_fixture("smartrecruiters/visa_p0.json.gz"))
    assert jobs and jobs[0].detail_needed
    a.apply_detail(jobs[0], load_fixture("smartrecruiters/visa_detail.json.gz"))
    assert jobs[0].description_md and jobs[0].apply_url.startswith(
        "https://jobs.smartrecruiters.com/"
    )
    assert jobs[0].employment_type == "Full-time"
    assert jobs[0].locations and "," in jobs[0].locations[0]


def test_recruitee():
    spec = SourceSpec(provider="recruitee", slug="bunq", company_slug="bunq", company_name="bunq")
    jobs = RecruiteeAdapter(None).parse_page(spec, load_fixture("recruitee/offers.json.gz"))
    assert jobs and jobs[0].apply_url.startswith("https://careers.bunq.com/o/")
    assert jobs[0].locations and jobs[0].description_md


def test_workable_detect():
    assert WorkableAdapter.detect("https://apply.workable.com/acme/j/ABC123/").slug == "acme"
    assert WorkableAdapter.detect("https://acme.workable.com/jobs/1").slug == "acme"


def test_simplify_json():
    spec = SourceSpec(
        provider="github",
        slug="SimplifyJobs/New-Grad-Positions",
        company_slug="agg",
        company_name="Simplify",
    )
    jobs = GitHubAggregatorAdapter(None).parse_page(
        spec, load_fixture("github/simplify_listings_sample.json.gz"), url="x.json"
    )
    assert len(jobs) > 300
    j = jobs[0]
    assert j.company_name and j.title and j.apply_url.startswith("http") and j.locations
    assert "utm_source" not in j.apply_url
    assert "simplify" in j.tags


def test_markdown_tables_three_formats():
    a = GitHubAggregatorAdapter(None)
    for slug, fx, expect_host in [
        ("vanshb03/New-Grad-2027", "github/vanshb03_README.md.gz", "jobs.ashbyhq.com"),
        (
            "zapplyjobs/New-Grad-Software-Engineering-Jobs-2027",
            "github/zapply_swe_README.md.gz",
            "myworkdayjobs.com",
        ),
        (
            "jobright-ai/2026-Software-Engineer-New-Grad",
            "github/jobright_README.md.gz",
            "jobright.ai",
        ),
    ]:
        spec = SourceSpec(provider="github", slug=slug, company_slug="agg", company_name="agg")
        jobs = a.parse_page(spec, load_fixture(fx), url="README.md")
        assert len(jobs) > 50, slug
        assert all(j.company_name and j.title and j.apply_url.startswith("http") for j in jobs), (
            slug
        )
        assert any(expect_host in j.apply_url for j in jobs), slug
        assert sum(1 for j in jobs if j.posted_at) > len(jobs) * 0.8, slug
        assert len({j.source_job_id for j in jobs}) == len(jobs)
    # vanshb03 splits multi-location cells
    spec = SourceSpec(
        provider="github", slug="vanshb03/New-Grad-2027", company_slug="agg", company_name="agg"
    )
    jobs = a.parse_page(spec, load_fixture("github/vanshb03_README.md.gz"), url="README.md")
    assert any(len(j.locations) > 1 for j in jobs)


def test_parse_date_forms():
    assert _parse_date("Aug 05")
    assert _parse_date("10m") and _parse_date("3d")
    assert _parse_date("") is None
