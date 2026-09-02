"""Applications tracker (§15): first-class, permanent, never hidden by a filter.

- mark_applied(posting_id)   one tap from queue/table/detail/Telegram → application row + posting.status
- add_manual(url, ...)       a job the system never saw; best-effort autofill from the URL
- duplicate guard            same posting, same cluster, or repost chain, or same apply_url
- follow-ups                 10 business days → nudge; 30 days no response → suggest `ghosted`
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from radar import db
from radar.util import business_days_after, parse_dt, to_iso, utcnow, utcnow_iso

STAGES = [
    "applied",
    "oa_pending",
    "oa_done",
    "screen",
    "onsite",
    "offer",
    "rejected",
    "ghosted",
    "withdrawn",
]
TERMINAL_STAGES = {"offer", "rejected", "ghosted", "withdrawn"}
RESPONSE_STAGES = {"oa_pending", "oa_done", "screen", "onsite", "offer", "rejected"}
FOLLOW_UP_BUSINESS_DAYS = 10
GHOSTED_AFTER_DAYS = 30


class DuplicateApplication(Exception):
    def __init__(self, existing: list[sqlite3.Row], reason: str) -> None:
        self.existing = existing
        self.reason = reason
        super().__init__(reason)


@dataclass
class Autofill:
    company_name: str | None = None
    title: str | None = None
    location: str | None = None
    posting_id: int | None = None
    source: str = "manual"
    confidence: float = 0.0
    note: str | None = None


def find_duplicates(
    conn: sqlite3.Connection, *, posting_id: int | None = None, apply_url: str | None = None
) -> tuple[list[sqlite3.Row], str]:
    """Return (existing applications, reason) that would make a new application a duplicate."""
    if posting_id is not None:
        direct = db.all_rows(conn, "SELECT * FROM applications WHERE posting_id = ?", (posting_id,))
        if direct:
            return direct, "already applied to this exact posting"
        p = db.one(
            conn,
            "SELECT cluster_id, repost_of_id, apply_url, canonical_url FROM postings WHERE id = ?",
            (posting_id,),
        )
        if p:
            if p["cluster_id"]:
                sib = db.all_rows(
                    conn,
                    "SELECT a.* FROM applications a JOIN postings s ON s.id = a.posting_id WHERE s.cluster_id = ? AND s.id != ?",
                    (p["cluster_id"], posting_id),
                )
                if sib:
                    return sib, "already applied to the same role via another source (same cluster)"
            chain = _repost_chain(conn, posting_id)
            if chain:
                rep = db.all_rows(
                    conn,
                    f"SELECT * FROM applications WHERE posting_id IN ({','.join('?' for _ in chain)})",
                    chain,
                )
                if rep:
                    return rep, "already applied to an earlier posting of this same req (repost)"
            apply_url = apply_url or p["apply_url"]
    if apply_url:
        same = db.all_rows(
            conn, "SELECT * FROM applications WHERE apply_url = ?", (_norm_url(apply_url),)
        )
        if same:
            return same, "an application with this exact URL already exists"
    return [], ""


def _repost_chain(conn: sqlite3.Connection, posting_id: int) -> list[int]:
    ids: list[int] = []
    cur = posting_id
    for _ in range(20):
        row = db.one(conn, "SELECT repost_of_id FROM postings WHERE id = ?", (cur,))
        if not row or not row["repost_of_id"]:
            break
        ids.append(row["repost_of_id"])
        cur = row["repost_of_id"]
    later = db.all_rows(conn, "SELECT id FROM postings WHERE repost_of_id = ?", (posting_id,))
    ids.extend(r["id"] for r in later)
    return ids


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"[?&](utm_[a-z]+|ref|source|src|gh_src|lever-source(?:%5B%5D|\[\])?)=[^&#]*", "", u)
    return u.rstrip("?&").rstrip("/")


def mark_applied(
    conn: sqlite3.Connection,
    posting_id: int,
    *,
    applied_at: str | None = None,
    referral_used: bool = False,
    referral_contact: str | None = None,
    notes: str | None = None,
    resume_version: str | None = None,
    force: bool = False,
    source_channel: str = "cli",
) -> int:
    p = db.one(
        conn,
        "SELECT p.*, c.slug AS company_slug FROM postings p LEFT JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
        (posting_id,),
    )
    if not p:
        raise KeyError(f"posting {posting_id} not found")
    dups, reason = find_duplicates(conn, posting_id=posting_id)
    if dups and not force:
        raise DuplicateApplication(dups, reason)
    now = utcnow_iso()
    applied_iso = to_iso(parse_dt(applied_at)) if applied_at else now
    applied_dt = parse_dt(applied_iso) or utcnow()
    loc = _location_label(p)
    with db.transaction(conn):
        app_id = db.insert(
            conn,
            "applications",
            {
                "posting_id": posting_id,
                "company_name": p["company_name"],
                "title": p["title"],
                "location": loc,
                "apply_url": _norm_url(p["apply_url"]),
                "applied_at": applied_iso,
                "stage": "applied",
                "stage_changed_at": applied_iso,
                "completed": 0,
                "referral_used": int(referral_used),
                "referral_contact": referral_contact,
                "follow_up_due": to_iso(business_days_after(applied_dt, FOLLOW_UP_BUSINESS_DAYS)),
                "notes_md": notes,
                "source_of_discovery": p["source_provider"],
                "target_category": p["target_category"],
                "resume_version_used": resume_version,
                "created_manually": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        db.insert(
            conn,
            "application_events",
            {
                "application_id": app_id,
                "at": now,
                "event_type": "created",
                "data_json": json.dumps({"via": source_channel}),
            },
        )
        db.update(
            conn,
            "postings",
            posting_id,
            {
                "status": "applied",
                "status_changed_at": now,
                "in_default_view": 0,
                "suppressed_reason": "applied",
                "priority": 0,
                "apply_priority_rank": None,
            },
        )
        db.add_event(conn, posting_id, "applied", {"application_id": app_id, "via": source_channel})
    return app_id


def _location_label(p: sqlite3.Row) -> str | None:
    try:
        locs = json.loads(p["locations_json"] or "[]")
    except json.JSONDecodeError:
        locs = []
    names = [
        loc.get("metro_name") or loc.get("raw")
        for loc in locs
        if loc.get("metro_name") or loc.get("raw")
    ]
    return "; ".join(dict.fromkeys(names)) or None


def add_manual(
    conn: sqlite3.Connection,
    *,
    url: str | None,
    company_name: str | None,
    title: str | None,
    location: str | None = None,
    applied_at: str | None = None,
    stage: str = "applied",
    notes: str | None = None,
    referral_used: bool = False,
    referral_contact: str | None = None,
    source_of_discovery: str = "manual",
    resume_version: str | None = None,
    posting_id: int | None = None,
    force: bool = False,
) -> int:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    if not (company_name and title):
        raise ValueError("company_name and title are required (autofill could not determine them)")
    norm_url = _norm_url(url) if url else None
    dups, reason = find_duplicates(conn, posting_id=posting_id, apply_url=norm_url)
    if dups and not force:
        raise DuplicateApplication(dups, reason)
    now = utcnow_iso()
    applied_iso = to_iso(parse_dt(applied_at)) if applied_at else now
    applied_dt = parse_dt(applied_iso) or utcnow()
    with db.transaction(conn):
        app_id = db.insert(
            conn,
            "applications",
            {
                "posting_id": posting_id,
                "company_name": company_name.strip(),
                "title": title.strip(),
                "location": (location or "").strip() or None,
                "apply_url": norm_url,
                "applied_at": applied_iso,
                "stage": stage,
                "stage_changed_at": applied_iso,
                "completed": int(stage in TERMINAL_STAGES and stage != "offer"),
                "referral_used": int(referral_used),
                "referral_contact": referral_contact,
                "follow_up_due": to_iso(business_days_after(applied_dt, FOLLOW_UP_BUSINESS_DAYS))
                if stage == "applied"
                else None,
                "notes_md": notes,
                "source_of_discovery": source_of_discovery,
                "resume_version_used": resume_version,
                "created_manually": 1,
                "created_at": now,
                "updated_at": now,
            },
        )
        db.insert(
            conn,
            "application_events",
            {
                "application_id": app_id,
                "at": now,
                "event_type": "created",
                "data_json": json.dumps({"manual": True}),
            },
        )
        if posting_id:
            db.update(conn, "postings", posting_id, {"status": "applied", "status_changed_at": now})
            db.add_event(conn, posting_id, "applied", {"application_id": app_id, "via": "manual"})
    return app_id


def set_stage(
    conn: sqlite3.Connection,
    app_id: int,
    stage: str,
    *,
    note: str | None = None,
    base_offered: float | None = None,
) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    row = db.one(conn, "SELECT * FROM applications WHERE id = ?", (app_id,))
    if not row:
        raise KeyError(f"application {app_id} not found")
    now = utcnow_iso()
    upd: dict[str, Any] = {"stage": stage, "stage_changed_at": now, "updated_at": now}
    if stage in RESPONSE_STAGES and not row["first_response_at"]:
        upd["first_response_at"] = now
    if stage in TERMINAL_STAGES and stage != "offer":
        upd["completed"] = 1
        upd["outcome"] = stage
        upd["follow_up_due"] = None
    if stage == "offer":
        upd["outcome"] = "offer"
        upd["follow_up_due"] = None
    if base_offered is not None:
        upd["base_offered"] = base_offered
    if note:
        upd["notes_md"] = ((row["notes_md"] or "") + f"\n\n[{now[:10]}] {note}").strip()
    with db.transaction(conn):
        db.update(conn, "applications", app_id, upd)
        db.insert(
            conn,
            "application_events",
            {
                "application_id": app_id,
                "at": now,
                "event_type": "stage_changed",
                "data_json": json.dumps({"from": row["stage"], "to": stage, "note": note}),
            },
        )


def set_completed(
    conn: sqlite3.Connection, app_id: int, completed: bool = True, outcome: str | None = None
) -> None:
    now = utcnow_iso()
    upd: dict[str, Any] = {"completed": int(completed), "updated_at": now}
    if outcome:
        upd["outcome"] = outcome
    if completed:
        upd["follow_up_due"] = None
    with db.transaction(conn):
        db.update(conn, "applications", app_id, upd)
        db.insert(
            conn,
            "application_events",
            {
                "application_id": app_id,
                "at": now,
                "event_type": "completed" if completed else "reopened",
                "data_json": json.dumps({"outcome": outcome}),
            },
        )


def suggestions(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """Follow-ups due and ghosted candidates. Suggestions only — nothing is auto-applied."""
    now = utcnow_iso()
    follow_ups = db.all_rows(
        conn,
        "SELECT * FROM applications WHERE completed = 0 AND follow_up_due IS NOT NULL AND follow_up_due <= ? ORDER BY follow_up_due",
        (now,),
    )
    ghosted = db.all_rows(
        conn,
        "SELECT * FROM applications WHERE completed = 0 AND stage = 'applied' AND first_response_at IS NULL "
        "AND julianday('now') - julianday(applied_at) >= ? ORDER BY applied_at",
        (GHOSTED_AFTER_DAYS,),
    )
    return {"follow_ups_due": follow_ups, "ghosted_candidates": ghosted}


def funnel_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = db.scalar(conn, "SELECT COUNT(*) FROM applications") or 0
    by_stage = {
        r["stage"]: r["n"]
        for r in db.all_rows(conn, "SELECT stage, COUNT(*) n FROM applications GROUP BY stage")
    }
    by_cat = {
        r["k"] or "unknown": r["n"]
        for r in db.all_rows(
            conn, "SELECT target_category k, COUNT(*) n FROM applications GROUP BY target_category"
        )
    }
    by_source = {
        r["k"] or "manual": r["n"]
        for r in db.all_rows(
            conn,
            "SELECT source_of_discovery k, COUNT(*) n FROM applications GROUP BY source_of_discovery",
        )
    }
    by_week = {
        r["wk"]: r["n"]
        for r in db.all_rows(
            conn,
            "SELECT strftime('%Y-W%W', applied_at) wk, COUNT(*) n FROM applications GROUP BY wk ORDER BY wk",
        )
    }
    responded = (
        db.scalar(conn, "SELECT COUNT(*) FROM applications WHERE first_response_at IS NOT NULL")
        or 0
    )
    med = db.scalar(
        conn,
        "SELECT AVG(d) FROM (SELECT julianday(first_response_at) - julianday(applied_at) d FROM applications WHERE first_response_at IS NOT NULL ORDER BY d LIMIT 2 - (SELECT COUNT(*) FROM applications WHERE first_response_at IS NOT NULL) % 2 OFFSET (SELECT (COUNT(*) - 1) / 2 FROM applications WHERE first_response_at IS NOT NULL))",
    )
    return {
        "total": total,
        "by_stage": by_stage,
        "by_category": by_cat,
        "by_source": by_source,
        "by_week": by_week,
        "response_rate": (responded / total) if total else None,
        "median_days_to_first_response": med,
        "active": db.scalar(conn, "SELECT COUNT(*) FROM applications WHERE completed = 0") or 0,
        "completed": db.scalar(conn, "SELECT COUNT(*) FROM applications WHERE completed = 1") or 0,
    }


# ---- URL autofill ---------------------------------------------------------------------------


async def autofill_from_url(conn: sqlite3.Connection, url: str, user_agent: str) -> Autofill:
    """Best-effort: match a known posting, else ask the ATS API, else read the page <title>."""
    norm = _norm_url(url)
    row = db.one(
        conn,
        "SELECT id, company_name, title, locations_json FROM postings WHERE apply_url = ? OR canonical_url = ? OR apply_url LIKE ? LIMIT 1",
        (norm, norm, norm + "%"),
    )
    if row:
        return Autofill(
            company_name=row["company_name"],
            title=row["title"],
            location=_location_label(row),
            posting_id=row["id"],
            source="known_posting",
            confidence=1.0,
        )
    # numeric id match (e.g. the same Greenhouse job id hosted on a company domain)
    m = re.search(r"(\d{6,})", norm)
    if m:
        row = db.one(
            conn,
            "SELECT id, company_name, title, locations_json FROM postings WHERE source_job_id = ? LIMIT 1",
            (m.group(1),),
        )
        if row:
            return Autofill(
                company_name=row["company_name"],
                title=row["title"],
                location=_location_label(row),
                posting_id=row["id"],
                source="known_posting",
                confidence=0.9,
            )

    from radar.fetch.adapters import ADAPTERS
    from radar.fetch.http import PoliteClient

    async with PoliteClient(user_agent, concurrency=2, timeout=20) as client:
        # Company-hosted Greenhouse page (…?gh_jid=123): guess the board slug from the host.
        gm = re.search(r"[?&]gh_jid=(\d+)", norm)
        if gm:
            host_core = urlparse_host(norm).split(".")[-2] if "." in urlparse_host(norm) else ""
            for slug in {host_core, host_core.replace("-", "")}:
                if not slug:
                    continue
                try:
                    resp = await client.get(
                        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{gm.group(1)}",
                        retries=0,
                    )
                except Exception:
                    continue
                if resp.status == 200:
                    j = resp.json()
                    return Autofill(
                        company_name=j.get("company_name") or slug.title(),
                        title=j.get("title"),
                        location=(j.get("location") or {}).get("name"),
                        source="greenhouse_api",
                        confidence=0.9,
                    )
        for provider, cls in ADAPTERS.items():
            spec = cls.detect(norm)
            if not spec:
                continue
            # Greenhouse: /{slug}/jobs/{id} ; Workday: detail path ; Oracle: /job/{id}
            try:
                if provider == "greenhouse":
                    jm = re.search(r"/jobs/(\d+)", norm)
                    if jm:
                        resp = await client.get(
                            f"https://boards-api.greenhouse.io/v1/boards/{spec.slug}/jobs/{jm.group(1)}"
                        )
                        if resp.status == 200:
                            j = resp.json()
                            return Autofill(
                                company_name=j.get("company_name") or spec.slug.title(),
                                title=j.get("title"),
                                location=(j.get("location") or {}).get("name"),
                                source="greenhouse_api",
                                confidence=0.9,
                            )
                elif provider == "workday":
                    from radar.fetch.adapters.workday import base_url

                    jm = re.search(r"(/job/.+)$", urlparse_path(norm))
                    if jm:
                        resp = await client.get(base_url(spec.slug) + jm.group(1))
                        if resp.status == 200:
                            info = resp.json().get("jobPostingInfo") or {}
                            return Autofill(
                                company_name=_tenant_name(spec.slug.split("/")[0]),
                                title=info.get("title"),
                                location=info.get("location"),
                                source="workday_api",
                                confidence=0.85,
                            )
                elif provider == "oracle":
                    from radar.fetch.adapters.oracle import detail_url

                    jm = re.search(r"/job/(\d+)", norm)
                    if jm:
                        resp = await client.get(detail_url(spec.slug, jm.group(1)))
                        if resp.status == 200:
                            items = resp.json().get("items") or []
                            if items:
                                d = items[0]
                                return Autofill(
                                    company_name=_tenant_name(spec.slug.split(".")[0]),
                                    title=d.get("Title"),
                                    location=d.get("PrimaryLocation"),
                                    source="oracle_api",
                                    confidence=0.85,
                                )
            except Exception:
                pass
        # Generic: fetch the page and read <title> / og:title
        try:
            resp = await client.get(norm, follow_redirects=True, retries=1)
        except Exception as e:
            return Autofill(note=f"could not fetch URL: {e}")
        if resp.status != 200:
            return Autofill(note=f"HTTP {resp.status}")
        html = resp.text[:300_000]
        og = re.search(
            r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I
        ) or re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:title["\']', html, re.I)
        tt = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        raw_title = (og.group(1) if og else (tt.group(1) if tt else "")).strip()
        raw_title = re.sub(r"\s+", " ", raw_title)
        company, title = _split_title(raw_title, urlparse_host(norm))
        site = re.search(r'property=["\']og:site_name["\']\s+content=["\']([^"\']+)', html, re.I)
        if site and not company:
            company = site.group(1)
        if company:
            company = (
                re.sub(
                    r"\s*[-–|]?\s*\b(careers?|jobs?|job board|hiring)\b\s*$",
                    "",
                    company,
                    flags=re.I,
                ).strip()
                or company
            )
        return Autofill(
            company_name=company,
            title=title,
            source="html_title",
            confidence=0.5 if title else 0.2,
            note=None if title else "page title not found; enter fields manually",
        )


def urlparse_path(u: str) -> str:
    from urllib.parse import urlparse

    return urlparse(u).path


def urlparse_host(u: str) -> str:
    from urllib.parse import urlparse

    return urlparse(u).netloc


def _tenant_name(t: str) -> str:
    return {
        "capitalone": "Capital One",
        "jpmc": "JPMorgan Chase",
        "mastercard": "Mastercard",
        "nvidia": "NVIDIA",
    }.get(t, t.replace("-", " ").title())


def _split_title(raw: str, host: str) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    for sep in (" | ", " - ", " – ", " — ", " at ", " @ ", " :: "):
        if sep in raw:
            a, b = raw.split(sep, 1)
            a, b = a.strip(), b.strip()
            # heuristically, the company is the shorter side or the one matching the host
            host_core = host.split(".")[-2] if "." in host else host
            if host_core and host_core.lower() in a.lower():
                return a, b
            if host_core and host_core.lower() in b.lower():
                return b, a
            return (b, a) if len(b) < len(a) else (a, b)
    return None, raw


def now_utc() -> datetime:
    return datetime.now(UTC)
