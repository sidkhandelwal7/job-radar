"""Company registry: companies.yaml ↔ companies / company_sources tables, plus name matching."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from radar import db
from radar.config import CONFIG_DIR, Config
from radar.models import SourceSpec
from radar.util import utcnow_iso

_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|group|holdings|technologies|technology|labs|ai|io|hq|the)\b\.?",
    re.I,
)
_NONALNUM = re.compile(r"[^a-z0-9]+")


def normalize_company_name(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = _SUFFIX.sub(" ", n)
    n = _NONALNUM.sub(" ", n).strip()
    return re.sub(r"\s+", " ", n)


@dataclass
class CompanyEntry:
    slug: str
    name: str
    aliases: list[str]
    tier: int | None
    category: str | None
    quant_firm: bool
    hq_metro: str | None
    tags: list[str]
    alumni_presence: str | None
    enabled: bool
    note: str | None
    sources: list[dict[str, Any]]


@lru_cache(maxsize=1)
def load_registry(path: Path | None = None) -> list[CompanyEntry]:
    """companies.yaml + companies.discovered.yaml (curated wins on slug collisions)."""
    p = path or (CONFIG_DIR / "companies.yaml")
    data = yaml.safe_load(p.read_text()) or {}
    entries = list(data.get("companies", []))
    seen = {c["slug"] for c in entries}
    disc = CONFIG_DIR / "companies.discovered.yaml"
    if path is None and disc.exists():
        for c in (yaml.safe_load(disc.read_text()) or {}).get("companies", []):
            if c["slug"] not in seen:
                entries.append(c)
                seen.add(c["slug"])
    out: list[CompanyEntry] = []
    for c in entries:
        out.append(
            CompanyEntry(
                slug=c["slug"],
                name=c["name"],
                aliases=list(c.get("aliases") or []),
                tier=c.get("tier"),
                category=c.get("category"),
                quant_firm=bool(c.get("quant_firm", False)),
                hq_metro=c.get("hq_metro"),
                tags=list(c.get("tags") or []),
                alumni_presence=c.get("alumni_presence"),
                enabled=bool(c.get("enabled", True)),
                note=c.get("note"),
                sources=list(c.get("sources") or []),
            )
        )
    return out


def cadence_for(entry: CompanyEntry, cfg: Config) -> str:
    if entry.slug in cfg.dream_list:
        return "15min"
    if entry.tier == 1:
        return "15min"
    if entry.tier == 2:
        return "hourly"
    return "6h"


def sync_registry(
    conn: sqlite3.Connection, cfg: Config, entries: list[CompanyEntry] | None = None
) -> dict[str, int]:
    """Upsert companies.yaml into the DB. Never deletes; disabled entries are marked enabled=0."""
    entries = entries or load_registry()
    now = utcnow_iso()
    n_companies = n_sources = 0
    with db.transaction(conn):
        for e in entries:
            is_dream = int(e.slug in cfg.dream_list)
            floor_exempt = int(cfg.is_floor_exempt(e.slug))
            is_quant = int(e.quant_firm or is_quant_firm_name(e.name, e.slug))
            row = db.one(conn, "SELECT id FROM companies WHERE slug = ?", (e.slug,))
            values = {
                "name": e.name,
                "aliases_json": json.dumps(e.aliases),
                "tier": e.tier,
                "is_dream_list": is_dream,
                "floor_exempt": floor_exempt,
                "is_quant_trading_firm": is_quant,
                "target_category": e.category,
                "tags_json": json.dumps(e.tags),
                "hq_metro": e.hq_metro,
                "alumni_presence": e.alumni_presence,
                "notes_md": e.note,
                "updated_at": now,
            }
            if row:
                cid = row["id"]
                db.update(conn, "companies", cid, values)
            else:
                values.update({"slug": e.slug, "created_at": now})
                cid = db.insert(conn, "companies", values)
            n_companies += 1
            for s in e.sources:
                provider, slug = s["provider"], s["slug"]
                srow = db.one(
                    conn,
                    "SELECT id FROM company_sources WHERE provider = ? AND slug = ?",
                    (provider, slug),
                )
                svalues = {
                    "company_id": cid,
                    "careers_url": s.get("careers_url"),
                    "cadence": s.get("cadence") or cadence_for(e, cfg),
                    "enabled": int(e.enabled and bool(s.get("enabled", True))),
                }
                if srow:
                    if db.scalar(
                        conn,
                        "SELECT disabled_reason FROM company_sources WHERE id = ?",
                        (srow["id"],),
                    ):
                        svalues.pop("enabled")  # an audited disable outlives YAML sync (D62)
                    db.update(conn, "company_sources", srow["id"], svalues)
                else:
                    svalues.update({"provider": provider, "slug": slug})
                    db.insert(conn, "company_sources", svalues)
                n_sources += 1
    n_aggs = sync_aggregators(conn)
    n_presets = sync_presets(conn)
    return {
        "companies": n_companies,
        "sources": n_sources,
        "aggregators": n_aggs,
        "presets": n_presets,
    }


def sync_presets(conn: sqlite3.Connection) -> int:
    """config/presets.yaml → saved_filters (is_preset=1). User-created saved filters are untouched."""
    p = CONFIG_DIR / "presets.yaml"
    if not p.exists():
        return 0
    now = utcnow_iso()
    n = 0
    with db.transaction(conn):
        for pr in (yaml.safe_load(p.read_text()) or {}).get("presets", []):
            row = db.one(conn, "SELECT id FROM saved_filters WHERE name = ?", (pr["name"],))
            values = {
                "query": pr.get("query") or "",
                "is_preset": 1,
                "alert_tier": pr.get("alert"),
                "sort": pr.get("sort"),
                "updated_at": now,
            }
            if row:
                db.update(conn, "saved_filters", row["id"], values)
            else:
                db.insert(conn, "saved_filters", {**values, "name": pr["name"], "created_at": now})
            n += 1
    return n


def sync_aggregators(conn: sqlite3.Connection) -> int:
    """Each aggregator repo is a pseudo-company with one `github` source polled every 15 min."""
    from radar.fetch.adapters.github_aggregators import load_aggregators

    now = utcnow_iso()
    n = 0
    with db.transaction(conn):
        for a in load_aggregators():
            cslug = "aggregator-" + a["slug"].split("/")[0].lower()
            cslug = (
                cslug
                if not db.one(
                    conn,
                    "SELECT id FROM companies WHERE slug = ? AND name != ?",
                    (cslug, a["name"]),
                )
                else "aggregator-" + a["slug"].lower().replace("/", "-")
            )
            row = db.one(conn, "SELECT id FROM companies WHERE name = ?", (a["name"],))
            values = {
                "name": a["name"],
                "tags_json": json.dumps(["aggregator"]),
                "notes_md": a.get("url"),
                "updated_at": now,
            }
            if row:
                cid = row["id"]
                db.update(conn, "companies", cid, values)
            else:
                values.update({"slug": cslug, "created_at": now})
                cid = db.insert(conn, "companies", values)
            srow = db.one(
                conn,
                "SELECT id FROM company_sources WHERE provider = 'github' AND slug = ?",
                (a["slug"],),
            )
            svalues = {
                "company_id": cid,
                "careers_url": a.get("url"),
                "cadence": "15min",
                "enabled": int(a.get("enabled", True)),
            }
            if srow:
                db.update(conn, "company_sources", srow["id"], svalues)
            else:
                svalues.update({"provider": "github", "slug": a["slug"]})
                db.insert(conn, "company_sources", svalues)
            n += 1
    return n


@lru_cache(maxsize=1)
def _quant_aliases() -> set[str]:
    """Registry slugs AND normalized name aliases of §1c quant-trading firms."""
    p = CONFIG_DIR / "quant_firms.yaml"
    if not p.exists():
        return set()
    data = yaml.safe_load(p.read_text()) or {}
    out: set[str] = set()
    for q in data.get("quant_trading_firms", []):
        if q.get("slug"):
            out.add(q["slug"])
        for a in [q.get("name"), *(q.get("aliases") or [])]:
            if a:
                out.add(normalize_company_name(a))
    return out


def is_quant_firm_name(name: str | None, slug: str | None = None) -> bool:
    aliases = _quant_aliases()
    if slug and slug in aliases:
        return True
    n = normalize_company_name(name or "")
    if not n:
        return False
    return n in aliases or any(len(a) >= 5 and (n.startswith(a + " ") or n == a) for a in aliases)


CADENCE_MINUTES = {"15min": 15, "hourly": 60, "6h": 360, "daily": 1440}


def source_specs(
    conn: sqlite3.Connection,
    *,
    providers: set[str] | None = None,
    company: str | None = None,
    only_enabled: bool = True,
    due_only: bool = False,
) -> list[SourceSpec]:
    """Registry sources as SourceSpecs. due_only=True keeps only sources whose cadence has elapsed
    since their last fetch (failed sources back off: cadence x 2^failures, capped at a day)."""
    sql = (
        "SELECT cs.*, c.slug AS company_slug, c.name AS company_name, c.tier AS company_tier, "
        "c.is_dream_list, c.target_category, c.is_quant_trading_firm FROM company_sources cs "
        "JOIN companies c ON c.id = cs.company_id WHERE 1=1"
    )
    params: list[Any] = []
    if only_enabled:
        sql += " AND cs.enabled = 1"
    if providers:
        sql += f" AND cs.provider IN ({','.join('?' for _ in providers)})"
        params.extend(sorted(providers))
    if company:
        sql += " AND (c.slug = ? OR c.name LIKE ?)"
        params.extend([company, f"%{company}%"])
    sql += " ORDER BY c.is_dream_list DESC, c.tier ASC, c.name ASC"
    specs: list[SourceSpec] = []
    for r in db.all_rows(conn, sql, params):
        if due_only and not _is_due(r):
            continue
        specs.append(
            SourceSpec(
                provider=r["provider"],
                slug=r["slug"],
                company_slug=r["company_slug"],
                company_name=r["company_name"],
                careers_url=r["careers_url"],
                cadence=r["cadence"],
                extra={
                    "source_id": r["id"],
                    "company_id": r["company_id"],
                    "etag": r["etag"],
                    "last_modified": r["last_modified"],
                    "company_tier": r["company_tier"],
                    "is_dream_list": r["is_dream_list"],
                    "target_category": r["target_category"],
                    "is_quant_trading_firm": r["is_quant_trading_firm"],
                },
            )
        )
    return specs


def _is_due(r: sqlite3.Row) -> bool:
    from radar.util import parse_dt, utcnow

    last = parse_dt(r["last_fetched_at"])
    if last is None:
        return True
    minutes = CADENCE_MINUTES.get(r["cadence"] or "hourly", 60)
    failures = int(r["consecutive_failures"] or 0)
    if failures:
        minutes = min(1440, minutes * (2 ** min(failures, 6)))
    # 10% early tolerance so a 15-min cron firing at 14m50s still counts
    return (utcnow() - last).total_seconds() >= minutes * 60 * 0.9


class CompanyMatcher:
    """Resolve free-text company names (from aggregators / manual entry) to registry companies."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._by_norm: dict[str, tuple[int, str]] = {}
        for r in db.all_rows(conn, "SELECT id, slug, name, aliases_json FROM companies"):
            names = [r["name"], *json.loads(r["aliases_json"] or "[]"), r["slug"].replace("-", " ")]
            for n in names:
                key = normalize_company_name(n)
                if key and key not in self._by_norm:
                    self._by_norm[key] = (r["id"], r["slug"])

    def match(self, name: str | None) -> tuple[int, str] | None:
        if not name:
            return None
        key = normalize_company_name(name)
        if key in self._by_norm:
            return self._by_norm[key]
        # tolerant: registry name is a prefix/suffix token set of the given name
        for k, v in self._by_norm.items():
            if len(k) >= 4 and (key.startswith(k + " ") or key.endswith(" " + k)):
                return v
        return None
