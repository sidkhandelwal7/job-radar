"""Slug discovery: turn employers seen in aggregator feeds into registry sources automatically.

For each pending `discovery_queue` row:
  1. detect a supported ATS from the example URL (Greenhouse / Lever / Ashby / Workday / Oracle /
     SmartRecruiters / Workable / Recruitee) via each adapter's detect()
  2. probe the board cheaply (one request) to confirm it's real and count rows
  3. append to config/companies.discovered.yaml (tier 3, category `other` unless inferable) and mark
     the queue row resolved — the next `radar fetch` polls it

URLs on unsupported sites (tesla.com, apple.com, lifeattiktok.com, jobright.ai, …) are marked
`unresolvable` with the reason; those employers stay covered through the aggregator rows.
Companies already in the registry are linked, never duplicated.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from radar import db
from radar.config import CONFIG_DIR, Config
from radar.fetch.adapters import DETECTABLE, get_adapter
from radar.fetch.http import PoliteClient
from radar.fetch.registry import CompanyMatcher
from radar.models import SourceSpec
from radar.util import slugify, utcnow_iso

log = logging.getLogger("radar.discover")
DISCOVERED_PATH = CONFIG_DIR / "companies.discovered.yaml"

# quick category hints from company names (discovery can't know better; the operator can edit)
_CAT_HINTS = [
    (
        re.compile(
            r"\b(bank|bancorp|capital|financial|securities|exchange|asset management|investments?|insurance|mutual|fidelity|schwab|trading|markets)\b",
            re.I,
        ),
        "bank_and_exchange_tech",
    ),
    (
        re.compile(
            r"\b(pay|fintech|lending|credit|card|wallet|crypto|coin|blockchain|treasury)\b", re.I
        ),
        "fintech_infrastructure",
    ),
    (
        re.compile(
            r"\b(defense|aerospace|space|missile|radar|autonomy|drone|government|federal|national lab|laborator)",
            re.I,
        ),
        "defense_and_gov_tech",
    ),
    (re.compile(r"\b(ai|labs?|research|intelligence|ml)\b", re.I), "ai_lab"),
]


@dataclass
class DiscoverStats:
    processed: int = 0
    resolved: int = 0
    linked_existing: int = 0
    unresolvable: int = 0
    failed_probe: int = 0
    added: list[str] = field(default_factory=list)


def load_discovered(path: Path = DISCOVERED_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"version": utcnow_iso()[:10], "companies": []}
    return yaml.safe_load(path.read_text()) or {"companies": []}


def save_discovered(data: dict[str, Any], path: Path = DISCOVERED_PATH) -> None:
    header = (
        "# Auto-discovered employers (radar discover). Same schema as companies.yaml, all tier 3.\n"
        "# Safe to edit: promote entries by moving them into companies.yaml, or set enabled: false.\n"
        "# Re-running discovery never duplicates a slug that already exists in either file.\n\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))


def detect_spec(url: str | None) -> SourceSpec | None:
    if not url:
        return None
    for cls in DETECTABLE:
        try:
            spec = cls.detect(url)
        except Exception:
            spec = None
        if spec:
            return spec
    return None


async def probe(client: PoliteClient, spec: SourceSpec) -> tuple[bool, int, str | None]:
    """One cheap request: (ok, row_count, error)."""
    adapter = get_adapter(spec.provider, client)
    try:
        if spec.provider == "workday":
            from radar.fetch.adapters.workday import base_url

            resp = await client.post(
                base_url(spec.slug) + "/jobs",
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                headers={"Content-Type": "application/json"},
                retries=0,
            )
            if resp.status != 200:
                return False, 0, f"HTTP {resp.status}"
            return True, int(resp.json().get("total") or 0), None
        if spec.provider == "oracle":
            from radar.fetch.adapters.oracle import list_url

            resp = await client.get(list_url(spec.slug, 1, 0), retries=0)
            if resp.status != 200:
                return False, 0, f"HTTP {resp.status}"
            items = resp.json().get("items") or []
            return True, int(items[0].get("TotalJobsCount") or 0) if items else 0, None
        if spec.provider == "smartrecruiters":
            resp = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{spec.slug}/postings?limit=1",
                retries=0,
            )
            if resp.status != 200:
                return False, 0, f"HTTP {resp.status}"
            return True, int(resp.json().get("totalFound") or 0), None
        raw = await adapter.fetch(spec, mode="incremental")
        if raw.error and not raw.jobs:
            return False, 0, raw.error
        return True, len(raw.jobs), None
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {str(e)[:120]}"


def _infer_category(name: str) -> str:
    for pat, cat in _CAT_HINTS:
        if pat.search(name):
            return cat
    return "other"


async def run_discovery(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    limit: int = 300,
    min_seen: int = 1,
    progress: Any = None,
) -> DiscoverStats:
    stats = DiscoverStats()
    rows = db.all_rows(
        conn,
        "SELECT * FROM discovery_queue WHERE status = 'pending' AND seen_count >= ? ORDER BY seen_count DESC, id LIMIT ?",
        (min_seen, limit),
    )
    if not rows:
        return stats
    discovered = load_discovered()
    existing_slugs = {c["slug"] for c in discovered["companies"]}
    from radar.fetch.registry import load_registry

    curated = load_registry()
    existing_slugs |= {c.slug for c in curated}
    existing_sources = {(s["provider"], s["slug"]) for c in curated for s in c.sources} | {
        (s["provider"], s["slug"]) for c in discovered["companies"] for s in c.get("sources", [])
    }
    matcher = CompanyMatcher(conn)
    now = utcnow_iso()

    async with PoliteClient(
        cfg.fetch.user_agent, concurrency=6, per_host_concurrency=2, timeout=20
    ) as client:
        sem = asyncio.Semaphore(6)

        async def one(
            row: sqlite3.Row,
        ) -> tuple[sqlite3.Row, SourceSpec | None, tuple[bool, int, str | None] | None]:
            spec = detect_spec(row["example_url"])
            if spec is None:
                return row, None, None
            if (spec.provider, spec.slug) in existing_sources:
                return row, spec, (True, -1, "already in registry")
            async with sem:
                return row, spec, await probe(client, spec)

        results = await asyncio.gather(*(one(r) for r in rows))

    with db.transaction(conn):
        for row, spec, res in results:
            stats.processed += 1
            upd: dict[str, Any] = {"last_seen_at": now}
            hit = matcher.match(row["company_name"])
            if hit:
                upd.update(
                    {
                        "status": "resolved",
                        "resolved_company_id": hit[0],
                        "note": "matched existing registry company",
                    }
                )
                stats.linked_existing += 1
                db.update(conn, "discovery_queue", row["id"], upd)
                if progress:
                    progress(row, "linked", hit[1])
                continue
            if spec is None:
                upd.update(
                    {
                        "status": "unresolvable",
                        "note": "no supported ATS detectable from URL (covered via aggregators)",
                    }
                )
                stats.unresolvable += 1
                db.update(conn, "discovery_queue", row["id"], upd)
                if progress:
                    progress(row, "unresolvable", None)
                continue
            ok, n, err = res or (False, 0, "no probe")
            upd.update({"detected_provider": spec.provider, "detected_slug": spec.slug})
            if not ok:
                upd.update(
                    {
                        "status": "unresolvable",
                        "note": f"detected {spec.provider}:{spec.slug} but probe failed: {err}",
                    }
                )
                stats.failed_probe += 1
                db.update(conn, "discovery_queue", row["id"], upd)
                if progress:
                    progress(row, "probe_failed", err)
                continue
            if err == "already in registry":
                upd.update(
                    {
                        "status": "resolved",
                        "note": f"source {spec.provider}:{spec.slug} already registered",
                    }
                )
                stats.linked_existing += 1
                db.update(conn, "discovery_queue", row["id"], upd)
                continue
            cslug = slugify(row["company_name"]) or slugify(spec.slug)
            base = cslug
            i = 2
            while cslug in existing_slugs:
                cslug = f"{base}-{i}"
                i += 1
            entry = {
                "slug": cslug,
                "name": row["company_name"],
                "tier": 3,
                "category": _infer_category(row["company_name"]),
                "discovered_at": now[:10],
                "discovered_from": row["example_url"],
                "sources": [{"provider": spec.provider, "slug": spec.slug}],
            }
            discovered["companies"].append(entry)
            existing_slugs.add(cslug)
            existing_sources.add((spec.provider, spec.slug))
            upd.update(
                {"status": "resolved", "note": f"added {spec.provider}:{spec.slug} ({n} rows)"}
            )
            stats.resolved += 1
            stats.added.append(f"{row['company_name']} → {spec.provider}:{spec.slug} ({n})")
            db.update(conn, "discovery_queue", row["id"], upd)
            if progress:
                progress(row, "added", f"{spec.provider}:{spec.slug} ({n} rows)")
    if stats.resolved:
        discovered["version"] = now[:10]
        save_discovered(discovered)
    return stats
