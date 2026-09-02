"""Local FastAPI backend for the dashboard (§13). Everything is local; no third-party calls except
on-demand link verification of a posting you are looking at."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from radar import db
from radar.config import CONFIG_DIR, PROJECT_ROOT, config_path, get_config, reset_config_cache
from radar.query import QueryError, compile_query, field_help
from radar.secrets import load_secrets
from radar.util import ago_human, utcnow_iso

app = FastAPI(
    title="Job Radar", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json"
)

from radar.score.views import LABELS as SUPPRESSION_LABELS  # noqa: E402

SORTABLE = {
    "priority": "p.priority DESC, p.id",
    "rank": "p.apply_priority_rank ASC",
    "composite": "p.composite_score DESC",
    "base": "COALESCE(p.base_est, p.base_posted_max) DESC",
    "effective": "p.effective_value DESC",
    "fit": "p.fit_score DESC",
    "urgency": "p.urgency_score DESC",
    "ev": "p.ev_estimate DESC",
    "first_seen": "p.first_seen_at DESC",
    "posted": "p.posted_at DESC",
    "company": "p.company_name ASC, p.title ASC",
    "title": "p.title ASC",
    "days_to_close": "p.application_deadline ASC",
    "winnability": "p.winnability_score DESC",
    "career_capital": "p.career_capital_score DESC",
    "location": "p.location_score DESC",
}
ROW_COLS = (
    "id, cluster_id, cluster_size, is_cluster_canonical, company_name, company_id, title, primary_metro, metros_json, locations_json, work_mode, "
    "base_posted_min, base_posted_max, base_est, base_est_low, base_est_high, comp_source, comp_confidence, effective_value, real_terms_vs_baseline, "
    "base_col_adjusted, location_utility_premium, tax_delta_vs_baseline, "
    "tc_year1_est, beats_baseline, beats_baseline_reason, composite_score, comp_score, career_capital_score, fit_score, winnability_score, location_score, culture_score, "
    "urgency_score, priority, apply_priority_rank, queue_action, ev_estimate, p_offer, prep_archetype, prep_hours_est, target_category, company_tier, is_dream_list, "
    "role_family, role_subfamily, seniority, is_new_grad, is_stretch, program_type, employment_type, in_scope, scope_reason, floor_result, floor_fail_reasons_json, "
    "hard_blockers_json, requires_clearance, requires_advanced_degree, min_years_experience, sponsorship, graduation_window, referral_likelihood, referral_secured, "
    "same_market_as_baseline_offer, status, status_changed_at, dismiss_reason, snooze_until, starred, tags_user_json, apply_url, canonical_url, url_status, "
    "url_last_verified_at, url_verify_method, posted_at, first_seen_at, last_seen_at, delisted_at, application_deadline, repost_of_id, repost_count, "
    "changed_since_first_seen, source, source_provider, description_fetched, is_international_only, score_version"
)


load_secrets()  # data/secrets.env → os.environ before any channel is built
_local = threading.local()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """One connection per worker thread, kept open: SQLite's page cache stays warm across requests
    (the DB is ~1.5 GB; a cold connection pays seconds on the first query)."""
    cfg = get_config()
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != str(cfg.db_path):
        conn = db.connect(cfg.db_path)
        conn.execute("PRAGMA cache_size = -262144")  # 256 MB page cache per connection
        conn.execute("PRAGMA temp_store = MEMORY")
        db.migrate(conn)
        _local.conn, _local.path = conn, str(cfg.db_path)
    yield conn


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = db.row_to_dict(r) or {}
    for k in list(d):
        if k.endswith("_json"):
            d.pop(k)
    d["url_age"] = ago_human(d.get("url_last_verified_at"))
    d["first_seen_age"] = ago_human(d.get("first_seen_at"))
    return d


def _default_where() -> str:
    return "p.in_default_view = 1"


def _suppressions(
    conn: sqlite3.Connection, base_where: str, params: list[Any]
) -> list[dict[str, Any]]:
    rows = db.all_rows(
        conn,
        f"SELECT p.suppressed_reason AS k, COUNT(*) AS n FROM postings p WHERE ({base_where}) AND p.in_default_view = 0 GROUP BY k",
        params,
    )
    order = {k: i for i, k in enumerate(SUPPRESSION_LABELS)}
    out = [
        {"key": r["k"], "label": SUPPRESSION_LABELS.get(r["k"], r["k"]), "count": int(r["n"])}
        for r in rows
        if r["k"]
    ]
    return sorted(out, key=lambda x: order.get(x["key"], 99))


# ---------------------------------------------------------------------------------------------
# Postings
# ---------------------------------------------------------------------------------------------


@app.get("/api/postings")
def list_postings(
    q: str = "",
    view: str = "default",
    sort: str = "priority",
    limit: int = Query(100, le=1000),
    offset: int = 0,
    facets: bool = True,
) -> dict[str, Any]:
    try:
        c = compile_query(q)
    except QueryError as e:
        raise HTTPException(400, str(e)) from None
    order = SORTABLE.get(sort, SORTABLE["priority"])
    with _conn() as conn:
        master_total = db.scalar(conn, f"SELECT COUNT(*) FROM postings p WHERE {c.where}", c.params)
        where = c.where if view == "master" else f"({c.where}) AND {_default_where()}"
        view_total = db.scalar(conn, f"SELECT COUNT(*) FROM postings p WHERE {where}", c.params)
        rows = db.all_rows(
            conn,
            f"SELECT {ROW_COLS} FROM postings p WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*c.params, limit, offset],
        )
        out: dict[str, Any] = {
            "rows": [_row(r) for r in rows],
            "total": int(view_total),
            "master_total": int(master_total),
            "suppressed": int(master_total - view_total),
            "view": view,
            "suppressions": _suppressions(conn, c.where, c.params) if view != "master" else [],
            "offset": offset,
            "limit": limit,
        }
        if facets:
            out["facets"] = _facets(conn, where, c.params)
    return out


FACET_EXPRS = {
    "beats_baseline": ("p.beats_baseline", 4),
    "metro": ("p.primary_metro", 40),
    "category": ("p.target_category", 10),
    "company": ("p.company_name", 25),
    "work_mode": ("p.work_mode", 5),
    "status": ("p.status", 6),
    "queue_action": ("p.queue_action", 6),
    "has_posted_comp": (
        "CASE WHEN p.base_posted_min IS NOT NULL OR p.base_posted_max IS NOT NULL THEN 'posted' ELSE 'estimated' END",
        2,
    ),
    "fit_bucket": (
        "CASE WHEN p.fit_score >= 0.7 THEN 'strong' WHEN p.fit_score >= 0.5 THEN 'ok' WHEN p.fit_score IS NULL THEN NULL ELSE 'weak' END",
        3,
    ),
    "base_bucket": (
        "CASE WHEN COALESCE(p.base_est, p.base_posted_max) >= 150000 THEN '150k+' WHEN COALESCE(p.base_est, p.base_posted_max) >= 120000 THEN '120-150k' WHEN COALESCE(p.base_est, p.base_posted_max) >= 100000 THEN '100-120k' WHEN COALESCE(p.base_est, p.base_posted_max) >= 85000 THEN '85-100k' WHEN COALESCE(p.base_est, p.base_posted_max) IS NULL THEN NULL ELSE '<85k' END",
        6,
    ),
    "dream": ("CASE WHEN p.is_dream_list = 1 THEN 'dream' ELSE 'other' END", 2),
    "days_to_close": (
        "CASE WHEN p.application_deadline IS NULL THEN NULL WHEN julianday(p.application_deadline) - julianday('now') <= 7 THEN '≤7d' WHEN julianday(p.application_deadline) - julianday('now') <= 30 THEN '≤30d' ELSE '30d+' END",
        3,
    ),
}


def _facets(
    conn: sqlite3.Connection, where: str, params: list[Any]
) -> dict[str, list[dict[str, Any]]]:
    """One pass materializes the facet columns of the match set; each facet is then a cheap GROUP BY."""
    cols = ", ".join(f"{expr} AS {name}" for name, (expr, _) in FACET_EXPRS.items())
    conn.execute("DROP TABLE IF EXISTS temp.facet_rows")
    conn.execute(
        f"CREATE TEMP TABLE facet_rows AS SELECT {cols} FROM postings p WHERE {where}", params
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for name, (_, limit) in FACET_EXPRS.items():
        rows = db.all_rows(
            conn,
            f"SELECT {name} AS k, COUNT(*) AS n FROM temp.facet_rows GROUP BY k ORDER BY n DESC LIMIT ?",
            [limit],
        )
        out[name] = [{"value": r["k"], "count": r["n"]} for r in rows if r["k"] is not None]
    conn.execute("DROP TABLE IF EXISTS temp.facet_rows")
    return out


@app.get("/api/postings/{pid}")
def get_posting(pid: int) -> dict[str, Any]:
    with _conn() as conn:
        r = db.one(
            conn,
            "SELECT p.*, d.description_md, d.score_explanation_json, d.beats_baseline_decomposition_json, d.requirements_json, "
            "c.slug AS company_slug, c.alumni_presence, c.median_days_to_close AS company_median_close "
            "FROM postings p LEFT JOIN posting_docs d ON d.posting_id = p.id LEFT JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
            (pid,),
        )
        if not r:
            raise HTTPException(404, "posting not found")
        d = db.row_to_dict(r) or {}
        for k in (
            "score_explanation_json",
            "beats_baseline_decomposition_json",
            "requirements_json",
            "locations_json",
            "metros_json",
            "tech_tags_json",
            "matched_strengths_json",
            "gaps_json",
            "floor_fail_reasons_json",
            "hard_blockers_json",
            "tags_user_json",
        ):
            d.pop(k, None)
        d["url_age"] = ago_human(d.get("url_last_verified_at"))
        d["siblings"] = (
            [
                dict(x)
                for x in db.all_rows(
                    conn,
                    "SELECT id, source, source_provider, title, apply_url, url_status, is_cluster_canonical, company_name FROM postings WHERE cluster_id = ? AND id != ?",
                    (r["cluster_id"], pid),
                )
            ]
            if r["cluster_id"]
            else []
        )
        d["events"] = [
            {"type": e["event_type"], "at": e["at"], "data": json.loads(e["data_json"] or "{}")}
            for e in db.all_rows(
                conn,
                "SELECT event_type, at, data_json FROM posting_events WHERE posting_id = ? ORDER BY at DESC LIMIT 40",
                (pid,),
            )
        ]
        d["applications"] = [
            dict(a)
            for a in db.all_rows(
                conn,
                "SELECT id, stage, applied_at, completed FROM applications WHERE posting_id = ?",
                (pid,),
            )
        ]
        d["link_checks"] = [
            dict(x)
            for x in db.all_rows(
                conn,
                "SELECT checked_at, method, status, http_status, final_url, reason FROM link_checks WHERE posting_id = ? ORDER BY checked_at DESC LIMIT 5",
                (pid,),
            )
        ]
        d["requirement_checklist"] = _checklist(d)
        d["duplicate_warning"] = _dup_warning(conn, pid)
        return d


def _checklist(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Requirements vs your resume (config.profile.skills), for the detail view."""
    from radar.enrich.resume import _tag_for

    skills = get_config().profile.skills
    req = d.get("requirements") or {}
    items: list[dict[str, Any]] = []
    for lang in (req.get("languages") or []) + (req.get("frameworks") or []):
        tag = _tag_for(lang)
        prof = skills[tag].proficiency if tag and tag in skills else None
        items.append(
            {
                "item": lang,
                "kind": "tech",
                "status": "have"
                if (prof or 0) >= 0.6
                else (
                    "partial" if (prof or 0) >= 0.3 else ("gap" if prof is not None else "unknown")
                ),
            }
        )
    if req.get("degree_min"):
        items.append(
            {
                "item": f"degree: {req['degree_min']}",
                "kind": "degree",
                "status": "gap" if req.get("advanced_degree_required") else "have",
            }
        )
    if req.get("min_years") is not None:
        items.append(
            {
                "item": f"{req['min_years']:g}+ years experience",
                "kind": "years",
                "status": "have"
                if req["min_years"] <= 1
                else ("partial" if req["min_years"] <= 2 else "gap"),
            }
        )
    if req.get("clearance") and req["clearance"] != "none":
        items.append(
            {
                "item": f"clearance: {req['clearance']}",
                "kind": "clearance",
                "status": "gap" if req["clearance"] == "required" else "partial",
            }
        )
    if req.get("sponsorship") == "does_not_offer":
        items.append(
            {"item": "no sponsorship (you don't need it)", "kind": "sponsorship", "status": "have"}
        )
    if req.get("graduation_window"):
        items.append(
            {
                "item": f"graduation window {req['graduation_window']}",
                "kind": "window",
                "status": "have" if "2027" in str(req["graduation_window"]) else "gap",
            }
        )
    return items


def _dup_warning(conn: sqlite3.Connection, pid: int) -> dict[str, Any] | None:
    from radar.applications import find_duplicates

    dups, reason = find_duplicates(conn, posting_id=pid)
    if dups:
        return {
            "reason": reason,
            "applications": [
                {
                    "id": a["id"],
                    "company_name": a["company_name"],
                    "title": a["title"],
                    "stage": a["stage"],
                    "applied_at": a["applied_at"],
                }
                for a in dups
            ],
        }
    return None


class Action(BaseModel):
    action: str  # applied | dismiss | shortlist | unshortlist | snooze | referral | note | star | tag | restore_link
    reason: str | None = None
    days: int | None = None
    contact: str | None = None
    text: str | None = None
    force: bool = False
    tags: list[str] | None = None


def _apply_action(conn: sqlite3.Connection, pid: int, a: Action) -> dict[str, Any]:
    from radar.workflow import apply_action

    res = apply_action(
        conn,
        pid,
        a.action,
        reason=a.reason,
        days=a.days,
        contact=a.contact,
        text=a.text,
        force=a.force,
        tags=a.tags,
        via="dashboard",
    )
    if res.get("error") == "posting not found":
        raise HTTPException(404, "posting not found")
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return dict(res)


@app.post("/api/postings/{pid}/action")
async def posting_action(pid: int, a: Action) -> dict[str, Any]:
    with _conn() as conn:
        if a.action == "restore_link":
            # re-verify first: if the link is demonstrably live again, record that instead of a
            # manual override; if the verifier still says dead, the human's word wins (recorded as manual)
            from radar.links import sweep

            await sweep(conn, get_config(), posting_ids=[pid])
            now_status = db.scalar(conn, "SELECT url_status FROM postings WHERE id = ?", (pid,))
            if now_status in ("live", "redirected"):
                conn.execute("UPDATE postings SET needs_rescore = 1 WHERE id = ?", (pid,))
                res: dict[str, Any] = {"ok": True, "verified": True, "url_status": now_status}
            else:
                res = _apply_action(conn, pid, a)
                res["verified"] = False
        else:
            res = _apply_action(conn, pid, a)
        if res.get("ok") and a.action in (
            "referral",
            "applied",
            "dismiss",
            "snooze",
            "override_floor",
            "restore_link",
        ):
            from radar.score.engine import score_all

            score_all(conn, get_config(), ids=[pid])
        return res


class BulkAction(BaseModel):
    ids: list[int]
    action: Action


@app.post("/api/postings/bulk")
def bulk_action(b: BulkAction) -> dict[str, Any]:
    results = []
    with _conn() as conn:
        for pid in b.ids[:500]:
            try:
                results.append({"id": pid, **_apply_action(conn, pid, b.action)})
            except HTTPException as e:
                results.append({"id": pid, "ok": False, "error": e.detail})
        from radar.score.engine import score_all

        score_all(conn, get_config(), ids=b.ids[:500])
    return {"results": results}


@app.post("/api/postings/{pid}/verify")
async def verify_posting(pid: int) -> dict[str, Any]:
    from radar.links import sweep

    cfg = get_config()
    with _conn() as conn:
        st = await sweep(conn, cfg, posting_ids=[pid])
        r = db.one(
            conn,
            "SELECT url_status, url_last_verified_at, url_verify_method, url_final FROM postings WHERE id = ?",
            (pid,),
        )
        return {**dict(r), "checked": st.checked, "url_age": ago_human(r["url_last_verified_at"])}


@app.get("/api/queue")
def queue(action: str | None = None, limit: int = 50) -> dict[str, Any]:
    cfg = get_config()
    with _conn() as conn:
        where = "p.apply_priority_rank IS NOT NULL" + (" AND p.queue_action = ?" if action else "")
        params: list[Any] = [action] if action else []
        rows = db.all_rows(
            conn,
            f"SELECT {ROW_COLS} FROM postings p WHERE {where} ORDER BY p.apply_priority_rank LIMIT ?",
            [*params, limit],
        )
        dead = [
            _row(r)
            for r in db.all_rows(
                conn,
                f"SELECT {ROW_COLS} FROM postings p WHERE p.apply_priority_rank IS NOT NULL AND p.queue_action = 'verify_link' ORDER BY p.apply_priority_rank LIMIT 20",
            )
        ]
        counts = {
            r["k"]: r["n"]
            for r in db.all_rows(
                conn,
                "SELECT queue_action k, COUNT(*) n FROM postings WHERE apply_priority_rank IS NOT NULL GROUP BY k",
            )
        }
        return {
            "rows": [_row(r) for r in rows],
            "counts": counts,
            "dead": dead,
            "total": sum(counts.values()),
            "today_cap": cfg.throughput.today_bucket_max,
            "applications_per_week": cfg.throughput.applications_per_week,
        }


@app.get("/api/export.csv")
def export_csv(q: str = "", view: str = "default") -> StreamingResponse:
    c = compile_query(q)
    where = c.where if view == "master" else f"({c.where}) AND {_default_where()}"
    cols = [
        "id",
        "company_name",
        "title",
        "primary_metro",
        "work_mode",
        "base_posted_min",
        "base_posted_max",
        "base_est",
        "comp_source",
        "effective_value",
        "beats_baseline",
        "composite_score",
        "apply_priority_rank",
        "queue_action",
        "status",
        "posted_at",
        "first_seen_at",
        "url_status",
        "url_last_verified_at",
        "apply_url",
        "canonical_url",
    ]

    def gen() -> Iterator[str]:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        yield buf.getvalue()
        with _conn() as conn:
            for r in db.all_rows(
                conn,
                f"SELECT {', '.join(cols)} FROM postings p WHERE {where} ORDER BY p.priority DESC",
                c.params,
            ):
                buf.seek(0)
                buf.truncate()
                w.writerow([r[k] for k in cols])
                yield buf.getvalue()

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=job-radar-export.csv"},
    )


# ---------------------------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------------------------


@app.get("/api/applications")
def list_applications(include_completed: bool = True) -> dict[str, Any]:
    from radar.applications import funnel_stats, suggestions

    with _conn() as conn:
        rows = db.all_rows(
            conn,
            "SELECT * FROM applications "
            + ("" if include_completed else "WHERE completed = 0 ")
            + "ORDER BY applied_at DESC",
        )
        sug = suggestions(conn)
        return {
            "rows": [dict(r) for r in rows],
            "stats": funnel_stats(conn),
            "follow_ups_due": [dict(r) for r in sug["follow_ups_due"]],
            "ghosted_candidates": [dict(r) for r in sug["ghosted_candidates"]],
        }


class NewApplication(BaseModel):
    url: str | None = None
    company_name: str | None = None
    title: str | None = None
    location: str | None = None
    applied_at: str | None = None
    stage: str = "applied"
    notes: str | None = None
    referral_contact: str | None = None
    source_of_discovery: str = "manual"
    posting_id: int | None = None
    force: bool = False


@app.post("/api/applications/autofill")
async def autofill(url: str = Body(..., embed=True)) -> dict[str, Any]:
    from radar.applications import autofill_from_url

    cfg = get_config()
    with _conn() as conn:
        af = await autofill_from_url(conn, url, cfg.fetch.user_agent)
        return af.__dict__


@app.post("/api/applications")
def create_application(a: NewApplication) -> dict[str, Any]:
    from radar.applications import DuplicateApplication, add_manual

    with _conn() as conn:
        try:
            app_id = add_manual(
                conn,
                url=a.url,
                company_name=a.company_name,
                title=a.title,
                location=a.location,
                applied_at=a.applied_at,
                stage=a.stage,
                notes=a.notes,
                referral_used=bool(a.referral_contact),
                referral_contact=a.referral_contact,
                source_of_discovery=a.source_of_discovery,
                posting_id=a.posting_id,
                force=a.force,
            )
        except DuplicateApplication as e:
            return {
                "ok": False,
                "duplicate": {
                    "reason": e.reason,
                    "applications": [
                        {
                            "id": x["id"],
                            "company_name": x["company_name"],
                            "title": x["title"],
                            "stage": x["stage"],
                        }
                        for x in e.existing
                    ],
                },
            }
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "id": app_id}


class AppPatch(BaseModel):
    stage: str | None = None
    completed: bool | None = None
    outcome: str | None = None
    notes_md: str | None = None
    note: str | None = None
    base_offered: float | None = None
    follow_up_due: str | None = None
    referral_contact: str | None = None
    resume_version_used: str | None = None


@app.patch("/api/applications/{app_id}")
def patch_application(app_id: int, p: AppPatch) -> dict[str, Any]:
    from radar.applications import set_completed, set_stage

    with _conn() as conn:
        if not db.one(conn, "SELECT id FROM applications WHERE id = ?", (app_id,)):
            raise HTTPException(404, "application not found")
        if p.stage:
            set_stage(conn, app_id, p.stage, note=p.note, base_offered=p.base_offered)
        if p.completed is not None:
            set_completed(conn, app_id, p.completed, outcome=p.outcome)
        upd = {
            k: v
            for k, v in {
                "notes_md": p.notes_md,
                "follow_up_due": p.follow_up_due,
                "referral_contact": p.referral_contact,
                "resume_version_used": p.resume_version_used,
                "base_offered": p.base_offered,
            }.items()
            if v is not None
        }
        if upd:
            with db.transaction(conn):
                db.update(conn, "applications", app_id, {**upd, "updated_at": utcnow_iso()})
        return dict(db.one(conn, "SELECT * FROM applications WHERE id = ?", (app_id,)))


@app.get("/api/applications/export.csv")
def export_applications() -> StreamingResponse:
    def gen() -> Iterator[str]:
        with _conn() as conn:
            rows = db.all_rows(conn, "SELECT * FROM applications ORDER BY applied_at")
            buf = io.StringIO()
            if rows:
                w = csv.DictWriter(buf, fieldnames=rows[0].keys())
                w.writeheader()
                yield buf.getvalue()
                for r in rows:
                    buf.seek(0)
                    buf.truncate()
                    w.writerow(dict(r))
                    yield buf.getvalue()

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


# ---------------------------------------------------------------------------------------------
# Calendar, config, presets, fields, health
# ---------------------------------------------------------------------------------------------


@app.get("/api/calendar")
def calendar() -> dict[str, Any]:
    from radar.score.engine import decision_calendar

    cfg = get_config()
    cal = decision_calendar(cfg)
    with _conn() as conn:
        cal["upcoming_deadlines"] = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT id, company_name, title, application_deadline, apply_url, queue_action FROM postings WHERE application_deadline >= date('now') AND apply_priority_rank IS NOT NULL ORDER BY application_deadline LIMIT 30",
            )
        ]
        cal["follow_ups"] = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT id, company_name, title, follow_up_due FROM applications WHERE completed = 0 AND follow_up_due IS NOT NULL ORDER BY follow_up_due LIMIT 30",
            )
        ]
    return cal


EDITABLE = (
    "weights",
    "location_utility_premium",
    "comp_gates",
    "modifiers",
    "switching_friction",
    "throughput",
    "ev",
    "dream_list",
    "floor_exempt_companies",
    "blocked_companies",
    "blocked_metros",
)


@app.get("/api/config")
def get_cfg() -> dict[str, Any]:
    cfg = get_config()
    d = cfg.model_dump(mode="json")
    return {k: d[k] for k in EDITABLE} | {
        "baseline": d["baseline"],
        "llm": {"enabled": d["llm"]["enabled"], "models": cfg.llm.models()},
        "plain_language": _plain_language(cfg),
    }


def _plain_language(cfg: Any) -> list[str]:
    lp = cfg.location_utility_premium
    return [
        f"You're implicitly valuing New York at ${lp.new_york:,.0f}/year over {cfg.baseline.metro.replace('_', ' ')}. Agree?",
        f"San Francisco at ${lp.san_francisco:,.0f}, Seattle at ${lp.seattle:,.0f} (plus WA's zero income tax), other hubs at ${lp.other_major_tech_hub:,.0f}. Remote is neutral.",
        f"A base at or above ${cfg.comp_gates.instant_yes:,.0f} is an instant yes; below ${cfg.comp_gates.hard_floor:,.0f} is hidden by default.",
        f"Walking away from the baseline offer today would cost about ${sum(__import__('radar.score.engine', fromlist=['x']).switching_friction(cfg)['items'].values()):,.0f} by your own itemization.",
    ]


class ConfigPatch(BaseModel):
    changes: dict[str, Any]
    commit: bool = False
    note: str | None = None


@app.post("/api/config/preview")
def preview_config(p: ConfigPatch) -> dict[str, Any]:
    """Apply changes in memory, re-score the current top of the queue, and diff — nothing is written."""
    import copy

    from radar.config import Config
    from radar.score.engine import score_all

    cfg = get_config()
    raw = yaml.safe_load(config_path().read_text())
    new_raw = copy.deepcopy(raw)
    for k, v in p.changes.items():
        if k not in EDITABLE:
            raise HTTPException(400, f"{k} is not editable from the dashboard")
        if isinstance(v, dict) and isinstance(new_raw.get(k), dict):
            new_raw[k].update(v)
        else:
            new_raw[k] = v
    try:
        new_cfg = Config.model_validate(new_raw)
    except Exception as e:
        raise HTTPException(400, f"invalid config: {e}") from None
    with _conn() as conn:
        before = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT id, company_name, title, apply_priority_rank, composite_score, beats_baseline FROM postings WHERE apply_priority_rank IS NOT NULL ORDER BY apply_priority_rank LIMIT 30",
            )
        ]
        ids = [
            r["id"]
            for r in db.all_rows(
                conn,
                "SELECT id FROM postings WHERE apply_priority_rank IS NOT NULL ORDER BY apply_priority_rank LIMIT 400",
            )
        ]
        # score into a temp copy of the rows? Cheaper: score in place with new cfg, diff, then restore with old cfg.
        score_all(conn, new_cfg, ids=ids)
        after = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT id, company_name, title, apply_priority_rank, composite_score, beats_baseline FROM postings WHERE apply_priority_rank IS NOT NULL ORDER BY apply_priority_rank LIMIT 30",
            )
        ]
        if p.commit:
            text = yaml.safe_dump(new_raw, sort_keys=False, allow_unicode=True)
            (CONFIG_DIR / "config.yaml").write_text(text)
            reset_config_cache()
            from radar.rescore import rescore

            rescore(conn, get_config())
            return {
                "committed": True,
                "before": before,
                "after": after,
                "note": "config.yaml written (comments reset — see git diff); full rescore done",
            }
        score_all(conn, cfg, ids=ids)  # restore
    return {
        "committed": False,
        "before": before,
        "after": after,
        "plain_language": _plain_language(new_cfg),
    }


@app.get("/api/presets")
def presets() -> list[dict[str, Any]]:
    with _conn() as conn:
        return [
            dict(r)
            for r in db.all_rows(conn, "SELECT * FROM saved_filters ORDER BY is_preset DESC, id")
        ]


class SavedFilter(BaseModel):
    name: str
    query: str
    sort: str | None = None
    alert_tier: str | None = None


@app.post("/api/saved-filters")
def save_filter(f: SavedFilter) -> dict[str, Any]:
    with _conn() as conn, db.transaction(conn):
        row = db.one(conn, "SELECT id FROM saved_filters WHERE name = ?", (f.name,))
        now = utcnow_iso()
        if row:
            db.update(
                conn,
                "saved_filters",
                row["id"],
                {"query": f.query, "sort": f.sort, "alert_tier": f.alert_tier, "updated_at": now},
            )
            return {"id": row["id"]}
        return {
            "id": db.insert(
                conn,
                "saved_filters",
                {
                    "name": f.name,
                    "query": f.query,
                    "sort": f.sort,
                    "alert_tier": f.alert_tier,
                    "is_preset": 0,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        }


@app.delete("/api/saved-filters/{fid}")
def delete_filter(fid: int) -> dict[str, Any]:
    with _conn() as conn, db.transaction(conn):
        conn.execute("DELETE FROM saved_filters WHERE id = ? AND is_preset = 0", (fid,))
    return {"ok": True}


@app.get("/api/fields")
def fields() -> dict[str, Any]:
    groups: dict[str, list[dict[str, str]]] = {}
    for f in field_help():
        groups.setdefault(f["group"], []).append(f)
    return {"groups": groups, "sortable": sorted(SORTABLE)}


@app.get("/api/postings/{posting_id}/kit")
def get_kit(posting_id: int) -> dict[str, Any]:
    with _conn() as conn:
        r = db.one(
            conn, "SELECT kit_md, kit_at FROM posting_docs WHERE posting_id = ?", (posting_id,)
        )
    return {
        "posting_id": posting_id,
        "kit_md": r["kit_md"] if r else None,
        "kit_at": r["kit_at"] if r else None,
    }


@app.post("/api/postings/{posting_id}/kit")
def draft_kit(posting_id: int, force: bool = False) -> dict[str, Any]:
    """Draft the application kit (one pinned drafting-model call; drafts only, never sent)."""
    from radar.enrich.llm import LLMUnavailable
    from radar.ops.kits import build_kit

    cfg = get_config()
    with _conn() as conn:
        try:
            r = build_kit(conn, cfg, posting_id, force=force)
        except LLMUnavailable as e:
            raise HTTPException(503, str(e)) from None
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
    return {k: v for k, v in r.items() if k != "path"}


@app.get("/api/ops")
def ops_status() -> dict[str, Any]:
    """Phase 6 status for the Health view: backups, snapshots, calibration, alarms, launchd."""
    from radar.ops import alarms
    from radar.ops.backup import list_backups

    cfg = get_config()
    with _conn() as conn:
        al = alarms.evaluate(conn, cfg)
        last = {
            k: db.kv_get(conn, k)
            for k in (
                "last_cycle_at",
                "last_backup_at",
                "last_snapshot_at",
                "last_calibration_at",
                "last_notify_at",
                "last_daily_digest_at",
                "last_weekly_digest_at",
            )
        }
        cal = db.one(
            conn,
            "SELECT month, ran_at, labeled, positives FROM calibration_runs ORDER BY id DESC LIMIT 1",
        )
        market = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT week_start, open_reqs, new_reqs, closed_reqs, in_scope_open, median_days_to_close FROM velocity_snapshots WHERE company_id IS NULL ORDER BY week_start DESC LIMIT 12",
            )
        ]
    try:
        from radar.ops.launchd import status as ld_status

        agents = ld_status()
    except Exception:
        agents = {}
    return {
        "alarms": alarms.as_dicts(al),
        "last": last,
        "backups": [
            {"name": p.name, "mb": round(p.stat().st_size / 1e6, 1)}
            for p in list_backups(cfg.backups_dir)[-5:]
        ],
        "calibration": dict(cal) if cal else None,
        "market_weeks": market,
        "launchd": agents,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    cfg = get_config()
    with _conn() as conn:
        total = db.scalar(conn, "SELECT COUNT(*) FROM postings") or 0
        in_scope = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE in_scope = 1") or 0
        enriched = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE has_requirements = 1") or 0
        sources = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT c.name, cs.provider, cs.slug, cs.cadence, cs.last_success_at, cs.last_row_count, cs.typical_row_count, cs.consecutive_failures, cs.last_error FROM company_sources cs JOIN companies c ON c.id = cs.company_id WHERE cs.enabled = 1 ORDER BY cs.consecutive_failures DESC, cs.last_success_at ASC LIMIT 400",
            )
        ]
        runs = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT id, kind, started_at, finished_at, status, llm_calls, llm_models_json, error FROM runs ORDER BY id DESC LIMIT 30",
            )
        ]
        llm_total = db.scalar(conn, "SELECT COALESCE(SUM(llm_calls),0) FROM runs") or 0
        per_model: dict[str, int] = {}
        for r in db.all_rows(conn, "SELECT llm_models_json FROM runs WHERE llm_calls > 0"):
            for k, v in json.loads(r["llm_models_json"] or "{}").items():
                per_model[k] = per_model.get(k, 0) + int(v)
        return {
            "counts": {
                "postings": total,
                "listed": db.scalar(
                    conn, "SELECT COUNT(*) FROM postings WHERE delisted_at IS NULL"
                ),
                "in_scope": in_scope,
                "queue": db.scalar(
                    conn, "SELECT COUNT(*) FROM postings WHERE apply_priority_rank IS NOT NULL"
                ),
                "applications": db.scalar(conn, "SELECT COUNT(*) FROM applications"),
                "companies": db.scalar(conn, "SELECT COUNT(*) FROM companies"),
                "sources": db.scalar(
                    conn, "SELECT COUNT(*) FROM company_sources WHERE enabled = 1"
                ),
                "dead_links": db.scalar(
                    conn, "SELECT COUNT(*) FROM postings WHERE url_status = 'dead'"
                ),
                "lca_rows": db.scalar(conn, "SELECT COUNT(*) FROM lca_wages"),
            },
            "funnel": {
                "postings": total,
                "in_scope": in_scope,
                "llm_enriched": enriched,
                "rules_eliminated_share": round(1 - in_scope / total, 4) if total else None,
            },
            "llm": {
                "enabled": cfg.llm.enabled,
                "models": cfg.llm.models(),
                "calls_total": llm_total,
                "per_model": per_model,
            },
            "sources": sources,
            "runs": runs,
            "last_cycle_at": db.kv_get(conn, "last_cycle_at"),
            "actions_invocations_this_month": __import__(
                "radar.actions_runner", fromlist=["x"]
            ).invocations_this_month(),
        }


@app.get("/api/calendar.ics")
def calendar_ics() -> Any:
    from fastapi.responses import Response

    from radar.notify.digest import ics_feed

    with _conn() as conn:
        return Response(
            ics_feed(conn, get_config()),
            media_type="text/calendar",
            headers={"Content-Disposition": "inline; filename=job-radar.ics"},
        )


@app.get("/api/notifications")
def notifications(limit: int = 100) -> dict[str, Any]:
    from radar.notify.engine import precision_report

    with _conn() as conn:
        rows = [
            dict(r)
            for r in db.all_rows(
                conn,
                "SELECT n.*, p.company_name, p.title FROM notifications n LEFT JOIN postings p ON p.id = n.posting_id ORDER BY n.sent_at DESC LIMIT ?",
                (limit,),
            )
        ]
        return {"rows": rows, "precision": precision_report(conn)}


@app.get("/api/companies/{cid}")
def company(cid: int) -> dict[str, Any]:
    with _conn() as conn:
        r = db.one(conn, "SELECT * FROM companies WHERE id = ?", (cid,))
        if not r:
            raise HTTPException(404)
        d = db.row_to_dict(r) or {}
        d["open_postings"] = db.scalar(
            conn,
            "SELECT COUNT(*) FROM postings WHERE company_id = ? AND delisted_at IS NULL",
            (cid,),
        )
        d["in_scope_postings"] = db.scalar(
            conn,
            "SELECT COUNT(*) FROM postings WHERE company_id = ? AND in_scope = 1 AND delisted_at IS NULL",
            (cid,),
        )
        d["posted_ranges"] = [
            dict(x)
            for x in db.all_rows(
                conn,
                "SELECT title, base_posted_min, base_posted_max, primary_metro FROM postings WHERE company_id = ? AND base_posted_min IS NOT NULL ORDER BY posted_at DESC LIMIT 20",
                (cid,),
            )
        ]
        return d


# ---------------------------------------------------------------------------------------------
# Static SPA
# ---------------------------------------------------------------------------------------------

DIST = PROJECT_ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> Any:
        p = DIST / full_path
        if full_path and p.exists() and p.is_file():
            return FileResponse(p)
        return FileResponse(DIST / "index.html")
else:

    @app.get("/")
    def root() -> JSONResponse:
        return JSONResponse(
            {
                "message": "Job Radar API is running. Build the dashboard with `cd web && npm run build`, then reload.",
                "docs": "/api/docs",
            }
        )


def serve(host: str = "127.0.0.1", port: int = 8787, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("radar.serve.api:app", host=host, port=port, reload=reload, log_level="warning")
