import pytest

from radar import db
from radar.query import QueryError, compile_query


@pytest.mark.parametrize(
    "q",
    [
        "base > 110000 AND category:big_tech AND days_to_close < 14 AND NOT requires_clearance",
        'company:"capital one" new grad',
        "beats_baseline:clearly_better OR (fit >= 0.7 AND NOT applied)",
        "metro:nyc tag:dotnet -delisted",
        "base>=100k status!=dismissed",
        "posted >= 2026-08-01",
        "",
    ],
)
def test_compiles_and_executes(conn, q):
    c = compile_query(q)
    db.all_rows(conn, f"SELECT p.id FROM postings p WHERE {c.where}", c.params)


def test_shorthands_and_k_suffix():
    c = compile_query("category:big_tech base>=100k metro:sf")
    assert "big_tech_swe" in c.params and 100000 in c.params and "san_francisco" in c.params


def test_keywords_not_swallowed_as_text():
    c = compile_query("android developer")
    assert c.params == ['"android"', '"developer"']
    c = compile_query("nyc or sf")
    assert " OR " in c.where


def test_unknown_field_errors():
    with pytest.raises(QueryError):
        compile_query("bogus:1")


# --- row-level correctness: the compiled SQL must select exactly the rows a human would ----------


def _insert(conn, **over):
    from radar.util import utcnow_iso

    now = utcnow_iso()
    v = {
        "source": "company_direct",
        "source_provider": "greenhouse",
        "source_slug": "x",
        "source_job_id": over.pop("jid"),
        "apply_url": f"https://x/{over.get('title', 'j')}",
        "first_seen_at": now,
        "last_seen_at": now,
        "company_name": "Acme",
        "title": "Software Engineer",
        "seniority": "new_grad",
        "role_family": "software_engineering",
        "in_scope": 1,
        "floor_result": "pass",
        "is_cluster_canonical": 1,
        "status": "new",
    }
    v.update(over)
    return db.insert_posting(conn, v)


@pytest.fixture()
def corpus(conn):
    """Nine rows that differ on exactly the axes the query language exposes."""
    ids = {}
    ids["nyc_big"] = _insert(
        conn,
        jid="1",
        company_name="Google",
        title="Software Engineer, New Grad",
        primary_metro="new_york",
        target_category="big_tech_swe",
        base_est=150000,
        base_posted_min=140000,
        base_posted_max=160000,
        beats_baseline="clearly_better",
        fit_score=0.8,
        requires_clearance=0,
        tech_tags_json='["python","go"]',
        posted_at="2026-08-10T00:00:00Z",
        application_deadline="2026-09-01",
        status="new",
    )
    ids["sf_est"] = _insert(
        conn,
        jid="2",
        company_name="Stripe",
        title="New Grad: Software Engineer",
        primary_metro="san_francisco",
        target_category="fintech_infrastructure",
        base_est=120000,
        beats_baseline="clearly_better",
        fit_score=0.6,
        requires_clearance=0,
        tech_tags_json='["ruby"]',
        posted_at="2026-07-01T00:00:00Z",
    )
    ids["dc_clear"] = _insert(
        conn,
        jid="3",
        company_name="Palantir",
        title="Forward Deployed Engineer",
        primary_metro="washington_dc",
        target_category="defense_and_gov_tech",
        base_est=110000,
        beats_baseline="arguably_better",
        fit_score=0.7,
        requires_clearance=1,
        tech_tags_json='["java"]',
        posted_at="2026-08-15T00:00:00Z",
    )
    ids["cheap"] = _insert(
        conn,
        jid="4",
        company_name="Acme",
        title="Junior Developer",
        primary_metro="pittsburgh",
        target_category="other",
        base_est=70000,
        beats_baseline="worse",
        fit_score=0.3,
        requires_clearance=0,
        tech_tags_json='["dotnet","csharp"]',
        posted_at="2026-08-18T00:00:00Z",
    )
    ids["applied"] = _insert(
        conn,
        jid="5",
        company_name="Capital One",
        title="Software Engineer, Early Career",
        primary_metro="washington_dc",
        target_category="bank_and_exchange_tech",
        base_est=105000,
        beats_baseline="arguably_better",
        fit_score=0.75,
        requires_clearance=0,
        status="applied",
        tech_tags_json='["java","aws"]',
        posted_at="2026-08-05T00:00:00Z",
    )
    ids["dismissed"] = _insert(
        conn,
        jid="6",
        company_name="Acme",
        title="Data Analyst",
        primary_metro="chicago",
        target_category="other",
        base_est=90000,
        beats_baseline="worse",
        fit_score=0.4,
        status="dismissed",
        dismiss_reason="not engineering",
        tech_tags_json="[]",
        posted_at="2026-08-01T00:00:00Z",
    )
    ids["delisted"] = _insert(
        conn,
        jid="7",
        company_name="Meta",
        title="Software Engineer, University Grad",
        primary_metro="seattle",
        target_category="big_tech_swe",
        base_est=160000,
        beats_baseline="clearly_better",
        fit_score=0.65,
        delisted_at="2026-08-19T00:00:00Z",
        tech_tags_json='["cpp"]',
        posted_at="2026-06-01T00:00:00Z",
    )
    ids["nocomp"] = _insert(
        conn,
        jid="8",
        company_name="Stealth",
        title="Founding Engineer",
        primary_metro=None,
        target_category="other",
        base_est=None,
        beats_baseline=None,
        fit_score=None,
        tech_tags_json="[]",
        posted_at=None,
    )
    ids["android"] = _insert(
        conn,
        jid="9",
        company_name="Duolingo",
        title="Android Developer, New Grad",
        primary_metro="pittsburgh",
        target_category="other",
        base_est=125000,
        beats_baseline="clearly_better",
        fit_score=0.5,
        tech_tags_json='["kotlin","android"]',
        posted_at="2026-08-12T00:00:00Z",
    )
    # `applied` means "has an application record" (the tracker is the source of truth), not status text
    from radar.util import utcnow_iso

    db.insert(
        conn,
        "applications",
        {
            "posting_id": ids["applied"],
            "company_name": "Capital One",
            "title": "Software Engineer, Early Career",
            "apply_url": "https://x/applied",
            "applied_at": utcnow_iso(),
            "stage": "applied",
            "stage_changed_at": utcnow_iso(),
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        },
    )
    conn.execute("INSERT INTO postings_fts(postings_fts) VALUES('rebuild')")
    conn.commit()
    return ids


def _select(conn, q):
    c = compile_query(q)
    return {
        r["id"] for r in db.all_rows(conn, f"SELECT p.id FROM postings p WHERE {c.where}", c.params)
    }


@pytest.mark.parametrize(
    "q, expect",
    [
        ("base >= 120k", {"nyc_big", "sf_est", "delisted", "android"}),
        ("base > 110k base < 130k", {"sf_est", "android"}),
        ("base < 85k", {"cheap"}),  # NULL base is neither < nor >= anything
        ("category:big_tech", {"nyc_big", "delisted"}),
        ("metro:nyc", {"nyc_big"}),
        ("metro:dc", {"dc_clear", "applied"}),
        ("beats_baseline:clearly_better", {"nyc_big", "sf_est", "delisted", "android"}),
        ("beats_baseline:clearly_better -delisted", {"nyc_big", "sf_est", "android"}),
        (
            "NOT requires_clearance",
            {"nyc_big", "sf_est", "cheap", "applied", "dismissed", "delisted", "nocomp", "android"},
        ),
        ("requires_clearance", {"dc_clear"}),
        ("applied", {"applied"}),
        (
            "status!=dismissed",
            {"nyc_big", "sf_est", "dc_clear", "cheap", "applied", "delisted", "nocomp", "android"},
        ),
        ('company:"capital one"', {"applied"}),
        ("company:acme", {"cheap", "dismissed"}),
        ("tag:dotnet", {"cheap"}),
        ("tag:java", {"dc_clear", "applied"}),
        ("fit >= 0.7", {"nyc_big", "dc_clear", "applied"}),
        ("posted >= 2026-08-10", {"nyc_big", "dc_clear", "cheap", "android"}),
        ("posted < 2026-08-01", {"sf_est", "delisted"}),
        ("has_posted_comp", {"nyc_big"}),
        (
            "NOT has_posted_comp",
            {
                "sf_est",
                "dc_clear",
                "cheap",
                "applied",
                "dismissed",
                "delisted",
                "nocomp",
                "android",
            },
        ),
        (
            "beats_baseline:clearly_better OR (fit >= 0.7 AND NOT applied)",
            {"nyc_big", "sf_est", "delisted", "android", "dc_clear"},
        ),
        ("android developer", {"android"}),  # free text → FTS over title
        ('"new grad" metro:pittsburgh', {"android"}),
        ("metro:nyc OR metro:sf", {"nyc_big", "sf_est"}),
        (
            "base >= 100k category:big_tech metro:sf",
            set(),
        ),  # AND of incompatible terms selects nothing
    ],
)
def test_query_selects_exactly_the_right_rows(conn, corpus, q, expect):
    got = _select(conn, q)
    names = {v: k for k, v in corpus.items()}
    assert {names[i] for i in got} == expect, f"{q!r} → {sorted(names[i] for i in got)}"


def test_empty_query_is_everything(conn, corpus):
    assert _select(conn, "") == set(corpus.values())


def test_k_suffix_and_plain_numbers_agree(conn, corpus):
    assert _select(conn, "base >= 120k") == _select(conn, "base >= 120000")


def test_null_comp_rows_are_excluded_from_every_numeric_comparison(conn, corpus):
    for q in ("base >= 0", "base < 1000000", "fit >= 0", "fit < 1"):
        assert corpus["nocomp"] not in _select(conn, q), q
