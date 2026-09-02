"""Link verification and rot detection (§14).

Two methods:
  api   — ask the ATS itself whether the job id still exists (Greenhouse/Workday/Oracle/...). Cheap,
          unambiguous, and immune to soft-404 HTML pages.
  http  — GET the apply_url following redirects, then check that the destination still refers to
          the specific req (id in the final URL or in the body). A 200 that landed on a generic
          careers/search page is `dead`, not `live`.

Every check is recorded in link_checks; the posting row carries the latest verdict.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from radar import db
from radar.config import Config
from radar.fetch.adapters import ADAPTERS, get_adapter
from radar.fetch.adapters.base import LinkVerdict
from radar.fetch.http import PoliteClient
from radar.models import SourceSpec
from radar.util import utcnow_iso

log = logging.getLogger("radar.links")

GENERIC_PATHS = re.compile(
    r"^/?(?:$|careers?/?$|jobs?/?$|search/?$|openings?/?$|positions?/?$|en(?:-us)?/?$|home/?$|job-search/?$|all-jobs/?$|opportunities/?$)",
    re.I,
)
NOT_FOUND_TEXT = re.compile(
    r"(job (?:you(?:'re| are) looking for|posting)? ?(?:is )?(?:no longer|not) (?:available|accepting|active|found|open)|"
    r"this (?:job|position|role|posting|opportunity) (?:has been|is) (?:filled|closed|removed|expired|no longer)|"
    r"no longer accepting applications|position has been filled|posting has expired|job not found|page not found|"
    r"we couldn'?t find (?:that|the) (?:job|page)|this job is no longer|sorry,? this (?:job|position))",
    re.I,
)


@dataclass
class VerifyStats:
    checked: int = 0
    live: int = 0
    redirected: int = 0
    dead: int = 0
    unverified: int = 0
    changed: int = 0


def _spec_for(row: sqlite3.Row) -> SourceSpec:
    return SourceSpec(
        provider=row["source_provider"],
        slug=row["source_slug"],
        company_slug=str(row["company_id"] or ""),
        company_name=row["company_name"],
    )


def _ids_for(row: sqlite3.Row) -> list[str]:
    """Identifiers that should still appear at the destination if the req is open."""
    ids = [str(row["source_job_id"])]
    # Numeric tail of the URL is usually the req id too
    m = re.search(r"(\d{5,})(?:[/?#]|$)", row["apply_url"] or "")
    if m and m.group(1) not in ids:
        ids.append(m.group(1))
    return ids


async def verify_http(
    client: PoliteClient, url: str, ids: list[str], title: str | None
) -> LinkVerdict:
    try:
        resp = await client.get(url, follow_redirects=True, max_redirects=6, retries=1)
    except Exception as e:
        return LinkVerdict(
            status="unverified", method="http", reason=f"{type(e).__name__}: {str(e)[:120]}"
        )
    final = resp.final_url
    redirected = bool(resp.history) and urlparse(final).path.rstrip("/") != urlparse(
        url
    ).path.rstrip("/")
    if resp.status in (404, 410):
        return LinkVerdict(
            status="dead",
            method="http",
            http_status=resp.status,
            final_url=final,
            reason=f"HTTP {resp.status}",
        )
    if resp.status >= 400:
        return LinkVerdict(
            status="unverified",
            method="http",
            http_status=resp.status,
            final_url=final,
            reason=f"HTTP {resp.status}",
        )
    body = resp.text[:400_000] if resp.content else ""
    id_in_url = any(i and i in final for i in ids)
    id_in_body = any(i and len(i) >= 4 and i in body for i in ids)
    title_in_body = bool(title) and title.lower()[:40] in body.lower()
    generic_dest = GENERIC_PATHS.match(urlparse(final).path or "/") is not None
    if redirected and generic_dest and not id_in_url:
        return LinkVerdict(
            status="dead",
            method="http",
            http_status=resp.status,
            final_url=final,
            reason="redirected to a generic careers page — req likely closed",
        )
    if NOT_FOUND_TEXT.search(body) and not id_in_body:
        return LinkVerdict(
            status="dead",
            method="http",
            http_status=resp.status,
            final_url=final,
            reason="page says the job is no longer available",
        )
    # Identity is the req id (in the final URL or the body). The title is supporting evidence only:
    # every "Software Engineer" page contains the words "Software Engineer", so a title hit can
    # upgrade an id hit's confidence in the reason text but can never make a page `live` by itself
    # (review finding 4).
    if id_in_url or id_in_body:
        status = "redirected" if redirected else "live"
        return LinkVerdict(
            status=status,
            method="http",
            http_status=resp.status,
            final_url=final,
            reason="destination still references this req id"
            + (" and title" if title_in_body else "")
            + (" (after redirect)" if redirected else ""),
        )
    if redirected:
        return LinkVerdict(
            status="dead",
            method="http",
            http_status=resp.status,
            final_url=final,
            reason="redirected and destination no longer references the req",
        )
    # 200 at the same URL with no req id visible (SPA shell, JS-rendered, or a soft 404): we do not
    # know. Say so rather than calling it live; the API verifier or the next source scan settles it.
    return LinkVerdict(
        status="unverified",
        method="http",
        http_status=resp.status,
        final_url=final,
        reason="HTTP 200 at the original URL but the req id is not visible in the HTML"
        + (" (title text present, which proves nothing)" if title_in_body else "")
        + " — JS-rendered page or soft 404",
    )


async def verify_row(client: PoliteClient, row: sqlite3.Row) -> LinkVerdict:
    provider = row["source_provider"]
    if provider in ADAPTERS:
        adapter = get_adapter(provider, client)
        verdict = await adapter.verify(_spec_for(row), row["source_job_id"], row["apply_url"])
        if verdict.status != "unverified" or verdict.reason != "no api verifier":
            # If the API says live but the apply_url is company-hosted (Greenhouse absolute_url), trust the API:
            # the board API is the source of truth for whether the req is open.
            return verdict
    return await verify_http(client, row["apply_url"], _ids_for(row), row["title"])


def record_verdict(
    conn: sqlite3.Connection, row: sqlite3.Row, v: LinkVerdict, run_id: int | None
) -> bool:
    now = utcnow_iso()
    changed = (row["url_status"] != v.status) and not (
        row["url_status"] == "live" and v.status == "redirected"
    )
    with db.transaction(conn):
        db.insert(
            conn,
            "link_checks",
            {
                "posting_id": row["id"],
                "checked_at": now,
                "url": row["apply_url"],
                "method": v.method,
                "status": v.status,
                "http_status": v.http_status,
                "final_url": v.final_url,
                "reason": v.reason,
            },
        )
        upd: dict[str, Any] = {
            "url_last_verified_at": now,
            "url_verify_method": v.method,
            "url_final": v.final_url,
        }
        if v.status != "unverified":
            upd["url_status"] = v.status
        elif row["url_status"] == "live" and row["url_verify_method"] == "source_presence":
            pass  # keep source-presence "live"; a transient HTTP error doesn't downgrade it
        else:
            upd["url_status"] = "unverified"
        if changed and ("dead" in (row["url_status"], v.status)):
            upd["needs_rescore"] = (
                1  # Today ↔ "verify before dismissing" is decided at scoring time
            )
        db.update(conn, "postings", row["id"], upd)
        if changed:
            db.add_event(
                conn,
                row["id"],
                "link_checked",
                {"from": row["url_status"], "to": v.status, "reason": v.reason, "method": v.method},
                run_id,
            )
    return changed


def select_for_sweep(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    limit: int,
    all_rows: bool = False,
    posting_ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    """Verify what would actually be acted on, in that order — not FIFO across half a million rows.

    Bands (D61): 1 application records · 2 Today bucket · 3 shortlisted · 4 queue by rank ·
    5 dream-list / clearly_better in the ranked set · 6 rows the source itself has not confirmed
    in >3 days (possible ghosts). Everything else is covered by source presence (the board's own
    feed listed it on the last scan), which is already a verification with a timestamp; spending
    HTTP requests re-checking rows nobody would open is why the old sweep never caught up
    (494k "due" rows against ~1,200 checks/day).
    """
    base = "SELECT p.*, c.slug AS company_slug FROM postings p LEFT JOIN companies c ON c.id = p.company_id"
    if posting_ids:
        q = f"{base} WHERE p.id IN ({','.join('?' for _ in posting_ids)})"
        return db.all_rows(conn, q, posting_ids)
    hours = cfg.links.verify_after_hours
    if all_rows:
        cands = db.all_rows(
            conn,
            f"{base} ORDER BY (p.delisted_at IS NOT NULL) DESC, p.url_last_verified_at ASC LIMIT ?",
            (limit * 4,),
        )
    else:
        cands = db.all_rows(
            conn,
            f"""{base}
            WHERE p.delisted_at IS NULL
              AND (p.url_last_verified_at IS NULL OR p.url_verify_method = 'source_presence'
                   OR julianday('now') - julianday(p.url_last_verified_at) > ?/24.0)
              AND (
                    EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id)
                    OR p.queue_action = 'apply_today'
                    OR p.status = 'shortlisted'
                    OR p.apply_priority_rank IS NOT NULL
                    OR julianday('now') - julianday(p.last_seen_at) > 3
                  )
            ORDER BY
              EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id) DESC,
              (p.queue_action = 'apply_today') DESC,
              (p.status = 'shortlisted') DESC,
              (p.apply_priority_rank IS NOT NULL) DESC,
              (p.is_dream_list = 1 OR p.beats_baseline = 'clearly_better') DESC,
              COALESCE(p.apply_priority_rank, 999999) ASC,
              p.url_last_verified_at ASC
            LIMIT ?""",
            (hours, limit * 4),
        )
    # Round-robin across hosts so a polite per-host rate doesn't serialize the whole sweep on one tenant.
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in cands:
        buckets.setdefault(urlparse(r["apply_url"]).netloc, []).append(r)
    out: list[sqlite3.Row] = []
    while len(out) < limit and any(buckets.values()):
        for host in list(buckets):
            if buckets[host]:
                out.append(buckets[host].pop(0))
                if len(out) >= limit:
                    break
            else:
                del buckets[host]
    return out


async def sweep(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    limit: int = 500,
    all_rows: bool = False,
    posting_ids: list[int] | None = None,
    progress: Any = None,
) -> VerifyStats:
    run_id = db.start_run(conn, "verify_links")
    rows = select_for_sweep(conn, cfg, limit=limit, all_rows=all_rows, posting_ids=posting_ids)
    stats = VerifyStats()
    async with PoliteClient(
        cfg.fetch.user_agent,
        concurrency=cfg.fetch.concurrency,
        per_host_concurrency=2,
        timeout=20,
        default_min_interval=1.0 / max(cfg.links.per_host_rps, 0.1),
    ) as client:
        sem = asyncio.Semaphore(cfg.fetch.concurrency)

        async def one(row: sqlite3.Row) -> tuple[sqlite3.Row, LinkVerdict]:
            async with sem:
                return row, await verify_row(client, row)

        for coro in asyncio.as_completed([one(r) for r in rows]):
            row, v = await coro
            changed = record_verdict(conn, row, v, run_id)
            stats.checked += 1
            stats.changed += int(changed)
            setattr(stats, v.status, getattr(stats, v.status) + 1)
            if progress:
                progress(row, v, changed)
    db.finish_run(conn, run_id, stats=stats.__dict__)
    return stats
