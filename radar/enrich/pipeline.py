"""LLM enrichment (§8.1, §8.5) — the only place that spends LLM subscription usage, after the free funnel.

Stage order per run (budgeted by config.llm.max_calls_per_run):
  1. requirement extraction  — classifier model, BATCHED (several postings per call), only in-scope + floor-pass
                               postings with a description and no `requirements_json` yet, best first
  2. resume gap analysis     — enrichment model, one posting per call, only the top of the active queue

Everything is cached by content hash (radar.enrich.llm), so reposts and re-scores are free.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from radar import db
from radar.config import Config
from radar.enrich import llm as llm_mod
from radar.enrich.llm import ClaudeHeadless, LLMUnavailable, reset_accounting
from radar.enrich.resume import load_resume, resume_embedding_text
from radar.util import utcnow_iso

log = logging.getLogger("radar.enrich")

REQ_BATCH = 6
REQ_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "degree_min": {
                        "type": "string",
                        "enum": ["none", "bachelors", "masters", "phd", "unknown"],
                    },
                    "advanced_degree_required": {"type": "boolean"},
                    "min_years": {"type": ["number", "null"]},
                    "max_years": {"type": ["number", "null"]},
                    "languages": {"type": "array", "items": {"type": "string"}},
                    "frameworks": {"type": "array", "items": {"type": "string"}},
                    "clearance": {
                        "type": "string",
                        "enum": ["none", "eligible_to_obtain", "required", "unknown"],
                    },
                    "sponsorship": {
                        "type": "string",
                        "enum": ["offers", "does_not_offer", "unknown"],
                    },
                    "graduation_window": {"type": ["string", "null"]},
                    "onsite_days": {"type": ["integer", "null"]},
                    "new_grad_eligible": {"type": "boolean"},
                    "salary_min": {"type": ["number", "null"]},
                    "salary_max": {"type": ["number", "null"]},
                    "summary": {"type": "string"},
                },
                "required": [
                    "idx",
                    "degree_min",
                    "advanced_degree_required",
                    "min_years",
                    "languages",
                    "frameworks",
                    "clearance",
                    "sponsorship",
                    "graduation_window",
                    "new_grad_eligible",
                    "salary_min",
                    "salary_max",
                    "summary",
                ],
            },
        }
    },
    "required": ["items"],
}
FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "match_score": {"type": "number"},
        "matched_strengths": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"strength": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["strength", "evidence"],
            },
        },
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
                "required": ["gap", "severity", "note"],
            },
        },
        "interview_themes": {"type": "array", "items": {"type": "string"}},
        "prep_archetype": {
            "type": "string",
            "enum": [
                "leetcode_grind",
                "system_design",
                "take_home",
                "domain_finance",
                "behavioral_heavy",
            ],
        },
        "prep_hours": {"type": "number"},
        "one_line_verdict": {"type": "string"},
    },
    "required": [
        "match_score",
        "matched_strengths",
        "gaps",
        "interview_themes",
        "prep_archetype",
        "prep_hours",
        "one_line_verdict",
    ],
}


def _req_prompt(batch: list[sqlite3.Row]) -> str:
    parts = [
        "You extract hiring requirements from job postings for a US new-grad software engineer (BS, graduating May 2027, US citizen, no sponsorship needed).",
        "For each posting return one item with the SAME idx. Be literal: only state a requirement if the text says it. 'Preferred' is not 'required'.",
        "graduation_window: the graduation dates the posting targets, e.g. '2026-2027' or '2027' or null. salary_min/max: annual USD base from the text, else null.",
        "",
    ]
    for i, r in enumerate(batch):
        desc = (r["description_md"] or "")[:3500]
        parts.append(f"=== idx {i} === {r['company_name']} — {r['title']}\n{desc}\n")
    return "\n".join(parts)


def extract_requirements(
    conn: sqlite3.Connection, cfg: Config, *, max_calls: int
) -> dict[str, Any]:
    client = ClaudeHeadless(cfg, task="classifier", max_calls=max_calls)
    rows = db.all_rows(
        conn,
        "SELECT p.id, p.company_name, p.title, d.description_md, p.requires_clearance, p.requires_advanced_degree, p.min_years_experience, p.sponsorship, p.graduation_window, p.base_posted_min "
        "FROM postings p JOIN posting_docs d ON d.posting_id = p.id WHERE p.in_scope = 1 AND p.floor_result IN ('pass','exempt') AND p.description_fetched = 1 AND p.has_requirements = 0 "
        "AND is_cluster_canonical = 1 AND delisted_at IS NULL AND status NOT IN ('dismissed','applied') ORDER BY priority DESC LIMIT ?",
        (max_calls * REQ_BATCH,),
    )
    done = 0
    touched: list[int] = []
    for i in range(0, len(rows), REQ_BATCH):
        batch = rows[i : i + REQ_BATCH]
        try:
            out = client.ask(_req_prompt(batch), REQ_SCHEMA)
        except LLMUnavailable as e:
            log.warning("LLM unavailable: %s", e)
            break
        if not out:
            break
        by_idx = {it.get("idx"): it for it in out.get("items", [])}
        with db.transaction(conn):
            for j, r in enumerate(batch):
                it = by_idx.get(j)
                if not it:
                    continue
                db.upsert_doc(
                    conn,
                    r["id"],
                    requirements_json=json.dumps(
                        {**it, "extracted_at": utcnow_iso(), "model": client.model}
                    ),
                )
                upd: dict[str, Any] = {"has_requirements": 1, "needs_rescore": 1}
                if it.get("clearance") == "required":
                    upd["requires_clearance"] = 1
                elif r["requires_clearance"] is None:
                    upd["requires_clearance"] = 0
                if it.get("advanced_degree_required") or it.get("degree_min") in ("masters", "phd"):
                    upd["requires_advanced_degree"] = 1
                elif r["requires_advanced_degree"] is None:
                    upd["requires_advanced_degree"] = 0
                if r["min_years_experience"] is None and it.get("min_years") is not None:
                    upd["min_years_experience"] = float(it["min_years"])
                if (r["sponsorship"] in (None, "unknown")) and it.get("sponsorship") in (
                    "offers",
                    "does_not_offer",
                ):
                    upd["sponsorship"] = it["sponsorship"]
                if not r["graduation_window"] and it.get("graduation_window"):
                    upd["graduation_window"] = str(it["graduation_window"])[:20]
                if (
                    r["base_posted_min"] is None
                    and it.get("salary_min")
                    and it.get("salary_max")
                    and 20000 <= it["salary_min"] <= it["salary_max"] <= 1_000_000
                ):
                    upd.update(
                        {
                            "base_posted_min": it["salary_min"],
                            "base_posted_max": it["salary_max"],
                            "base_posted_currency": "USD",
                            "base_posted_interval": "year",
                            "comp_source": "posted_range_llm",
                        }
                    )
                db.update(conn, "postings", r["id"], upd)
                touched.append(r["id"])
                done += 1
    return {"postings": done, "calls": client.calls_made, "touched": touched}


def _fit_prompt(cfg: Config, resume_text: str, r: sqlite3.Row) -> str:
    op, pr = cfg.operator, cfg.profile
    school = f"{op.school}, " if op.school else ""
    who = f"{pr.summary} ({school}graduating {op.graduation:%B %Y}, citizenship {op.citizenship})"
    rules = ["- Cite evidence ONLY from the resume text below; never invent experience."]
    if pr.edges:
        rules.append("- Their edges: " + "; ".join(pr.edges) + ".")
    if pr.gaps:
        rules.append(
            "- Their gaps: "
            + "; ".join(pr.gaps)
            + ". Name a stack mismatch explicitly whenever the posting's languages are ones they lack."
        )
    if pr.domain_tag and pr.domain_categories:
        rules.append(
            "- The domain edge only matters for these target categories: "
            + ", ".join(pr.domain_categories)
            + "."
        )
    if not op.treat_as_quant_candidate:
        rules.append(
            "- They are NOT a quant candidate; a rotational-program title at their current employer is not evidence of quant skill."
        )
    rules.append(f"- LeetCode ability is self-rated '{op.leetcode_level}'; weight it lightly.")
    return (
        f"You are helping {who} decide how well ONE job posting fits their actual resume.\n"
        "Rules you must follow:\n" + "\n".join(rules) + "\n"
        "match_score is 0..1 for how well the resume fits THIS posting. prep_hours is realistic prep for the loop.\n\n"
        f"=== RESUME ===\n{resume_text}\n\n=== POSTING: {r['company_name']} — {r['title']} ({r['primary_metro'] or 'location ?'}) ===\n{(r['description_md'] or '')[:5000]}\n"
    )


def resume_gaps(conn: sqlite3.Connection, cfg: Config, *, max_calls: int) -> dict[str, Any]:
    resume = load_resume(cfg)
    if resume is None:
        return {"postings": 0, "calls": 0, "note": "no resume.pdf"}
    client = ClaudeHeadless(cfg, task="enrichment", max_calls=max_calls)
    rows = db.all_rows(
        conn,
        "SELECT p.id, p.company_name, p.title, d.description_md, p.primary_metro, p.fit_score FROM postings p JOIN posting_docs d ON d.posting_id = p.id WHERE p.priority > 0 AND p.description_fetched = 1 "
        "AND p.has_llm_fit = 0 ORDER BY (p.apply_priority_rank IS NULL), p.apply_priority_rank ASC, (p.queue_action = 'needs_review') DESC, p.priority DESC LIMIT ?",
        (max_calls,),
    )
    rtext = resume_embedding_text(resume)
    done = 0
    touched: list[int] = []
    for r in rows:
        try:
            out = client.ask(_fit_prompt(cfg, rtext, r), FIT_SCHEMA)
        except LLMUnavailable as e:
            log.warning("LLM unavailable: %s", e)
            break
        if not out:
            break
        req = db.one(
            conn, "SELECT requirements_json FROM posting_docs WHERE posting_id = ?", (r["id"],)
        )
        try:
            reqd = json.loads(req["requirements_json"]) if req and req["requirements_json"] else {}
        except json.JSONDecodeError:
            reqd = {}
        reqd["llm_fit"] = {**out, "model": client.model, "at": utcnow_iso()}
        db.upsert_doc(conn, r["id"], requirements_json=json.dumps(reqd))
        det = float(r["fit_score"] or 0.5)
        blended = round(0.5 * det + 0.5 * float(out.get("match_score") or det), 3)
        with db.transaction(conn):
            db.update(
                conn,
                "postings",
                r["id"],
                {
                    "has_llm_fit": 1,
                    "has_requirements": 1,
                    "needs_rescore": 1,
                    "matched_strengths_json": json.dumps(
                        [
                            {"strength": m["strength"], "evidence": m["evidence"], "source": "llm"}
                            for m in out.get("matched_strengths", [])
                        ][:6]
                    ),
                    "gaps_json": json.dumps(
                        [{**g, "source": "llm"} for g in out.get("gaps", [])][:6]
                    ),
                    "prep_archetype": out.get("prep_archetype"),
                    "prep_hours_est": out.get("prep_hours"),
                    "fit_score": blended,
                },
            )
        touched.append(r["id"])
        done += 1
    return {"postings": done, "calls": client.calls_made, "touched": touched}


def enrich(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    max_calls: int | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Run both stages within the call budget, then re-score the touched postings."""
    if not cfg.llm.enabled:
        return {"status": "disabled (llm.enabled=false) — deterministic-only scoring", "calls": 0}
    reset_accounting()
    budget = max_calls if max_calls is not None else cfg.llm.max_calls_per_run
    req_budget = int(budget * 0.6)
    a = extract_requirements(conn, cfg, max_calls=req_budget)
    b = resume_gaps(conn, cfg, max_calls=max(0, budget - a["calls"]))
    touched = sorted(set(a["touched"]) | set(b["touched"]))
    if touched:
        from radar.score.engine import score_all

        score_all(conn, cfg, run_id=run_id, ids=touched)
        # re-apply the LLM blend (score_all recomputes the deterministic fit) for the gap-analyzed rows
        for pid in b["touched"]:
            row = db.one(
                conn,
                "SELECT p.fit_score, d.requirements_json FROM postings p JOIN posting_docs d ON d.posting_id = p.id WHERE p.id = ?",
                (pid,),
            )
            try:
                ms = json.loads(row["requirements_json"])["llm_fit"]["match_score"]
            except (TypeError, KeyError, json.JSONDecodeError):
                continue
            with db.transaction(conn):
                db.update(
                    conn,
                    "postings",
                    pid,
                    {"fit_score": round(0.5 * float(row["fit_score"] or 0.5) + 0.5 * float(ms), 3)},
                )
    acct = llm_mod.ACCOUNTING.as_dict()
    if run_id is not None:
        db.update(
            conn,
            "runs",
            run_id,
            {"llm_calls": acct["calls"], "llm_models_json": json.dumps(acct["by_model"])},
        )
    total = db.scalar(conn, "SELECT COUNT(*) FROM postings") or 0
    in_scope = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE in_scope = 1") or 0
    enriched = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE has_requirements = 1") or 0
    return {
        "requirements": {"postings": a["postings"], "calls": a["calls"]},
        "resume_gaps": {"postings": b["postings"], "calls": b["calls"]},
        "llm": acct,
        "funnel": {
            "postings": total,
            "in_scope": in_scope,
            "llm_enriched": enriched,
            "llm_share_of_postings": round(enriched / total, 4) if total else 0,
        },
    }
