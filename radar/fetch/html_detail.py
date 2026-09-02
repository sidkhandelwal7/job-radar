"""Descriptions for aggregator-only rows (Google, Amazon, Apple, …) by reading the employer's own
posting page — politely, robots.txt-respecting, never for hosts that forbid automated access (§17).

Preferred extraction: schema.org JobPosting JSON-LD (`description`), which most large career sites
embed. Fallback: the largest text block inside <main>/<article>/<body> after stripping chrome.
Pages are stored in the raw store (kind=html_detail) like every other payload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import urllib.robotparser
from typing import Any
from urllib.parse import urlparse

from radar import db
from radar.config import Config
from radar.fetch.adapters.base import html_to_md
from radar.fetch.http import PoliteClient
from radar.fetch.raw_store import RawStore
from radar.parse.posting import derive_from_description

log = logging.getLogger("radar.html_detail")

# Hosts whose terms/robots forbid automated collection, or that are aggregators themselves.
DENY_HOSTS = (
    "metacareers.com",
    "facebook.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "jobright.ai",
    "simplify.jobs",
)
_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)
_BLOCK = re.compile(
    r"<(script|style|nav|header|footer|noscript|svg|form)[^>]*>.*?</\1>", re.I | re.S
)
_MAIN = re.compile(r"<(main|article)[^>]*>(.*?)</\1>", re.I | re.S)


class Robots:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client
        self.cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        if any(host == d or host.endswith("." + d) for d in DENY_HOSTS):
            return False
        if host not in self.cache:
            # Fail CLOSED (review finding 8, RFC 9309 §2.3.1.4): if we cannot read robots.txt —
            # DNS error, timeout, 401/403/429, 5xx — we do not know what the host allows, so we
            # fetch nothing from it this run. Only an explicit 404/410 ("no robots file") permits.
            rp: urllib.robotparser.RobotFileParser | None = None
            try:
                resp = await self.client.get(
                    f"https://{host}/robots.txt", retries=0, follow_redirects=True
                )
                if resp.status == 200:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(resp.text.splitlines())
                elif resp.status in (404, 410):
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse([])  # no robots file: nothing is disallowed
                else:
                    log.info(
                        "robots.txt for %s answered HTTP %s — denying this run", host, resp.status
                    )
            except Exception as e:
                log.info(
                    "robots.txt for %s unreachable (%s) — denying this run", host, type(e).__name__
                )
            self.cache[host] = rp
        rp = self.cache[host]
        return bool(rp) and rp.can_fetch(self.client.user_agent, url)


def extract_description(html: str) -> tuple[str | None, str]:
    """Return (markdown, method)."""
    for m in _JSONLD.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and it.get("@graph"):
                items.extend(x for x in it["@graph"] if isinstance(x, dict))
            if (
                isinstance(it, dict)
                and str(it.get("@type", "")).lower() == "jobposting"
                and it.get("description")
            ):
                md = html_to_md(it["description"])
                if md and len(md) > 200:
                    return md, "jsonld"
    body = _BLOCK.sub(" ", html)
    mains = [m.group(2) for m in _MAIN.finditer(body)] or [body]
    best = max(mains, key=lambda x: len(re.sub(r"<[^>]+>", "", x)))
    md = html_to_md(best)
    if md and len(md) > 400:
        # trim obvious chrome lines
        lines = [ln for ln in md.splitlines() if len(ln.strip()) > 2]
        return "\n".join(lines)[:20000], "main_block"
    return None, "none"


def _apply(
    conn: sqlite3.Connection, r: sqlite3.Row, md: str, method: str, run_id: int | None
) -> None:
    derived = derive_from_description(md, r["title"])
    tags = derived.pop("tech_tags", [])
    upd: dict[str, Any] = {
        "description_fetched": 1,
        "tech_tags_json": json.dumps(tags),
        "scored_at": None,
    }
    cur = db.one(
        conn, "SELECT base_posted_min, graduation_window FROM postings WHERE id = ?", (r["id"],)
    )
    for k, v in derived.items():
        if k.startswith("base_posted") or k in (
            "comp_source",
            "base_posted_currency",
            "base_posted_interval",
        ):
            if cur["base_posted_min"] is None:
                upd[k] = v
        elif k == "graduation_window":
            if v and not cur["graduation_window"]:
                upd[k] = v
        else:
            upd[k] = v
    with db.transaction(conn):
        db.upsert_doc(conn, r["id"], description_md=md)
        db.update(conn, "postings", r["id"], upd)
        db.add_event(
            conn, r["id"], "html_detail_fetched", {"method": method, "chars": len(md)}, run_id
        )


async def fetch_via_ats(client: PoliteClient, url: str) -> tuple[str | None, str]:
    """If the URL identifies a job on a supported ATS, get the description from that ATS's JSON API."""
    import html as htmllib
    import re as _re

    from radar.fetch.adapters import DETECTABLE, get_adapter
    from radar.models import RawJob

    spec = None
    for cls in DETECTABLE:
        spec = cls.detect(url)
        if spec:
            break
    if not spec:
        return None, "not an ATS url"
    adapter = get_adapter(spec.provider, client)
    path = urlparse(url).path
    try:
        if spec.provider == "greenhouse":
            m = _re.search(r"/jobs/(\d+)", path) or _re.search(r"gh_jid=(\d+)", url)
            if not m:
                return None, "no greenhouse id"
            resp = await client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{spec.slug}/jobs/{m.group(1)}",
                retries=1,
            )
            if resp.status != 200:
                return None, f"greenhouse HTTP {resp.status}"
            return html_to_md(htmllib.unescape(resp.json().get("content") or "")), "greenhouse_api"
        if spec.provider == "lever":
            m = _re.search(r"/([0-9a-f-]{36})", path)
            if not m:
                return None, "no lever id"
            resp = await client.get(
                f"https://api.lever.co/v0/postings/{spec.slug}/{m.group(1)}?mode=json", retries=1
            )
            if resp.status != 200:
                return None, f"lever HTTP {resp.status}"
            jobs = adapter.parse_page(spec, json.dumps([resp.json()]).encode())
            return (jobs[0].description_md if jobs else None), "lever_api"
        if spec.provider == "ashby":
            m = _re.search(r"/([0-9a-f-]{36})", path)
            if not m:
                return None, "no ashby id"
            resp = await client.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{spec.slug}?includeCompensation=true",
                retries=1,
            )
            if resp.status != 200:
                return None, f"ashby HTTP {resp.status}"
            for j in adapter.parse_page(spec, resp.content):
                if j.source_job_id == m.group(1):
                    return j.description_md, "ashby_api"
            return None, "ashby id not on board"
        if spec.provider == "workday":
            from radar.fetch.adapters.workday import base_url

            m = _re.search(r"(/job/.+)$", path)
            if not m:
                return None, "no workday path"
            resp = await client.get(base_url(spec.slug) + m.group(1), retries=1)
            if resp.status != 200:
                return None, f"workday HTTP {resp.status}"
            job = RawJob(source_job_id="x", title="", apply_url=url)
            adapter.apply_detail(job, resp.content)
            return job.description_md, "workday_api"
        if spec.provider == "oracle":
            from radar.fetch.adapters.oracle import detail_url

            m = _re.search(r"/job/(\d+)", path)
            if not m:
                return None, "no oracle id"
            resp = await client.get(detail_url(spec.slug, m.group(1)), retries=1)
            if resp.status != 200:
                return None, f"oracle HTTP {resp.status}"
            job = RawJob(source_job_id=m.group(1), title="", apply_url=url)
            adapter.apply_detail(job, resp.content)
            return job.description_md, "oracle_api"
        if spec.provider == "smartrecruiters":
            m = _re.search(r"/(\d{9,})", path)
            if not m:
                return None, "no smartrecruiters id"
            resp = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{spec.slug}/postings/{m.group(1)}",
                retries=1,
            )
            if resp.status != 200:
                return None, f"smartrecruiters HTTP {resp.status}"
            job = RawJob(source_job_id=m.group(1), title="", apply_url=url)
            adapter.apply_detail(job, resp.content)
            return job.description_md, "smartrecruiters_api"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:80]}"
    return None, f"no api path for {spec.provider}"


async def fetch_missing(
    conn: sqlite3.Connection, cfg: Config, *, limit: int = 40, run_id: int | None = None
) -> dict[str, Any]:
    rows = db.all_rows(
        conn,
        "SELECT id, apply_url, canonical_url, title, company_name, source_provider FROM postings "
        "WHERE description_fetched = 0 AND source_provider = 'github' AND is_cluster_canonical = 1 AND delisted_at IS NULL "
        "AND (priority > 0 OR is_dream_list = 1 OR company_tier = 1) AND in_scope = 1 "
        "AND NOT EXISTS (SELECT 1 FROM posting_events e WHERE e.posting_id = postings.id AND e.event_type = 'html_detail_failed' AND e.at > datetime('now', '-7 days')) "
        "ORDER BY (apply_priority_rank IS NULL), apply_priority_rank ASC, priority DESC LIMIT ?",
        (limit,),
    )
    stats = {"candidates": len(rows), "fetched": 0, "robots_blocked": 0, "failed": 0}
    if not rows:
        return stats
    store = RawStore(cfg.raw_dir)
    async with PoliteClient(
        cfg.fetch.user_agent,
        concurrency=4,
        per_host_concurrency=1,
        timeout=25,
        default_min_interval=1.0,
    ) as client:
        robots = Robots(client)

        async def one(r: sqlite3.Row) -> None:
            url = r["apply_url"]
            md, method = await fetch_via_ats(client, url)
            if md:
                _apply(conn, r, md, method, run_id)
                stats["fetched"] += 1
                return
            if not await robots.allowed(url):
                stats["robots_blocked"] += 1
                db.add_event(
                    conn,
                    r["id"],
                    "html_detail_failed",
                    {"reason": "robots.txt disallows or host denied"},
                    run_id,
                )
                return
            try:
                resp = await client.get(url, follow_redirects=True, retries=1)
            except Exception as e:
                stats["failed"] += 1
                db.add_event(conn, r["id"], "html_detail_failed", {"reason": str(e)[:120]}, run_id)
                return
            if resp.status != 200 or not resp.content:
                stats["failed"] += 1
                db.add_event(
                    conn, r["id"], "html_detail_failed", {"reason": f"HTTP {resp.status}"}, run_id
                )
                if resp.status in (404, 410):  # that GET was a link check: the posting is gone
                    from radar.fetch.adapters.base import LinkVerdict
                    from radar.links import record_verdict

                    full = db.one(conn, "SELECT * FROM postings WHERE id = ?", (r["id"],))
                    record_verdict(
                        conn,
                        full,
                        LinkVerdict(
                            status="dead",
                            method="http",
                            http_status=resp.status,
                            final_url=resp.final_url,
                            reason=f"HTTP {resp.status} fetching the posting page",
                        ),
                        run_id,
                    )
                return
            store.store(
                conn,
                provider="html",
                slug=urlparse(url).netloc,
                url=url,
                content=resp.content,
                http_status=resp.status,
                run_id=run_id,
                source_id=None,
                kind="html_detail",
                row_count=1,
            )
            md, method = extract_description(resp.text)
            if not md:
                stats["failed"] += 1
                db.add_event(
                    conn,
                    r["id"],
                    "html_detail_failed",
                    {"reason": "no description block found"},
                    run_id,
                )
                return
            _apply(conn, r, md, method, run_id)
            stats["fetched"] += 1

        await asyncio.gather(*(one(r) for r in rows))
    return stats
