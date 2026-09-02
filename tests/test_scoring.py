"""Scope rules, comp cascade, engine invariants (§1c, §2, §9d, §10, §11)."""

import json

from radar import db
from radar.enrich.comp_model import PeerModel, estimate_comp
from radar.score.engine import decision_calendar, score_all, switching_friction
from radar.score.scope import classify_scope
from radar.util import utcnow_iso


def _p(**over):
    base = {
        "role_family": "software_engineering",
        "seniority": "new_grad",
        "employment_type": "full_time",
        "is_new_grad": 1,
        "min_years_experience": None,
        "requires_clearance": 0,
        "requires_advanced_degree": 0,
        "graduation_window": None,
        "sponsorship": "unknown",
        "is_international_only": 0,
        "primary_metro": "new_york",
        "base_posted_min": None,
        "base_posted_max": None,
        "target_category": "big_tech_swe",
        "company_tier": 1,
    }
    base.update(over)
    return base


def test_scope_rules(tmp_project):
    cfg = tmp_project
    assert classify_scope(_p(), None, cfg).in_scope
    assert not classify_scope(_p(seniority="senior"), None, cfg).in_scope
    assert "internship" in classify_scope(_p(employment_type="internship"), None, cfg).hard_blockers
    assert (
        "active clearance required"
        in classify_scope(_p(requires_clearance=1), None, cfg).hard_blockers
    )
    assert (
        "MS/PhD required" in classify_scope(_p(requires_advanced_degree=1), None, cfg).hard_blockers
    )
    r = classify_scope(_p(graduation_window="2026"), None, cfg)
    assert any("graduation window" in b for b in r.hard_blockers)
    assert classify_scope(_p(graduation_window="2026-2027"), None, cfg).in_scope
    assert classify_scope(_p(min_years_experience=3), None, cfg).hard_blockers
    s = classify_scope(_p(seniority="mid", min_years_experience=2), None, cfg)
    assert s.in_scope and s.is_stretch
    assert classify_scope(_p(role_family="it_support"), None, cfg).floor_fail_reasons
    assert classify_scope(_p(role_family="not_a_role"), None, cfg).scope_reason.startswith(
        "talent pool"
    )
    assert not classify_scope(_p(is_international_only=1), None, cfg).in_scope


def test_floor_and_exemption(tmp_project):
    cfg = tmp_project
    below = int(cfg.comp_gates.hard_floor) - 5000
    fail = classify_scope(
        _p(base_posted_min=60000, base_posted_max=below), {"slug": "x", "is_dream_list": 0}, cfg
    )
    assert fail.floor_result == "fail"
    ex = classify_scope(
        _p(base_posted_min=60000, base_posted_max=below),
        {"slug": "google", "is_dream_list": 1},
        cfg,
    )
    assert ex.floor_result == "exempt"


def test_quant_cap_flag(tmp_project):
    r = classify_scope(_p(role_family="quant"), None, tmp_project)
    assert r.quant_capped and not r.in_scope
    r2 = classify_scope(_p(), {"slug": "jane-street", "is_quant_trading_firm": 1}, tmp_project)
    assert r2.quant_capped and r2.in_scope  # a SWE seat at a quant firm stays in the list, capped


def _insert(conn, **over):
    now = utcnow_iso()
    vals = {
        "source": "company_direct",
        "source_provider": "greenhouse",
        "source_slug": "x",
        "source_job_id": over.pop("jid", "1"),
        "apply_url": "https://x/1",
        "first_seen_at": now,
        "last_seen_at": now,
        "company_name": "X",
        "title": "Software Engineer, New Grad",
        "title_normalized": "software engineer new grad",
        "role_family": "software_engineering",
        "role_subfamily": "generalist",
        "seniority": "new_grad",
        "is_new_grad": 1,
        "employment_type": "full_time",
        "primary_metro": "new_york",
        "locations_json": json.dumps(
            [
                {
                    "raw": "New York, NY",
                    "kind": "metro",
                    "metro": "new_york",
                    "premium_bucket": "new_york",
                    "tax_jurisdiction": "ny_nyc",
                    "metro_name": "New York, NY",
                }
            ]
        ),
        "metros_json": '["new_york"]',
        "tech_tags_json": "[]",
        "description_md": "We build things in Java and Python.",
        "description_fetched": 1,
    }
    vals.update(over)
    return db.insert_posting(conn, vals)


def test_comp_cascade_posted_then_peer(conn, tmp_project):
    # seed peer data: 6 NYC new-grad SWE postings with posted ranges
    for i in range(6):
        _insert(
            conn,
            jid=f"p{i}",
            base_posted_min=110000,
            base_posted_max=140000,
            company_tier=2,
            target_category="big_tech_swe",
        )
    pid = _insert(conn, jid="q", company_tier=2, target_category="big_tech_swe")
    peers = PeerModel(conn)
    posted = estimate_comp(
        {
            "base_posted_min": 100000,
            "base_posted_max": 120000,
            "comp_source": "posted_range",
            "target_category": "big_tech_swe",
            "seniority": "new_grad",
            "primary_metro": "new_york",
        },
        None,
        peers,
        tmp_project,
    )
    assert (
        posted.comp_source == "posted_range"
        and posted.comp_confidence >= 0.9
        and 100000 <= posted.base_est <= 120000
    )
    row = dict(db.one(conn, "SELECT * FROM postings WHERE id = ?", (pid,)))
    est = estimate_comp(row, {"tier": 2, "target_category": "big_tech_swe"}, peers, tmp_project)
    assert est.comp_source.startswith("peer_model") and est.comp_confidence < 0.5
    assert 100000 <= est.base_est <= 150000
    assert est.tc_year1_est > est.base_est


def test_score_all_invariants(conn, tmp_project):
    cfg = tmp_project
    cfg.location_utility_premium.new_york = 40_000  # the parity-in-NYC case needs a stated premium
    _insert(
        conn,
        jid="a",
        base_posted_min=150000,
        base_posted_max=170000,
        comp_source="posted_range",
        company_tier=1,
        is_dream_list=1,
        target_category="big_tech_swe",
        company_id=5,
    )
    _insert(
        conn,
        jid="b",
        base_posted_min=90000,
        base_posted_max=95000,
        comp_source="posted_range",
        company_tier=2,
        target_category="fintech_infrastructure",
        title="Software Engineer I",
        title_normalized="software engineer i",
    )
    _insert(
        conn,
        jid="c",
        seniority="senior",
        title="Senior Software Engineer",
        title_normalized="senior software engineer",
    )
    _insert(
        conn,
        jid="d",
        base_posted_min=60000,
        base_posted_max=int(cfg.comp_gates.hard_floor) - 5000,  # below the floor
        comp_source="posted_range",
        company_tier=3,
        target_category="other",
    )
    _insert(
        conn,
        jid="e",
        requires_clearance=1,
        base_posted_min=120000,
        base_posted_max=140000,
        comp_source="posted_range",
        company_tier=1,
        target_category="defense_and_gov_tech",
    )
    st = score_all(conn, cfg)
    assert st["scored"] == 5
    rows = {r["source_job_id"]: dict(r) for r in db.all_rows(conn, "SELECT * FROM postings")}
    assert rows["a"]["beats_baseline"] == "clearly_better"
    # D64: an unenriched row is parked as needs_review behind every enriched row, however good
    assert (
        rows["a"]["queue_action"] == "needs_review" and rows["a"]["apply_priority_rank"] is not None
    )
    conn.execute(
        "UPDATE postings SET has_requirements = 1, needs_rescore = 1 WHERE source_job_id = 'a'"
    )
    score_all(conn, cfg, only_unscored=True)
    rows = {r["source_job_id"]: dict(r) for r in db.all_rows(conn, "SELECT * FROM postings")}
    assert rows["a"]["queue_action"] == "apply_today" and rows["a"]["apply_priority_rank"] == 1
    assert rows["b"]["beats_baseline"] in (
        "arguably_better",
        "clearly_better",
    )  # a parity-level NYC posting with a stated premium must not be `worse`
    assert (
        rows["c"]["in_scope"] == 0
        and rows["c"]["priority"] == 0
        and rows["c"]["apply_priority_rank"] is None
    )
    assert rows["d"]["floor_result"] == "fail" and rows["d"]["apply_priority_rank"] is None
    assert rows["e"]["in_scope"] == 0 and "clearance" in rows["e"]["scope_reason"]
    x = json.loads(db.get_doc(conn, rows["a"]["id"])["score_explanation_json"])
    assert set(x["sub_scores"]) == {
        "comp_score",
        "career_capital_score",
        "fit_score",
        "winnability_score",
        "location_score",
        "culture_score",
    }
    assert abs(sum(v["weight"] for v in x["sub_scores"].values()) - 1.0) < 1e-9
    assert x["location"]["location_utility_premium"] == cfg.location_utility_premium.new_york
    assert x["fit"]["gaps"] and any(
        "Big Tech stack" in g["gap"] for g in x["fit"]["gaps"]
    )  # §1b named
    assert 0 < rows["a"]["p_offer"] < 0.3
    assert rows["a"]["queue_action"] in (
        "apply_today",
        "apply_this_week",
        "get_referral_first",
        "watch",
    )


def test_workflow_removes_from_queue(conn, tmp_project):
    pid = _insert(
        conn,
        jid="w",
        base_posted_min=150000,
        base_posted_max=170000,
        comp_source="posted_range",
        company_tier=1,
        target_category="big_tech_swe",
    )
    score_all(conn, tmp_project)
    assert db.scalar(conn, "SELECT apply_priority_rank FROM postings WHERE id = ?", (pid,)) == 1
    db.update(conn, "postings", pid, {"status": "applied"})
    score_all(conn, tmp_project)
    assert db.scalar(conn, "SELECT apply_priority_rank FROM postings WHERE id = ?", (pid,)) is None
    assert (
        db.scalar(conn, "SELECT beats_baseline FROM postings WHERE id = ?", (pid,))
        == "clearly_better"
    )  # still scored, just not queued


def test_switching_friction_curve_and_calendar(tmp_project):
    from datetime import date

    cfg = tmp_project
    cfg.switching_friction.signing_bonus_clawback = 5000  # the example config ships every term at 0
    cfg.switching_friction.goodwill_cost_at_start = 8000
    early = switching_friction(cfg, date(2026, 10, 1))
    late = switching_friction(cfg, date(2027, 5, 1))
    assert early["total"] < late["total"]
    assert early["items"]["signing_bonus_clawback"] == 5000
    assert early["cheap_zone"] and not late["cheap_zone"]
    today = date(2026, 8, 20)
    cal = decision_calendar(cfg, today)
    assert cal["days_to_deadline"] == (cfg.baseline.decision_deadline - today).days
    assert "competing offers" in cal["deadline_note"]
    assert len(cal["switching_window"]["curve"]) > 10


def test_title_window_and_presets(conn, tmp_project):
    from radar.parse.posting import _title_window

    assert _title_window("New Grad 2026: Software Engineer", True) == "2026"
    assert _title_window("2027 Software Engineer Program - Full-Time", True) == "2027"
    assert _title_window("Software Engineer (2026-2027 grads)", True) == "2026-2027"
    assert _title_window("Software Engineer II", False) is None
    names = {
        r["name"] for r in db.all_rows(conn, "SELECT name FROM saved_filters WHERE is_preset = 1")
    }
    assert {
        "Apply First",
        "Clearly Better",
        "Arguably Better (needs a call)",
        "Dream List",
        "Watchlist",
        "Everything (Master)",
        "Floor Failures Audit",
        "High-Pay International",
    } <= names
    from radar.query import compile_query

    for r in db.all_rows(conn, "SELECT query FROM saved_filters"):
        c = compile_query(r["query"])
        db.all_rows(conn, f"SELECT p.id FROM postings p WHERE {c.where} LIMIT 1", c.params)


def test_confirmed_dead_link_leaves_today_but_unverified_stays(conn, tmp_project):
    """Queue policy: only a CONFIRMED dead link is demoted (to the verify-before-dismissing group);
    `unverified` — which after D54 includes JS-rendered boards that are genuinely open — keeps its
    bucket. `restore_link` puts a demoted row back."""
    from radar.score.engine import rank_queue
    from radar.util import utcnow_iso
    from radar.workflow import apply_action

    now = utcnow_iso()

    def mk(jid, status):
        return db.insert_posting(
            conn,
            {
                "source": "company_direct",
                "source_provider": "greenhouse",
                "source_slug": "x",
                "source_job_id": jid,
                "apply_url": f"https://x/{jid}",
                "first_seen_at": now,
                "last_seen_at": now,
                "company_name": "Acme",
                "title": "Software Engineer",
                "in_scope": 1,
                "floor_result": "pass",
                "is_cluster_canonical": 1,
                "priority": 0.9,
                "queue_action": "apply_today",
                "url_status": status,
            },
        )

    dead, unverified, live = mk("d", "dead"), mk("u", "unverified"), mk("l", "live")
    rank_queue(conn, tmp_project)
    acts = {
        r["id"]: r["queue_action"]
        for r in db.all_rows(conn, "SELECT id, queue_action FROM postings")
    }
    assert acts[dead] == "verify_link"
    assert acts[unverified] == "apply_today" and acts[live] == "apply_today"
    assert (
        db.scalar(conn, "SELECT apply_priority_rank FROM postings WHERE id = ?", (dead,))
        is not None
    )  # still ranked, not hidden
    # one-tap restore: manual verdict, back into Today after rescore
    r = apply_action(conn, dead, "restore_link", via="test")
    assert r.ok
    row = db.one(
        conn,
        "SELECT url_status, url_verify_method, needs_rescore FROM postings WHERE id = ?",
        (dead,),
    )
    assert (
        row["url_status"] == "live"
        and row["url_verify_method"] == "manual"
        and row["needs_rescore"] == 1
    )
    conn.execute(
        "UPDATE postings SET queue_action = 'apply_today' WHERE id = ?", (dead,)
    )  # what scoring recomputes
    rank_queue(conn, tmp_project)
    assert (
        db.scalar(conn, "SELECT queue_action FROM postings WHERE id = ?", (dead,)) == "apply_today"
    )
    assert (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM posting_events WHERE posting_id = ? AND event_type = 'link_restored'",
            (dead,),
        )
        == 1
    )
