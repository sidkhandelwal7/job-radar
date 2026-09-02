"""Per-role application kit (§11): drafts only, never sent, never auto-applied.

One call to the pinned `drafting` model per posting (cached by content hash) producing:
  1. resume bullets re-ordered for THIS posting (only bullets that exist on the resume)
  2. a first-draft "why this firm" paragraph grounded in facts from the posting/company record
  3. a referral-request message to paste (the operator sends it; the system never does)
  4. the three likeliest interview themes and what to prep for each
The kit is stored in posting_docs.kit_md and data/kits/<id>.md and shown in the Detail view.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from radar import db
from radar.config import Config
from radar.enrich.llm import ClaudeHeadless, LLMUnavailable
from radar.enrich.resume import load_resume, resume_embedding_text
from radar.util import utcnow_iso

KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "resume_bullets_ordered": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"bullet": {"type": "string"}, "why_first": {"type": "string"}},
                "required": ["bullet", "why_first"],
            },
            "maxItems": 8,
        },
        "why_this_firm": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "referral_message": {"type": "string"},
        "interview_themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string"},
                    "why": {"type": "string"},
                    "prep": {"type": "string"},
                },
                "required": ["theme", "why", "prep"],
            },
            "minItems": 3,
            "maxItems": 3,
        },
        "honest_risks": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": [
        "resume_bullets_ordered",
        "why_this_firm",
        "facts_used",
        "referral_message",
        "interview_themes",
        "honest_risks",
    ],
}


def _prompt(
    cfg: Config,
    resume_text: str,
    p: dict[str, Any],
    company: dict[str, Any] | None,
    reqs: dict[str, Any],
) -> str:
    facts = []
    if company:
        for k in (
            "tier",
            "target_category",
            "hq_metro",
            "stage",
            "headcount",
            "is_public",
            "rto_policy",
            "notes_md",
        ):
            if company.get(k) not in (None, "", 0):
                facts.append(f"{k}: {company[k]}")
    req_lines = (
        json.dumps({k: v for k, v in reqs.items() if k != "llm_fit"}, default=str)[:2500]
        if reqs
        else "(none extracted)"
    )
    op, pr = cfg.operator, cfg.profile
    school = f"{op.school}, " if op.school else ""
    who = f"{pr.summary} ({school}graduating {op.graduation:%B %Y}, citizenship {op.citizenship})"
    return (
        f"You are drafting application materials for {who}. "
        "Everything you write is a DRAFT the candidate will edit and send themselves; never address the reader as if you were sending it.\n"
        "Hard rules:\n"
        "- resume_bullets_ordered: choose and order ONLY bullets that appear verbatim (or lightly trimmed) in the resume below, most relevant to this posting first. Never invent experience.\n"
        "- why_this_firm: 90–130 words, first person, concrete — cite only facts present in the posting text or company record below and list each fact you used in facts_used. No flattery, no buzzwords.\n"
        "- referral_message: ≤ 90 words, first person, for a LinkedIn/alumni message to a current engineer; mentions the exact role title and one specific, true reason; ends with a low-pressure ask.\n"
        "- interview_themes: exactly three, specific to this posting's stack and level; prep = one concrete thing to do this week.\n"
        "- honest_risks: what in the resume does NOT match (e.g. the posting's main language is absent from the resume). Say it plainly.\n\n"
        f"=== RESUME ===\n{resume_text}\n\n"
        f"=== POSTING: {p['company_name']} — {p['title']} ({p.get('primary_metro') or 'location ?'}) ===\n"
        f"apply url: {p.get('apply_url')}\nextracted requirements: {req_lines}\n"
        f"company record: {'; '.join(facts) or '(no extra facts)'}\n\n{(p.get('description_md') or '')[:6000]}\n"
    )


def render_kit(out: dict[str, Any], p: dict[str, Any], model: str) -> str:
    L = [
        f"# Application kit — {p['company_name']}: {p['title']}",
        "",
        f"_Drafted {utcnow_iso()[:16]}Z · edit before using · nothing here is ever sent automatically._",
        "",
        f"Apply: {p.get('apply_url')}",
        "",
    ]
    L += ["## Resume bullets, in this order", ""]
    for i, b in enumerate(out.get("resume_bullets_ordered", []), 1):
        L += [f"{i}. {b['bullet']}", f"   — _{b['why_first']}_"]
    L += ["", "## Why this firm (first draft)", "", out.get("why_this_firm", ""), ""]
    if out.get("facts_used"):
        L += ["Facts used: " + "; ".join(out["facts_used"]), ""]
    L += [
        "## Referral request (paste, then personalize)",
        "",
        "```",
        out.get("referral_message", ""),
        "```",
        "",
    ]
    L += ["## Three likeliest interview themes", ""]
    for t in out.get("interview_themes", []):
        L += [f"- **{t['theme']}** — {t['why']}", f"  - prep: {t['prep']}"]
    if out.get("honest_risks"):
        L += ["", "## Honest risks", ""] + [f"- {r}" for r in out["honest_risks"]]
    return "\n".join(L) + "\n"


def build_kit(
    conn: sqlite3.Connection, cfg: Config, posting_id: int, *, force: bool = False
) -> dict[str, Any]:
    row = db.one(
        conn,
        "SELECT p.*, d.description_md, d.requirements_json, d.kit_md, d.kit_at FROM postings p LEFT JOIN posting_docs d ON d.posting_id = p.id WHERE p.id = ?",
        (posting_id,),
    )
    if not row:
        raise ValueError(f"posting {posting_id} not found")
    p = dict(row)
    if p.get("kit_md") and not force:
        return {
            "posting_id": posting_id,
            "cached": True,
            "kit_md": p["kit_md"],
            "kit_at": p["kit_at"],
        }
    resume = load_resume(cfg)
    if resume is None:
        raise LLMUnavailable(
            "no resume.pdf — kits are grounded in your resume (operator.resume_path)"
        )
    company = (
        db.one(conn, "SELECT * FROM companies WHERE id = ?", (p["company_id"],))
        if p.get("company_id")
        else None
    )
    try:
        reqs = json.loads(p["requirements_json"]) if p.get("requirements_json") else {}
    except json.JSONDecodeError:
        reqs = {}
    client = ClaudeHeadless(cfg, task="drafting", max_calls=1)
    if not client.available:
        raise LLMUnavailable("LLM disabled or `claude` CLI not on PATH")
    out = client.ask(
        _prompt(cfg, resume_embedding_text(resume), p, dict(company) if company else None, reqs),
        KIT_SCHEMA,
        timeout=240,
    )
    if not out:
        raise LLMUnavailable("drafting call returned nothing (budget exhausted or model error)")
    md = render_kit(out, p, client.model)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO posting_docs (posting_id, kit_md, kit_at) VALUES (?,?,?) ON CONFLICT(posting_id) DO UPDATE SET kit_md = excluded.kit_md, kit_at = excluded.kit_at",
            (posting_id, md, utcnow_iso()),
        )
        db.insert(
            conn,
            "posting_events",
            {
                "posting_id": posting_id,
                "event_type": "kit_drafted",
                "at": utcnow_iso(),
                "data_json": json.dumps({"model": client.model}),
            },
        )
    kdir = cfg.data_dir / "kits"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / f"{posting_id}.md").write_text(md)
    return {
        "posting_id": posting_id,
        "cached": False,
        "kit_md": md,
        "kit_at": utcnow_iso(),
        "model": client.model,
        "path": str(kdir / f"{posting_id}.md"),
    }
