"""§10–§11: six sub-scores → composite → three-state verdict → urgency → priority → queue action.

Deterministic and explainable. Every posting gets `score_explanation_json` with the inputs behind
each number. LLM enrichment (radar.enrich.pipeline) runs separately and only refines inputs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from radar import db
from radar.config import Config
from radar.enrich.comp_model import PeerModel, estimate_comp
from radar.enrich.resume import fit_for_posting, load_resume
from radar.score.location_value import compute_location_value, three_state_verdict
from radar.score.scope import classify_scope
from radar.util import parse_dt, utcnow, utcnow_iso

log = logging.getLogger("radar.score")
SCORE_VERSION = "2026-08-30.1"  # D64: start-date gate + unenriched parking
DEFAULT_DAYS_TO_CLOSE = 45.0
SEASON_START = date(2026, 8, 1)


@dataclass
class ScoreStats:
    scored: int = 0
    in_scope: int = 0
    floor_pass: int = 0
    queue: int = 0
    embedded: int = 0
    elapsed_s: float = 0.0
    verdicts: dict[str, int] = field(default_factory=dict)
    funnel: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


# ---------------------------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------------------------


def comp_score(effective_value: float | None, confidence: float, cfg: Config) -> tuple[float, str]:
    g = cfg.comp_gates
    if effective_value is None:
        return 0.45, "no comp signal → neutral 0.45"
    ev = effective_value
    if ev <= g.hard_floor:
        raw = 0.3 * ev / g.hard_floor
    elif ev <= g.parity:
        raw = 0.3 + 0.3 * (ev - g.hard_floor) / (g.parity - g.hard_floor)
    elif ev <= g.instant_yes:
        raw = 0.6 + 0.15 * (ev - g.parity) / max(1, g.instant_yes - g.parity)
    else:
        raw = min(1.0, 0.75 + 0.25 * (ev - g.instant_yes) / 40_000)
    score = confidence * raw + (1 - confidence) * 0.5
    return round(
        score, 3
    ), f"effective ${ev:,.0f} → raw {raw:.2f}, shrunk toward 0.5 by confidence {confidence:.0%}"


RANK_SCORE = {1: 1.0, 2: 0.9, 3: 0.8, 4: 0.65, 5: 0.5, 6: 0.35, 7: 0.3, 8: 0.25}


def career_capital_score(
    p: dict[str, Any], company: dict[str, Any] | None, cfg: Config
) -> tuple[float, str]:
    cat = p.get("target_category") or "other"
    rank = cfg.target_ranking.get(cat, 8)
    s = RANK_SCORE.get(rank, 0.25)
    parts = [f"category #{rank} ({cat}) {s:.2f}"]
    tier = p.get("company_tier")
    if tier == 1:
        s += 0.12
        parts.append("Tier-1 +0.12")
    elif tier == 2:
        s += 0.05
        parts.append("Tier-2 +0.05")
    if (
        p.get("role_subfamily") in ("backend", "distributed_infra", "generalist", "fullstack")
        and p.get("role_family") == "software_engineering"
    ):
        s += 0.04
        parts.append("technical depth +0.04")
    if p.get("program_type") in ("rotational", "new_grad_program", "analyst_program"):
        s += 0.04
        parts.append("structured program (mentorship density) +0.04")
    if p.get("is_dream_list"):
        s *= cfg.modifiers.dream_list_career_capital_multiplier
        parts.append(f"dream list ×{cfg.modifiers.dream_list_career_capital_multiplier}")
    return round(min(1.0, s), 3), "; ".join(parts)


def winnability_score(
    p: dict[str, Any], scope: Any, company: dict[str, Any] | None, cfg: Config
) -> tuple[float, str]:
    m = cfg.modifiers
    s = 0.5
    parts = ["base 0.50"]
    tier = p.get("company_tier")
    if tier == 1:
        s -= 0.12
        parts.append("Tier-1 selectivity −0.12")
    elif tier == 3 or tier is None:
        s += 0.08
        parts.append("less selective +0.08")
    if p.get("program_type") in ("rotational", "new_grad_program", "analyst_program"):
        s += 0.10
        parts.append("large cohort program +0.10")
    if scope.is_new_grad:
        s += 0.10
        parts.append("explicit new-grad req +0.10")
    rl = scope.referral_likelihood
    if p.get("referral_secured"):
        s += 0.15
        parts.append("referral secured +0.15")
    elif rl == "likely":
        s += 0.08
        parts.append("referral likely +0.08")
    elif rl == "possible":
        s += 0.04
        parts.append("referral possible +0.04")
    if cfg.operator.gpa >= 3.5:
        s += 0.04
        parts.append("GPA screen passes +0.04")
    # LeetCode: capped share (§1c). medium → half of the cap.
    lc = {"weak": 0.25, "medium": 0.5, "strong": 1.0}.get(cfg.operator.leetcode_level, 0.5)
    lc_contrib = m.leetcode_max_share_of_winnability * lc
    s = s * (1 - m.leetcode_max_share_of_winnability) + lc_contrib
    parts.append(
        f"LeetCode {cfg.operator.leetcode_level} contributes {lc_contrib:.2f} (≤{m.leetcode_max_share_of_winnability:.0%})"
    )
    # (the old stretch ×0.85 winnability multiplier is gone — stretch is a gate now, D63)
    if scope.quant_capped:
        s = min(s, m.quant_trading_firm_winnability_cap)
        parts.append(f"quant seat → capped at {m.quant_trading_firm_winnability_cap} (§1c)")
    return round(max(0.02, min(1.0, s)), 3), "; ".join(parts)


LOCATION_SCORES = {
    "new_york": 1.0,
    "san_francisco": 0.9,
    "seattle": 0.85,
    "other_major_tech_hub": 0.6,
    "washington_dc": 0.5,
    "remote": 0.5,
    "elsewhere": 0.35,
}


def location_score(bucket: str | None, metro_name: str) -> tuple[float, str]:
    s = LOCATION_SCORES.get(bucket or "", 0.4)
    return s, f"{metro_name} ({bucket or 'unknown'}) → {s:.2f}; family proximity weight 0"


def culture_score(p: dict[str, Any], company: dict[str, Any] | None) -> tuple[float, str]:
    s = 0.6
    parts = ["base 0.60"]
    if company:
        if (company.get("repost_rate") or 0) > 0.3:
            s -= 0.1
            parts.append("chronic reposter −0.10")
        layoffs = company.get("layoff_history") or []
        if layoffs:
            s -= 0.1
            parts.append("layoff history −0.10")
        rto = (company.get("rto_policy") or "").lower()
        if "5" in rto or "full" in rto:
            s -= 0.05
            parts.append("5-day RTO −0.05")
    if p.get("work_mode") in ("hybrid", "remote"):
        s += 0.05
        parts.append(f"{p.get('work_mode')} +0.05")
    if p.get("repost_of_id"):
        s -= 0.05
        parts.append("this req is a repost −0.05")
    return round(max(0.0, min(1.0, s)), 3), "; ".join(parts)


# ---------------------------------------------------------------------------------------------
# EV, switching friction, urgency
# ---------------------------------------------------------------------------------------------


def switching_friction(cfg: Config, on: date | None = None) -> dict[str, Any]:
    """Itemized friction of walking away from the baseline offer on date `on` (default today). Rises convexly toward start."""
    rf = cfg.switching_friction
    on = on or date.today()
    sign = cfg.baseline.decision_deadline
    start = cfg.baseline.start_date
    total_days = max(1, (start - sign).days)
    elapsed = min(max((on - sign).days, 0), total_days)
    frac = elapsed / total_days
    goodwill = rf.goodwill_cost_at_signing + (
        rf.goodwill_cost_at_start - rf.goodwill_cost_at_signing
    ) * (frac**rf.curve_exponent)
    items = {
        "signing_bonus_clawback": float(rf.signing_bonus_clawback),
        "goodwill": round(goodwill),
        "university_channel": float(rf.university_channel_cost),
    }
    total = sum(items.values())
    return {
        "date": on.isoformat(),
        "days_until_start": (start - on).days,
        "fraction_of_window": round(frac, 3),
        "items": items,
        "total": round(total),
        "cheap_zone": on <= rf.cheap_zone_ends,
    }


def expected_value(
    p: dict[str, Any],
    *,
    effective_value: float | None,
    career_capital: float,
    winnability: float,
    prep_hours: float,
    same_market: bool,
    cfg: Config,
    friction_total: float,
) -> tuple[float | None, float, dict[str, Any]]:
    if effective_value is None:
        return None, 0.0, {"note": "no comp signal"}
    p_offer = 0.02 + 0.18 * winnability
    delta3 = cfg.ev.horizon_years * (effective_value - cfg.baseline.base_salary)
    cc_premium = (career_capital - 0.5) * 10 * cfg.ev.career_capital_premium_per_point
    upside = delta3 + cc_premium
    prep_cost = prep_hours * cfg.ev.hourly_opportunity_cost
    friction = friction_total + (cfg.switching_friction.same_market_penalty if same_market else 0)
    ev = p_offer * upside - prep_cost - p_offer * friction
    return (
        round(ev),
        round(p_offer, 3),
        {
            "p_offer": round(p_offer, 3),
            "three_year_effective_delta": round(delta3),
            "career_capital_premium": round(cc_premium),
            "prep_cost": round(prep_cost),
            "switching_friction_if_offer": round(friction),
            "note": "EV = P(offer) × (3-yr delta + career premium) − prep cost − P(offer) × switching friction",
        },
    )


def urgency_score(
    p: dict[str, Any], company: dict[str, Any] | None, *, first_drop: bool, today: date
) -> tuple[float, dict[str, Any]]:
    days_open = 0.0
    opened = parse_dt(p.get("posted_at") or p.get("first_seen_at"))
    if opened:
        days_open = max(0.0, (utcnow() - opened).total_seconds() / 86400)
    median_close = float((company or {}).get("median_days_to_close") or DEFAULT_DAYS_TO_CLOSE)
    closing_risk = min(1.0, days_open / median_close)
    deadline_prox = 0.0
    dl = parse_dt(p.get("application_deadline"))
    if dl:
        dleft = (dl.date() - today).days
        deadline_prox = (
            1.0 if dleft <= 7 else (0.7 if dleft <= 14 else (0.4 if dleft <= 30 else 0.1))
        )
    batch = (
        1.0
        if p.get("program_type") in ("rotational", "new_grad_program", "analyst_program")
        else 0.0
    )
    seasonal = 1.0 if date(today.year, 9, 1) <= today <= date(today.year, 12, 31) else 0.5
    u = (
        0.25
        + 0.25 * closing_risk
        + 0.2 * deadline_prox
        + 0.15 * batch
        + 0.05 * seasonal
        + (0.3 if first_drop else 0)
    )
    u = max(0.1, min(1.0, u))
    return round(u, 3), {
        "days_open": round(days_open, 1),
        "median_days_to_close": median_close,
        "closing_risk": round(closing_risk, 2),
        "deadline_proximity": deadline_prox,
        "rolling_vs_batch": "batch" if batch else "rolling",
        "first_drop": first_drop,
        "seasonal_position": seasonal,
        "estimated_days_to_close": round(max(0.0, median_close - days_open)),
    }


# ---------------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------------


def _primary_location(p: dict[str, Any]) -> dict[str, Any] | None:
    try:
        locs = json.loads(p.get("locations_json") or "[]")
    except json.JSONDecodeError:
        return None
    pm = p.get("primary_metro")
    for loc in locs:
        if pm and (loc.get("metro") == pm or loc.get("kind") == pm):
            return loc
    return locs[0] if locs else None


def _company_rows(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    out = {}
    for r in db.all_rows(conn, "SELECT * FROM companies"):
        d = db.row_to_dict(r) or {}
        out[r["id"]] = d
    return out


def _first_drops(conn: sqlite3.Connection) -> set[int]:
    """First new-grad engineering req of the season per Tier-1/dream company (§11)."""
    rows = db.all_rows(
        conn,
        "SELECT id FROM postings p WHERE id IN (SELECT MIN(id) FROM postings WHERE company_id IS NOT NULL AND is_new_grad = 1 "
        "AND role_family IN ('software_engineering','ml_ai','data_engineering','devops_sre','security') AND (company_tier = 1 OR is_dream_list = 1) "
        "AND COALESCE(posted_at, first_seen_at) >= ? GROUP BY company_id)",
        (SEASON_START.isoformat(),),
    )
    return {r["id"] for r in rows}


def score_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    run_id: int | None = None,
    only_unscored: bool = False,
    ids: list[int] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    t0 = time.monotonic()
    stats = ScoreStats()
    companies = _company_rows(conn)
    peers = PeerModel(conn)
    from radar.enrich.lca import LcaPrior

    lca = LcaPrior(conn)
    resume = load_resume(cfg)
    today = date.today()
    friction = switching_friction(cfg, today)
    first_drops = _first_drops(conn)
    where = "1=1"
    params: list[Any] = []
    if ids:
        where = f"p.id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    elif only_unscored:
        where = "(p.scored_at IS NULL OR p.score_version != ? OR p.needs_rescore = 1)"
        params = [SCORE_VERSION]
    elif not full:
        # "eligible" mode (default for rescore): rows that could possibly enter the queue, plus anything
        # currently holding a score, so weight/config edits finish in seconds. `full=True` after rule edits.
        where = (
            "(p.seniority NOT IN ('senior','staff','principal','manager','executive','internship') "
            "AND p.role_family IN ('software_engineering','ml_ai','data_engineering','devops_sre','security','unknown') "
            "AND COALESCE(p.employment_type,'') != 'internship') OR p.composite_score > 0 OR p.scored_at IS NULL"
        )
    cols = (
        "id, company_id, company_name, title, title_normalized, role_family, role_subfamily, seniority, employment_type, is_new_grad, "
        "min_years_experience, requires_clearance, requires_advanced_degree, graduation_window, sponsorship, is_international_only, start_flag, start_evidence, "
        "primary_metro, locations_json, base_posted_min, base_posted_max, comp_source, target_category, company_tier, is_dream_list, "
        "program_type, tech_tags_json, referral_secured, same_market_as_baseline_offer, work_mode, repost_of_id, posted_at, first_seen_at, "
        "last_seen_at, application_deadline, status, snooze_until, is_cluster_canonical, delisted_at, override_floor, "
        "description_fetched, scored_at, score_version, has_requirements, has_llm_fit"
    )
    rows = [
        dict(r) for r in db.all_rows(conn, f"SELECT {cols} FROM postings p WHERE {where}", params)
    ]
    stats.funnel["postings"] = len(rows)
    # embeddings for in-scope rows only (two-stage funnel)
    emb_sim: dict[int, float] = {}
    try:
        from radar.enrich import embed

        if resume and embed.available():
            cache = embed.EmbeddingCache(cfg)
            from radar.enrich.resume import resume_embedding_text

            rvec = cache.get(resume_embedding_text(resume))
            cand = [
                r
                for r in rows
                if r.get("description_fetched")
                and classify_scope(r, companies.get(r["company_id"]), cfg).in_scope
            ]
            descs: dict[int, str] = {}
            for i in range(0, len(cand), 900):
                chunk = cand[i : i + 900]
                q = ",".join("?" * len(chunk))
                for d in db.all_rows(
                    conn,
                    f"SELECT posting_id AS id, description_md FROM posting_docs WHERE posting_id IN ({q})",
                    [c["id"] for c in chunk],
                ):
                    descs[d["id"]] = d["description_md"] or ""
            texts = [
                embed.posting_text(r["title"], descs.get(r["id"], ""), r["company_name"])
                for r in cand
            ]
            for r, v in zip(cand, cache.get_many(texts), strict=True):
                emb_sim[r["id"]] = embed.cosine(rvec, v)
            stats.embedded = len(cand)
    except Exception as e:
        log.warning("embedding step skipped: %s", e)
    # LLM-extracted requirements (docs table) for rows that have them
    req_ids = [r["id"] for r in rows if r.get("has_requirements")]
    reqs: dict[int, dict[str, Any]] = {}
    for i in range(0, len(req_ids), 900):
        chunk = req_ids[i : i + 900]
        for d in db.all_rows(
            conn,
            f"SELECT posting_id, requirements_json FROM posting_docs WHERE posting_id IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            with contextlib.suppress(json.JSONDecodeError):
                reqs[d["posting_id"]] = json.loads(d["requirements_json"] or "{}")
    now = utcnow_iso()
    w = cfg.weights
    updates: list[tuple[Any, ...]] = []
    light: list[tuple[Any, ...]] = []
    docs: list[tuple[int, str, str]] = []
    for p in rows:
        company = companies.get(p["company_id"]) if p.get("company_id") else None
        scope = classify_scope(p, company, cfg)
        # Out-of-scope rows that can never enter the queue (wrong seniority / non-engineering) get
        # scope fields only — no comp / fit work. Hard-blocked in-scope-shaped rows are scored fully
        # so the audit views still show what they would have been.
        if not scope.in_scope and (
            p.get("seniority")
            in ("senior", "staff", "principal", "manager", "executive", "internship")
            or scope.scope_reason.startswith(("step down", "not a software", "role family"))
        ):
            docs.append(
                (
                    p["id"],
                    json.dumps(
                        {
                            "score_version": SCORE_VERSION,
                            "scope": {"in_scope": False, "reason": scope.scope_reason},
                            "note": "out of scope — not scored",
                        }
                    ),
                    "{}",
                )
            )
            light.append(
                (
                    int(scope.in_scope),
                    scope.scope_reason,
                    json.dumps(scope.hard_blockers),
                    scope.floor_result,
                    json.dumps(scope.floor_fail_reasons),
                    int(scope.is_stretch),
                    int(scope.is_new_grad or bool(p.get("is_new_grad"))),
                    scope.referral_likelihood,
                    SCORE_VERSION,
                    now,
                    p["id"],
                )
            )
            stats.scored += 1
            continue
        comp = estimate_comp({**p, "is_stretch": scope.is_stretch}, company, peers, cfg, lca=lca)
        primary = _primary_location(p)
        nominal = comp.base_est
        loc = compute_location_value(nominal, primary, cfg) if nominal else None
        rank = cfg.target_ranking.get(p.get("target_category") or "other", 8)
        verdict = three_state_verdict(
            nominal_base=nominal,
            comp_confidence=comp.comp_confidence,
            comp_source=comp.comp_source,
            location=loc,
            company_tier=p.get("company_tier"),
            is_dream_list=bool(p.get("is_dream_list")),
            target_rank=rank,
            cfg=cfg,
        )
        fit = fit_for_posting(
            {**p, "requirements": reqs.get(p["id"])},
            resume,
            cfg,
            embedding_sim=emb_sim.get(p["id"]),
        )
        cs, cs_x = comp_score(loc.effective_value if loc else None, comp.comp_confidence, cfg)
        cc, cc_x = career_capital_score(p, company, cfg)
        wn, wn_x = winnability_score(p, scope, company, cfg)
        ls, ls_x = location_score(
            loc.premium_bucket if loc else (primary or {}).get("premium_bucket"),
            loc.metro_name if loc else ((primary or {}).get("metro_name") or "unknown"),
        )
        cu, cu_x = culture_score(p, company)
        composite = (
            w.comp_score * cs
            + w.career_capital_score * cc
            + w.fit_score * fit.fit_score
            + w.winnability_score * wn
            + w.location_score * ls
            + w.culture_score * cu
        )
        mods = []
        if comp.comp_confidence < cfg.modifiers.low_comp_confidence_threshold:
            composite *= cfg.modifiers.low_comp_confidence_multiplier
            mods.append(f"low comp confidence ×{cfg.modifiers.low_comp_confidence_multiplier}")
        if p.get("referral_secured"):
            composite *= cfg.modifiers.secured_referral_multiplier
            mods.append(f"secured referral ×{cfg.modifiers.secured_referral_multiplier}")
        composite = round(min(1.0, composite), 4)
        fd = p["id"] in first_drops
        urg, urg_x = urgency_score(p, company, first_drop=fd, today=today)
        same_market = bool(p.get("same_market_as_baseline_offer"))
        ev, p_offer, ev_x = expected_value(
            p,
            effective_value=loc.effective_value if loc else None,
            career_capital=cc,
            winnability=wn,
            prep_hours=fit.prep_hours_est,
            same_market=same_market,
            cfg=cfg,
            friction_total=friction["total"],
        )
        snoozed = bool(p.get("snooze_until") and p["snooze_until"] > now)
        active = (
            scope.in_scope
            and scope.floor_result in ("pass", "exempt")
            and p.get("status") not in ("applied", "dismissed")
            and not snoozed
            and bool(p.get("is_cluster_canonical", 1))
            and not p.get("delisted_at")
        )
        priority = round(composite * urg, 4) if active else 0.0
        explanation = {
            "score_version": SCORE_VERSION,
            "verdict": verdict.to_dict(),
            "location": loc.to_dict() if loc else None,
            "comp": {**comp.to_dict(), "explanation": comp.explanation},
            "scope": {
                "in_scope": scope.in_scope,
                "reason": scope.scope_reason,
                "hard_blockers": scope.hard_blockers,
                "floor": scope.floor_result,
                "floor_reasons": scope.floor_fail_reasons,
                "notes": scope.notes,
            },
            "sub_scores": {
                "comp_score": {"value": cs, "weight": w.comp_score, "why": cs_x},
                "career_capital_score": {
                    "value": cc,
                    "weight": w.career_capital_score,
                    "why": cc_x,
                },
                "fit_score": {
                    "value": fit.fit_score,
                    "weight": w.fit_score,
                    "why": fit.explanation,
                },
                "winnability_score": {"value": wn, "weight": w.winnability_score, "why": wn_x},
                "location_score": {"value": ls, "weight": w.location_score, "why": ls_x},
                "culture_score": {"value": cu, "weight": w.culture_score, "why": cu_x},
            },
            "modifiers": mods,
            "composite": composite,
            "urgency": {"value": urg, **urg_x},
            "ev": ev_x,
            "switching_friction_today": friction,
            "same_market_as_baseline_offer": same_market,
            "fit": fit.to_dict(),
            "active_in_queue": active,
        }
        queue_action = _queue_action(active, fit, scope, urg, p, verdict.state)
        docs.append(
            (
                p["id"],
                json.dumps(explanation, default=str),
                json.dumps(loc.to_dict() if loc else {}),
            )
        )
        updates.append(
            (
                int(scope.in_scope),
                scope.scope_reason,
                json.dumps(scope.hard_blockers),
                scope.floor_result,
                json.dumps(scope.floor_fail_reasons),
                int(scope.is_stretch),
                int(scope.is_new_grad or bool(p.get("is_new_grad"))),
                scope.referral_likelihood,
                comp.base_est,
                comp.base_est_low,
                comp.base_est_high,
                comp.signing_est,
                comp.bonus_target_pct_est,
                comp.equity_type,
                comp.equity_annual_est,
                comp.tc_year1_est,
                comp.comp_source if not p.get("base_posted_min") else p.get("comp_source"),
                comp.comp_confidence,
                loc.base_col_adjusted if loc else None,
                loc.base_after_tax_est if loc else None,
                loc.tax_delta if loc else None,
                loc.location_utility_premium if loc else None,
                loc.effective_value if loc else None,
                loc.real_terms_vs_baseline if loc else None,
                loc.tax_rate if loc else None,
                cs,
                cc,
                fit.fit_score,
                wn,
                ls,
                cu,
                composite,
                verdict.state,
                verdict.reason,
                urg,
                priority,
                ev,
                p_offer,
                fit.prep_archetype,
                fit.prep_hours_est,
                json.dumps(fit.matched_strengths),
                json.dumps(fit.gaps),
                SCORE_VERSION,
                now,
                queue_action,
                urg_x["estimated_days_to_close"],
                int(fd),
                p["id"],
            )
        )
        stats.scored += 1
        stats.in_scope += int(scope.in_scope)
        stats.floor_pass += int(scope.floor_result in ("pass", "exempt"))
        stats.queue += int(active)
        stats.verdicts[verdict.state] = stats.verdicts.get(verdict.state, 0) + 1
    with db.transaction(conn):
        conn.executemany(
            "UPDATE postings SET in_scope=?, scope_reason=?, hard_blockers_json=?, floor_result=?, floor_fail_reasons_json=?, is_stretch=?, is_new_grad=?, referral_likelihood=?, "
            "score_version=?, scored_at=?, composite_score=0, priority=0, apply_priority_rank=NULL, queue_action=NULL, beats_baseline=NULL, beats_baseline_reason=NULL, needs_rescore=0 WHERE id=?",
            light,
        )
        conn.executemany(
            "UPDATE postings SET in_scope=?, scope_reason=?, hard_blockers_json=?, floor_result=?, floor_fail_reasons_json=?, is_stretch=?, is_new_grad=?, referral_likelihood=?, "
            "base_est=?, base_est_low=?, base_est_high=?, signing_est=?, bonus_target_pct_est=?, equity_type=?, equity_annual_est=?, tc_year1_est=?, comp_source=?, comp_confidence=?, "
            "base_col_adjusted=?, base_after_tax_est=?, tax_delta_vs_baseline=?, location_utility_premium=?, effective_value=?, real_terms_vs_baseline=?, tax_rate=?, "
            "comp_score=?, career_capital_score=?, fit_score=?, winnability_score=?, location_score=?, culture_score=?, composite_score=?, beats_baseline=?, beats_baseline_reason=?, "
            "urgency_score=?, priority=?, ev_estimate=?, p_offer=?, prep_archetype=?, prep_hours_est=?, matched_strengths_json=?, gaps_json=?, score_version=?, scored_at=?, queue_action=?, "
            "est_days_to_close=?, first_drop=?, needs_rescore=0 WHERE id=?",
            updates,
        )
        conn.executemany(
            "INSERT INTO posting_docs (posting_id, score_explanation_json, beats_baseline_decomposition_json) VALUES (?, ?, ?) "
            "ON CONFLICT(posting_id) DO UPDATE SET score_explanation_json = excluded.score_explanation_json, beats_baseline_decomposition_json = excluded.beats_baseline_decomposition_json",
            docs,
        )
    rank_queue(conn, cfg)
    from radar.score.views import stamp_view_flags

    stamp_view_flags(conn, ids if ids else None)
    stats.funnel.update(
        {
            "in_scope": stats.in_scope,
            "floor_pass": stats.floor_pass,
            "queue": stats.queue,
            "embedded": stats.embedded,
        }
    )
    stats.elapsed_s = round(time.monotonic() - t0, 2)
    return stats.as_dict()


def _queue_action(
    active: bool,
    fit: Any,
    scope: Any,
    urgency: float,
    p: dict[str, Any],
    verdict: str | None = None,
) -> str | None:
    if not active:
        return None
    if p.get("is_stretch"):
        return None  # stretch (2 YoE): Stretch view only — never the queue (D63)
    if verdict == "worse":
        return "watch"  # in scope, but nothing argues for it over the baseline — never an apply-today item
    high_gaps = [g for g in fit.gaps if g.get("severity") == "high"]
    if fit.fit_score < 0.35 or len(high_gaps) >= 2:
        return "blocked_needs_prep"
    if (
        scope.referral_likelihood == "likely"
        and not p.get("referral_secured")
        and (p.get("company_tier") == 1 or p.get("is_dream_list"))
        and urgency < 0.6
    ):
        return "get_referral_first"  # there's time: a referral first beats a cold application
    if urgency >= 0.6:
        # D64: an unenriched posting is not apply-ready — requirements were never extracted, so a
        # disqualifier in the body may be invisible. Park it; enrichment flips it on the next pass.
        return "apply_today" if p.get("has_requirements") else "needs_review"
    if urgency >= 0.4:
        return "apply_this_week" if p.get("has_requirements") else "needs_review"
    return "watch"


def rank_queue(conn: sqlite3.Connection, cfg: Config) -> int:
    """apply_priority_rank over active rows; 'today' bucket = top N by priority. Only changed rows are written."""
    rows = db.all_rows(
        conn,
        "SELECT id, priority, queue_action, apply_priority_rank, url_status FROM postings WHERE priority > 0 AND is_stretch = 0 "
        "ORDER BY (queue_action = 'needs_review'), priority DESC, id",  # D64: unenriched rows rank after every enriched row
    )
    max_today = cfg.throughput.today_bucket_max
    updates: list[tuple[int | None, str | None, int]] = []
    today_seen = 0
    for i, r in enumerate(rows):
        action = r["queue_action"]
        if r["url_status"] == "dead" and action is not None:
            # a CONFIRMED dead link (404/410, redirect to a generic page, "no longer available")
            # leaves Today for its own small group — verify before dismissing, one tap restores.
            # `unverified` (JS-rendered boards after D54) stays where it scored.
            action = "verify_link"
        elif action == "apply_today":
            today_seen += 1
            if today_seen > max_today:
                action = "apply_this_week"
        if r["apply_priority_rank"] != i + 1 or action != r["queue_action"]:
            updates.append((i + 1, action, r["id"]))
    with db.transaction(conn):
        conn.execute(
            "UPDATE postings SET apply_priority_rank = NULL WHERE apply_priority_rank IS NOT NULL AND (priority IS NULL OR priority <= 0 OR is_stretch = 1)"
        )
        conn.executemany(
            "UPDATE postings SET apply_priority_rank = ?, queue_action = ? WHERE id = ?", updates
        )
    return len(rows)


def decision_calendar(cfg: Config, today: date | None = None) -> dict[str, Any]:
    """§11: two dates, not one. The deadline (no competing offer will exist by then) and the rolling
    switching window with its cost curve, plus the season-elapsed tracker."""
    today = today or date.today()
    rf = cfg.switching_friction
    sign, start = cfg.baseline.decision_deadline, cfg.baseline.start_date
    curve = []
    d = sign
    while d <= start:
        curve.append(
            {
                "date": d.isoformat(),
                **{
                    k: v
                    for k, v in switching_friction(cfg, d).items()
                    if k in ("total", "items", "cheap_zone")
                },
            }
        )
        d += timedelta(days=14)
    season_start, season_end = date(today.year, 9, 1), date(today.year, 12, 31)
    season_frac = (today - season_start).days / max(1, (season_end - season_start).days)
    return {
        "today": today.isoformat(),
        "baseline_decision_deadline": sign.isoformat(),
        "days_to_deadline": (sign - today).days,
        "deadline_note": "Expect few or no competing offers in hand by this date. Decide on the baseline, then keep looking — the tool cannot produce an alternative before the deadline.",
        "baseline_start": start.isoformat(),
        "switching_window": {
            "from": sign.isoformat(),
            "to": start.isoformat(),
            "cheap_zone_ends": rf.cheap_zone_ends.isoformat(),
            "curve": curve,
            "today": switching_friction(cfg, today),
        },
        "season": {
            "start": season_start.isoformat(),
            "end": season_end.isoformat(),
            "elapsed_fraction": round(max(0.0, min(1.0, season_frac)), 3),
            "note": "September through December is when the large majority of new-grad reqs drop, and it is also when switching friction is lowest — both point the same direction: resolve early.",
        },
    }
