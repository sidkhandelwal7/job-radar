"""The always-on fetch layer for GitHub Actions (§4 "the laptop problem").

Runs WITHOUT the SQLite database. State lives in the repo:

  actions/state.json.gz        per-source ETags + the set of job ids already seen
  actions/deltas/YYYY-MM-DD.jsonl   one compact line per newly seen posting, with a deterministic
                                    pre-score (p0 / p1 / none) — no LLM, ever
  actions/runs.jsonl           one line per run: timing, requests, minute accounting (budget guard)

The laptop ingests deltas with `radar ingest-deltas` (first_seen_at is backdated to when Actions
saw the posting; rows the laptop hasn't fetched yet are inserted provisionally from the delta).
Phase 5 sends P0/P1 notifications from here, so a sleeping laptop never means a silent phone.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.config import PROJECT_ROOT, Config
from radar.fetch.adapters import get_adapter
from radar.fetch.http import PoliteClient
from radar.fetch.registry import CompanyEntry, cadence_for, load_registry, normalize_company_name
from radar.models import RawJob, SourceSpec
from radar.parse.posting import build_posting_values
from radar.parse.titles import ENGINEERING_FAMILIES
from radar.util import utcnow, utcnow_iso

log = logging.getLogger("radar.actions")

ACTIONS_DIR = PROJECT_ROOT / "actions"
STATE_VERSION = 1
MAX_SEEN_PER_SOURCE = (
    12000  # newest ids are kept; a dropped old id re-seen later only costs a duplicate delta line
)


@dataclass
class SourceState:
    etag: str | None = None
    last_modified: str | None = None
    seen: list[str] = field(default_factory=list)
    last_full_at: str | None = None
    last_ok_at: str | None = None
    failures: int = 0


@dataclass
class RunStats:
    started_at: str
    host: str = "actions"
    sources: int = 0
    ok: int = 0
    not_modified: int = 0
    failed: int = 0
    new: int = 0
    p0: int = 0
    p1: int = 0
    requests: int = 0
    bytes: int = 0
    elapsed_s: float = 0.0
    seed: bool = False


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "sources": {}}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(state, f, separators=(",", ":"))


def tier1_specs(cfg: Config, entries: list[CompanyEntry] | None = None) -> list[SourceSpec]:
    """15-minute sources (dream list + Tier-1) and the aggregator repos, straight from YAML."""
    from radar.fetch.adapters.github_aggregators import load_aggregators

    specs: list[SourceSpec] = []
    for e in entries or load_registry():
        if not e.enabled or cadence_for(e, cfg) != "15min":
            continue
        for s in e.sources:
            specs.append(
                SourceSpec(
                    provider=s["provider"],
                    slug=s["slug"],
                    company_slug=e.slug,
                    company_name=e.name,
                    careers_url=s.get("careers_url"),
                    cadence="15min",
                    extra={
                        "company_tier": e.tier,
                        "is_dream_list": int(e.slug in cfg.dream_list),
                        "target_category": e.category,
                        "is_quant_trading_firm": int(e.quant_firm),
                    },
                )
            )
    for a in load_aggregators():
        if a.get("enabled", True):
            specs.append(
                SourceSpec(
                    provider="github",
                    slug=a["slug"],
                    company_slug="aggregator",
                    company_name=a["name"],
                    cadence="15min",
                    extra={"aggregator": a},
                )
            )
    return specs


class YamlCompanyResolver:
    """CompanyMatcher without a database: registry names/aliases → registry info."""

    def __init__(self, cfg: Config, entries: list[CompanyEntry]) -> None:
        self._by_norm: dict[str, dict[str, Any]] = {}
        for e in entries:
            info = {
                "id": None,
                "name": e.name,
                "tier": e.tier,
                "is_dream_list": int(e.slug in cfg.dream_list),
                "target_category": e.category,
                "is_quant_trading_firm": int(e.quant_firm),
            }
            for n in (e.name, *e.aliases, e.slug.replace("-", " ")):
                k = normalize_company_name(n)
                if k and k not in self._by_norm:
                    self._by_norm[k] = info

    def resolve(self, job: RawJob) -> dict[str, Any]:
        name = (job.company_name or "").strip()
        hit = self._by_norm.get(normalize_company_name(name))
        if hit:
            return hit
        return {
            "id": None,
            "name": name or None,
            "tier": None,
            "is_dream_list": 0,
            "target_category": None,
            "is_quant_trading_firm": 0,
        }


def prescore(values: dict[str, Any]) -> tuple[str, str]:
    """Deterministic P0/P1 decision (§20), good enough to page from the cloud. Returns (tier, reason)."""
    from radar.parse.quals import title_hard_seniority

    eng = values.get("role_family") in ENGINEERING_FAMILIES
    new_grad = bool(values.get("is_new_grad")) or values.get("seniority") == "new_grad"
    excluded = (
        title_hard_seniority(values.get("title")) is not None  # D63: the title wins
        or (values.get("min_years_experience") or 0) == 2  # stretch band never pages
        or (
            values.get("employment_type") in ("internship", "contract", "part_time")
            or values.get("is_international_only")
            or values.get("requires_clearance") == 1
            or values.get("requires_advanced_degree") == 1
            or values.get("seniority")
            in ("senior", "staff", "principal", "manager", "executive", "internship")
            or (values.get("min_years_experience") or 0) >= 3
        )
    )
    if excluded or not eng:
        return "none", "out of scope"
    if values.get("is_dream_list") and new_grad:
        return "p0", "dream-list company opened a new-grad engineering req"
    if (values.get("company_tier") == 1) and new_grad:
        return "p1", "Tier-1 new-grad engineering req"
    if values.get("is_dream_list") and values.get("min_years_experience") is not None:
        return "p1", "dream-list engineering req"
    return "none", "in scope, not alert-worthy"


def _delta_row(
    values: dict[str, Any], spec: SourceSpec, tier: str, reason: str, now: str
) -> dict[str, Any]:
    return {
        "seen_at": now,
        "provider": spec.provider,
        "slug": spec.slug,
        "job_id": values["source_job_id"],
        "company": values["company_name"],
        "company_slug": spec.company_slug if spec.provider != "github" else None,
        "title": values["title"],
        "apply_url": values["apply_url"],
        "canonical_url": values.get("canonical_url"),
        "locations": [loc["raw"] for loc in json.loads(values["locations_json"])][:6],
        "primary_metro": values.get("primary_metro"),
        "posted_at": values.get("posted_at"),
        "seniority": values.get("seniority"),
        "role_family": values.get("role_family"),
        "is_new_grad": values.get("is_new_grad"),
        "employment_type": values.get("employment_type"),
        "base_min": values.get("base_posted_min"),
        "base_max": values.get("base_posted_max"),
        "is_dream_list": values.get("is_dream_list"),
        "company_tier": values.get("company_tier"),
        "target_category": values.get("target_category"),
        "alert": tier,
        "alert_reason": reason,
    }


async def run_actions_cycle(
    cfg: Config,
    *,
    actions_dir: Path = ACTIONS_DIR,
    detail_policy: str = "none",
    specs: list[SourceSpec] | None = None,
) -> RunStats:
    t0 = time.monotonic()
    now = utcnow_iso()
    stats = RunStats(started_at=now)
    state_path = actions_dir / "state.json.gz"
    state = load_state(state_path)
    stats.seed = not state[
        "sources"
    ]  # first run: everything is "new"; notifications must skip seeds
    entries = load_registry()
    resolver = YamlCompanyResolver(cfg, entries)
    specs = specs if specs is not None else tier1_specs(cfg, entries)
    stats.sources = len(specs)
    deltas: list[dict[str, Any]] = []

    async with PoliteClient(
        cfg.fetch.user_agent,
        concurrency=cfg.fetch.concurrency,
        per_host_concurrency=cfg.fetch.per_host_concurrency,
        timeout=25,
    ) as client:
        sem = asyncio.Semaphore(cfg.fetch.concurrency)

        async def one(spec: SourceSpec) -> None:
            key = spec.key
            st = SourceState(**state["sources"].get(key, {}))
            adapter = get_adapter(spec.provider, client)
            # full scan once per 6h per source (keeps `seen` honest), incremental otherwise
            full_due = (
                st.last_full_at is None
                or (
                    utcnow() - datetime.fromisoformat(st.last_full_at.replace("Z", "+00:00"))
                ).total_seconds()
                > 6 * 3600
            )
            mode = "full" if full_due else "incremental"
            async with sem:
                try:
                    raw = await adapter.fetch(
                        spec, mode=mode, etag=st.etag, last_modified=st.last_modified
                    )
                except Exception as e:
                    st.failures += 1
                    state["sources"][key] = asdict(st)
                    stats.failed += 1
                    log.warning("%s failed: %s", key, e)
                    return
            stats.requests += raw.requests_made
            stats.bytes += raw.bytes_downloaded
            if raw.not_modified:
                stats.requests += 1  # the conditional GET itself
                stats.not_modified += 1
                stats.ok += 1
                st.last_ok_at = now
                state["sources"][key] = asdict(st)
                return
            if raw.error and not raw.jobs:
                st.failures += 1
                stats.failed += 1
                state["sources"][key] = asdict(st)
                return
            seen = set(st.seen)
            new_jobs = [j for j in raw.jobs if j.source_job_id not in seen]
            if new_jobs and detail_policy != "none" and hasattr(adapter, "fetch_details"):
                need = [j for j in new_jobs if j.detail_needed][:40]
                if need:
                    pages = await adapter.fetch_details(spec, need)
                    stats.requests += len(pages)
            for j in new_jobs:
                company = resolver.resolve(j) if spec.provider == "github" else None
                values = build_posting_values(j, spec, company=company)
                tier, reason = prescore(values)
                row = _delta_row(values, spec, tier, reason, now)
                if stats.seed:
                    row["seed"] = True
                deltas.append(row)
                stats.new += 1
                if tier == "p0":
                    stats.p0 += 1
                elif tier == "p1":
                    stats.p1 += 1
            # feeds are newest-first: current ids first, then older remembered ids, newest kept
            current = [j.source_job_id for j in raw.jobs]
            merged = list(dict.fromkeys([*current, *st.seen]))
            st.seen = merged[:MAX_SEEN_PER_SOURCE]
            st.etag, st.last_modified = raw.etag or st.etag, raw.last_modified or st.last_modified
            st.last_ok_at = now
            st.failures = 0
            if mode == "full":
                st.last_full_at = now  # Actions never delists, so a capped full scan still counts
            state["sources"][key] = asdict(st)
            stats.ok += 1

        await asyncio.gather(*(one(s) for s in specs))

    if deltas:
        day = now[:10]
        dpath = actions_dir / "deltas" / f"{day}.jsonl"
        dpath.parent.mkdir(parents=True, exist_ok=True)
        with dpath.open("a", encoding="utf-8") as f:
            for d in deltas:
                f.write(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n")
    state["updated_at"] = now
    save_state(state_path, state)
    stats.elapsed_s = round(time.monotonic() - t0, 1)
    (actions_dir / "runs.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with (actions_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(stats), separators=(",", ":")) + "\n")
    return stats


def invocations_this_month(actions_dir: Path = ACTIONS_DIR) -> int:
    """Raw count of recorded runs this month. GitHub bills each job rounded UP to a whole minute, so
    this is a floor on minutes, not an estimate of them — there is deliberately no self-computed
    "billed minutes" figure (D52)."""
    p = actions_dir / "runs.jsonl"
    if not p.exists():
        return 0
    month = datetime.now(UTC).strftime("%Y-%m")
    n = 0
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("started_at", "").startswith(month):
            n += 1
    return n


def billing_from_github(token: str, user: str) -> dict[str, Any]:
    """Real usage from GitHub's billing API (needs a PAT with the `user` / `read:user` scope; the
    workflow's GITHUB_TOKEN cannot read billing). Raises on any failure — never guesses."""
    import httpx

    r = httpx.get(
        f"https://api.github.com/users/{user}/settings/billing/actions",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    return {
        "total_minutes_used": d.get("total_minutes_used"),
        "total_paid_minutes_used": d.get("total_paid_minutes_used"),
        "included_minutes": d.get("included_minutes"),
        "minutes_used_breakdown": d.get("minutes_used_breakdown"),
    }


def notify_from_deltas(cfg: Config, *, actions_dir: Path = ACTIONS_DIR) -> dict[str, Any]:
    """Cloud-side P0/P1: send Telegram alerts for fresh delta rows (never seeds), honoring a small
    per-run cap and quiet hours for P1, and drain button taps to actions/actions.jsonl."""
    from radar.notify.channels import Payload, Telegram
    from radar.notify.engine import in_quiet_hours
    from radar.notify.telegram_actions import drain_to_file

    tg = Telegram()
    state_path = actions_dir / "notify_state.json"
    state: dict[str, Any] = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"notified": [], "sent_today": {}}
    )
    stats = {
        "sent": 0,
        "skipped_seed": 0,
        "skipped_dup": 0,
        "quiet_held": 0,
        "drained": 0,
        "channel": "telegram" if tg.available() else "none",
    }
    if tg.available():
        try:
            from radar.notify.telegram_webhook import webhook_info

            webhook_active = bool(webhook_info(tg).get("url"))
        except Exception:
            webhook_active = False
        if webhook_active:
            stats["drained"] = 0  # the Worker queue holds taps; getUpdates would 409
        else:
            stats["drained"] = drain_to_file(actions_dir, state, tg)
    day = utcnow_iso()[:10]
    files = sorted((actions_dir / "deltas").glob("*.jsonl"))[-2:]
    notified = set(state.get("notified", []))
    sent_today = state.get("sent_today", {})
    if sent_today.get("day") != day:
        sent_today = {"day": day, "p0": 0, "p1": 0}
    quiet = in_quiet_hours(cfg)
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("alert") not in ("p0", "p1"):
                continue
            key = f"{d['provider']}:{d['slug']}:{d['job_id']}"
            if d.get("seed"):
                stats["skipped_seed"] += 1
                notified.add(key)
                continue
            if key in notified:
                stats["skipped_dup"] += 1
                continue
            tier = d["alert"]
            if tier == "p1" and quiet:
                stats["quiet_held"] += 1
                continue
            cap = {"p0": 3, "p1": 12}[tier]
            if sent_today.get(tier, 0) >= cap:
                continue
            notified.add(key)
            if not tg.available():
                continue
            base = (
                f"${d['base_min'] / 1000:.0f}–{d['base_max'] / 1000:.0f}k posted"
                if d.get("base_min") and d.get("base_max")
                else "comp not posted"
            )
            # inline buttons: the cloud has no posting ids, so callback data carries a short hash
            # of the natural key; the laptop resolves it via this file (committed) on apply
            h = hashlib.sha1(key.encode()).hexdigest()[:12]
            state.setdefault("keys", {})[h] = f"{d['provider']}|{d['slug']}|{d['job_id']}"
            p = Payload(
                tier=tier,
                title=f"[{tier.upper()}] {d['company']} — {d['title']}",
                body_lines=[
                    f"{', '.join(d.get('locations') or []) or d.get('primary_metro') or 'location ?'} · {base}",
                    f"why: {d.get('alert_reason')}",
                    "scored on the laptop at the next sync; open the link to decide now",
                ],
                url=d["apply_url"],
                buttons=[
                    ("✓ Applied", f"actk:applied:{h}"),
                    ("☆ Shortlist", f"actk:shortlist:{h}"),
                    ("✕ Dismiss", f"actk:dismiss:{h}"),
                    ("zz 7d", f"actk:snooze:{h}"),
                ],
            )
            if tg.send(p):
                stats["sent"] += 1
                sent_today[tier] = sent_today.get(tier, 0) + 1
            else:
                stats["failed"] = (
                    stats.get("failed", 0) + 1
                )  # a configured channel that did not deliver
    state["notified"] = sorted(notified)[-20000:]
    if len(state.get("keys", {})) > 5000:  # keep the hash map bounded (newest wins)
        state["keys"] = dict(list(state["keys"].items())[-5000:])
    state["sent_today"] = sent_today
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))
    return stats
