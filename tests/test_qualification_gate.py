"""D63: qualification is a gate, evaluated before scoring. Each band pinned."""

import pytest

from radar.parse.quals import extract_min_years, title_hard_seniority, title_newgrad_signal
from radar.score.scope import classify_scope

# --- extraction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, want",
    [
        ("4+ years of experience in software development", 4.0),
        ("minimum of 3 years experience with Java", 3.0),
        ("at least 5 years of professional experience", 5.0),
        ("2-4 years of relevant experience", 2.0),
        ("0-2 years of experience", 0.0),
        ("5 or more years of industry experience", 5.0),
        ("min. 2 yrs experience", 2.0),
        ("You have 2+ years of experience, including internship experience", 0.0),
        ("no prior experience required", 0.0),
        ("New grads are welcome to apply", 0.0),
        ("we value curiosity", None),
    ],
)
def test_extract_min_years(text, want):
    got, _max, _ev = extract_min_years(None, text)
    assert got == want, (text, got)


def test_smallest_stated_minimum_wins_and_ranges_keep_max():
    lo, hi, ev = extract_min_years(
        None, "0-2 years of experience. 5+ years of experience preferred for senior track."
    )
    assert lo == 0.0 and hi == 2.0 and "0-2" in ev


# --- title hard block --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, blocked",
    [
        ("Senior Software Engineer", True),
        ("Software Engineer II, Early Career", True),
        ("Software Engineer III", True),
        ("Staff Engineer", True),
        ("Sr. Data Engineer", True),
        ("Principal Architect", True),
        ("Engineering Manager", True),
        ("Tech Lead", True),
        ("Software Engineer 2", True),
        ("Software Engineer L4", True),
        ("Software Development Engineer I", False),
        ("Software Engineer, New Grad", False),
        ("Junior Developer", False),
        ("Software Engineer, Early Career", False),
        ("Graduate Software Engineer", False),
    ],
)
def test_title_hard_seniority(title, blocked):
    assert (title_hard_seniority(title) is not None) == blocked, title


def test_newgrad_title_signal():
    for t in (
        "Software Engineer, New Grad",
        "Entry Level Developer",
        "University Grad SWE",
        "Campus Hire — Backend",
        "Early Career Engineer",
        "Graduate Analyst",
    ):
        assert title_newgrad_signal(t), t
    assert not title_newgrad_signal("Software Engineer, Payments")


# --- the gate bands ----------------------------------------------------------------------------


def _p(**over):
    base = {
        "title": "Software Engineer, New Grad",
        "role_family": "software_engineering",
        "seniority": "new_grad",
        "employment_type": "full_time",
        "is_new_grad": 1,
        "min_years_experience": None,
        "tech_tags_json": '["python"]',
    }
    base.update(over)
    return base


def test_band_eligible_0_to_1_years(tmp_project):
    for years in (None, 0.0, 1.0):
        r = classify_scope(_p(min_years_experience=years), None, tmp_project)
        assert r.in_scope and not r.is_stretch, years


def test_band_stretch_2_years_out_of_queue(tmp_project):
    r = classify_scope(_p(min_years_experience=2.0), None, tmp_project)
    assert r.in_scope and r.is_stretch
    from radar.score.engine import _queue_action
    from radar.score.views import suppression_reason

    assert (
        suppression_reason(
            {
                "is_cluster_canonical": 1,
                "in_scope": 1,
                "is_stretch": 1,
                "floor_result": "pass",
                "status": "new",
            }
        )
        == "stretch"
    )

    class F:  # minimal fit shim
        fit_score = 0.8
        gaps = []

    class S:
        referral_likelihood = "unlikely"

    assert _queue_action(True, F(), S(), 0.9, {"is_stretch": 1}, "clearly_better") is None


def test_band_3plus_suppressed_with_reason(tmp_project):
    r = classify_scope(_p(min_years_experience=4.0), None, tmp_project)
    assert not r.in_scope and "4+ years required" in r.scope_reason


def test_band_advanced_degree_suppressed(tmp_project):
    r = classify_scope(_p(requires_advanced_degree=1), None, tmp_project)
    assert not r.in_scope and "MS/PhD" in r.scope_reason


def test_band_null_years_without_newgrad_signal_is_qualification_unknown(tmp_project):
    r = classify_scope(
        _p(title="Software Engineer, Payments", seniority="unknown", is_new_grad=0),
        None,
        tmp_project,
    )
    assert not r.in_scope and "qualification unknown" in r.scope_reason
    # …but a new-grad title signal makes the same null row eligible
    r2 = classify_scope(
        _p(title="Software Engineer, Early Career", seniority="unknown", is_new_grad=0),
        None,
        tmp_project,
    )
    assert r2.in_scope


def test_title_seniority_beats_description(tmp_project):
    r = classify_scope(
        _p(title="Software Engineer II, Early Career", min_years_experience=0.0), None, tmp_project
    )
    assert not r.in_scope and "title seniority" in r.scope_reason


def test_stretch_never_notifies(conn, tmp_project, monkeypatch):
    from radar import db
    from radar.notify.engine import find_candidates
    from radar.util import utcnow_iso

    now = utcnow_iso()
    db.insert_posting(
        conn,
        {
            "source": "company_direct",
            "source_provider": "greenhouse",
            "source_slug": "x",
            "source_job_id": "st1",
            "apply_url": "https://x/st1",
            "first_seen_at": now,
            "last_seen_at": now,
            "company_name": "Acme",
            "title": "SWE",
            "in_scope": 1,
            "is_stretch": 1,
            "floor_result": "pass",
            "is_cluster_canonical": 1,
            "beats_baseline": "clearly_better",
            "status": "new",
        },
    )
    cands = find_candidates(conn, tmp_project, since="2000-01-01")
    assert all(c.posting["source_job_id"] != "st1" for c in cands)


# --- start-date gate (D64) ---------------------------------------------------------------------

NOTION_SENTENCE = "**This is NOT a new grad role! This role is for candidates with 0-2 YOE and can start full time right away.**"


def test_the_exact_notion_sentence_is_incompatible():
    from radar.parse.quals import start_date_signal

    flag, ev = start_date_signal(NOTION_SENTENCE, earliest_start=(2027, 5))
    assert flag == "incompatible"
    assert "not a new grad role" in ev.lower() or "right away" in ev.lower()


@pytest.mark.parametrize(
    "text, want",
    [
        ("This is not a new graduate role.", "incompatible"),
        ("You can start immediately.", "incompatible"),
        ("Immediate start required.", "incompatible"),
        ("Candidates must be immediately available.", "incompatible"),
        ("Currently enrolled students are not eligible.", "incompatible"),
        ("Must be able to start by January 2027.", "incompatible"),
        ("Must be able to start by June 2027.", None),  # after May 15 2027 — fine
        ("Class of 2027 candidates are encouraged to apply.", "compatible"),
        ("This role is for 2027 graduates with a Summer 2027 start.", "compatible"),
        ("Graduating in Spring 2027? Apply here.", "compatible"),
        ("December 2026 or Spring 2027 grads welcome.", "compatible"),
        ("We ship fast and value ownership.", None),
    ],
)
def test_start_date_signal(text, want):
    from radar.parse.quals import start_date_signal

    flag, _ = start_date_signal(text, earliest_start=(2027, 5))
    assert flag == want, text


def test_body_beats_title_for_start_date(tmp_project):
    """'Early Career' in the title means nothing against an explicit immediate-start body."""
    r = classify_scope(
        _p(
            title="Software Engineer, Early Career",
            min_years_experience=0.0,
            start_flag="incompatible",
            start_evidence="not a new grad role",
        ),
        None,
        tmp_project,
    )
    assert not r.in_scope and "start date incompatible" in r.scope_reason


def test_compatible_start_signal_lets_null_years_through(tmp_project):
    r = classify_scope(
        _p(
            title="Software Engineer, Payments",
            seniority="unknown",
            is_new_grad=0,
            start_flag="compatible",
        ),
        None,
        tmp_project,
    )
    assert r.in_scope


def test_ingest_derives_the_start_flag(tmp_project):
    from radar.parse.posting import derive_from_description

    out = derive_from_description(
        NOTION_SENTENCE + " 0-2 years of experience.", "Software Engineer, Early Career"
    )
    assert out["start_flag"] == "incompatible" and out["min_years_experience"] == 0.0


# --- unenriched parking (D64) ------------------------------------------------------------------


def test_unenriched_rows_park_in_needs_review_and_never_alert(conn, tmp_project):
    from radar import db
    from radar.notify.engine import find_candidates
    from radar.score.engine import _queue_action
    from radar.util import utcnow_iso

    class F:
        fit_score = 0.8
        gaps = []

    class S:
        referral_likelihood = "unlikely"

    assert (
        _queue_action(True, F(), S(), 0.9, {"has_requirements": 0}, "clearly_better")
        == "needs_review"
    )
    assert (
        _queue_action(True, F(), S(), 0.9, {"has_requirements": 1}, "clearly_better")
        == "apply_today"
    )
    assert (
        _queue_action(True, F(), S(), 0.5, {"has_requirements": 0}, "clearly_better")
        == "needs_review"
    )
    now = utcnow_iso()
    db.insert_posting(
        conn,
        {
            "source": "company_direct",
            "source_provider": "greenhouse",
            "source_slug": "x",
            "source_job_id": "ur1",
            "apply_url": "https://x/ur1",
            "first_seen_at": now,
            "last_seen_at": now,
            "company_name": "Acme",
            "title": "SWE, New Grad",
            "in_scope": 1,
            "is_new_grad": 1,
            "is_dream_list": 1,
            "role_family": "software_engineering",
            "seniority": "new_grad",
            "floor_result": "pass",
            "is_cluster_canonical": 1,
            "beats_baseline": "clearly_better",
            "has_requirements": 0,
            "status": "new",
        },
    )
    cands = find_candidates(conn, tmp_project, since="2000-01-01")
    mine = [c for c in cands if c.posting["source_job_id"] == "ur1"]
    assert mine and mine[0].tier == "none" and mine[0].trigger == "unenriched_needs_review"
