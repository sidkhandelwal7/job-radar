"""Fetch pipeline: registry → adapters → raw store → normalize → upsert postings → delist.

Invariants:
  * raw payloads are stored before anything is parsed (append-only, §3.1)
  * postings are never deleted; delisting sets delisted_at
  * workflow columns are never written here
  * a drifted source (row count collapsed) never triggers delists
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from radar import db
from radar.config import Config
from radar.fetch.adapters import get_adapter
from radar.fetch.adapters.base import BaseAdapter, FetchedPage, FetchRaw
from radar.fetch.http import CircuitOpen, PoliteClient, RetryLater
from radar.fetch.raw_store import RawStore
from radar.models import RawJob, SourceSpec
from radar.parse.posting import POSTING_DERIVED_COLUMNS, build_posting_values
from radar.parse.titles import ENGINEERING_FAMILIES, normalize_title
from radar.util import parse_dt, utcnow, utcnow_iso

log = logging.getLogger("radar.pipeline")

FULL_SCAN_INTERVAL_HOURS = 6
DRIFT_MIN_TYPICAL = 20
DRIFT_COLLAPSE_RATIO = 0.2
SKIP_DETAIL_SENIORITY = {"senior", "staff", "principal", "manager", "executive", "internship"}


@dataclass
class SourceOutcome:
    spec: SourceSpec
    ok: bool
    mode: str = "full"
    rows: int = 0
    new: int = 0
    changed: int = 0
    delisted: int = 0
    relisted: int = 0
    details_fetched: int = 0
    not_modified: bool = False
    drift: bool = False
    error: str | None = None
    requests: int = 0
    bytes: int = 0
    elapsed_ms: int = 0
    unknown_companies: int = 0
    new_posting_ids: list[int] = field(default_factory=list)


@dataclass
class FetchSummary:
    run_id: int
    outcomes: list[SourceOutcome] = field(default_factory=list)
    cluster_stats: Any = None

    @property
    def stats(self) -> dict[str, Any]:
        ok = [o for o in self.outcomes if o.ok]
        deferred = [o for o in self.outcomes if o.mode == "deferred"]
        return {
            "sources": len(self.outcomes),
            "sources_ok": len(ok),
            "sources_failed": len(self.outcomes) - len(ok) - len(deferred),
            "sources_deferred": len(deferred),  # cycle budget ran out; still due next cycle
            "rows_seen": sum(o.rows for o in ok),
            "new": sum(o.new for o in ok),
            "changed": sum(o.changed for o in ok),
            "delisted": sum(o.delisted for o in ok),
            "relisted": sum(o.relisted for o in ok),
            "details_fetched": sum(o.details_fetched for o in ok),
            "not_modified": sum(1 for o in ok if o.not_modified),
            "drift_alarms": sum(1 for o in self.outcomes if o.drift),
            "requests": sum(o.requests for o in self.outcomes),
            "bytes": sum(o.bytes for o in self.outcomes),
        }


class CompanyResolver:
    """Resolve aggregator rows' free-text company names to registry companies; queue the rest."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        from radar.fetch.registry import CompanyMatcher

        self.conn = conn
        self.matcher = CompanyMatcher(conn)
        self._rows: dict[int, dict[str, Any]] = {}
        for r in db.all_rows(
            conn,
            "SELECT id, name, tier, is_dream_list, target_category, is_quant_trading_firm FROM companies",
        ):
            self._rows[r["id"]] = {
                "id": r["id"],
                "name": r["name"],
                "tier": r["tier"],
                "is_dream_list": r["is_dream_list"],
                "target_category": r["target_category"],
                "is_quant_trading_firm": r["is_quant_trading_firm"],
            }
        self.unknown: dict[str, dict[str, Any]] = {}

    def resolve(self, job: RawJob) -> dict[str, Any]:
        name = (job.company_name or "").strip()
        hit = self.matcher.match(name)
        if hit:
            info = dict(self._rows[hit[0]])
            info["name"] = (
                name or info["name"]
            )  # keep the row's own spelling? no — registry name is canonical
            info["name"] = self._rows[hit[0]]["name"]
            return info
        if name:
            from radar.fetch.registry import normalize_company_name

            key = normalize_company_name(name)
            u = self.unknown.setdefault(
                key,
                {
                    "name": name,
                    "url": job.apply_url,
                    "company_url": job.raw.get("company_url"),
                    "n": 0,
                },
            )
            u["n"] += 1
        return {
            "id": None,
            "name": name or None,
            "tier": None,
            "is_dream_list": 0,
            "target_category": None,
            "is_quant_trading_firm": 0,
        }

    def flush_unknown(self) -> int:
        """Upsert unknown employers into discovery_queue."""
        now = utcnow_iso()
        n = 0
        with db.transaction(self.conn):
            for key, u in self.unknown.items():
                self.conn.execute(
                    "INSERT INTO discovery_queue (company_name_norm, company_name, example_url, company_url, seen_count, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(company_name_norm) DO UPDATE SET "
                    "seen_count = seen_count + excluded.seen_count, last_seen_at = excluded.last_seen_at, "
                    "example_url = COALESCE(discovery_queue.example_url, excluded.example_url), company_url = COALESCE(discovery_queue.company_url, excluded.company_url)",
                    (key, u["name"], u["url"], u["company_url"], u["n"], now, now),
                )
                n += 1
        return n


def _should_fetch_detail(job: RawJob, policy: str) -> bool:
    if not job.detail_needed:
        return False
    if policy == "none":
        return False
    if policy == "all_new":
        return True
    ti = normalize_title(job.title)
    if ti.seniority in SKIP_DETAIL_SENIORITY:
        return False
    return ti.role_family in ENGINEERING_FAMILIES or ti.role_family == "unknown"


FULL_SCAN_HOURS_BY_CADENCE = {"15min": 2, "hourly": 6, "6h": 6, "daily": 24}


def choose_mode(
    conn: sqlite3.Connection, spec: SourceSpec, adapter: BaseAdapter, *, force_full: bool
) -> str:
    """Full scans (all pages; the only mode that may delist) every 2 h for 15-min sources and every
    6 h otherwise; incremental (newest pages only) in between. Single-request providers are always full."""
    if force_full or adapter.incremental_items >= 10_000:
        return "full"
    last_full = db.kv_get(conn, f"last_full_scan:{spec.key}")
    if not last_full:
        return "full"
    dt = parse_dt(last_full)
    hours = FULL_SCAN_HOURS_BY_CADENCE.get(spec.cadence, FULL_SCAN_INTERVAL_HOURS)
    if dt is None or (utcnow() - dt).total_seconds() > hours * 3600:
        return "full"
    return "incremental"


async def fetch_all(
    conn: sqlite3.Connection,
    cfg: Config,
    specs: list[SourceSpec],
    *,
    host: str = "laptop",
    force_full: bool = False,
    detail_policy: str | None = None,
    progress: Any = None,
    cluster: bool = True,
    budget_seconds: float | None = None,
) -> FetchSummary:
    run_id = db.start_run(conn, "fetch", host=host)
    summary = FetchSummary(run_id=run_id)
    store = RawStore(cfg.raw_dir)
    policy = detail_policy or cfg.fetch.detail_fetch
    async with PoliteClient(
        cfg.fetch.user_agent,
        concurrency=cfg.fetch.concurrency,
        per_host_concurrency=cfg.fetch.per_host_concurrency,
        timeout=cfg.fetch.timeout_seconds,
    ) as client:
        sem = asyncio.Semaphore(cfg.fetch.concurrency)
        t_start = time.monotonic()
        # None → the configured cycle budget; a negative number → unlimited (manual `radar fetch`)
        budget = cfg.fetch.cycle_budget_seconds if budget_seconds is None else budget_seconds

        async def run_one(spec: SourceSpec) -> SourceOutcome:
            async with sem:
                if budget >= 0 and time.monotonic() - t_start > budget:
                    # out of time for this cycle: leave the source due (no bookkeeping written)
                    return SourceOutcome(
                        spec=spec,
                        ok=False,
                        error="deferred: cycle budget exhausted",
                        mode="deferred",
                    )
                try:
                    outcome = await asyncio.wait_for(
                        _fetch_source(conn, cfg, client, store, spec, run_id, policy, force_full),
                        timeout=cfg.fetch.source_timeout_seconds,
                    )
                except TimeoutError:
                    outcome = SourceOutcome(
                        spec=spec,
                        ok=False,
                        error=f"timed out after {cfg.fetch.source_timeout_seconds:.0f}s",
                    )
                    _record_failure(conn, spec.extra.get("source_id"), outcome.error)
                if progress:
                    progress(outcome)
                return outcome

        results = await asyncio.gather(*(run_one(s) for s in specs), return_exceptions=True)
    for spec, res in zip(specs, results, strict=True):
        if isinstance(res, BaseException):
            log.exception("source %s crashed", spec.key, exc_info=res)
            summary.outcomes.append(
                SourceOutcome(spec=spec, ok=False, error=f"{type(res).__name__}: {res}")
            )
        else:
            summary.outcomes.append(res)
    status = (
        "ok"
        if all(o.ok for o in summary.outcomes)
        else ("partial" if any(o.ok for o in summary.outcomes) else "failed")
    )
    stats = summary.stats
    if cluster and any(o.new or o.changed or o.delisted for o in summary.outcomes):
        from radar.dedupe.cluster import run_clustering

        cs = run_clustering(conn, run_id=run_id)
        stats["clustering"] = {k: v for k, v in cs.__dict__.items() if k != "examples"}
        summary.cluster_stats = cs
    db.finish_run(conn, run_id, status=status, stats=stats)
    return summary


async def _fetch_source(
    conn: sqlite3.Connection,
    cfg: Config,
    client: PoliteClient,
    store: RawStore,
    spec: SourceSpec,
    run_id: int,
    policy: str,
    force_full: bool,
) -> SourceOutcome:
    t0 = time.monotonic()
    outcome = SourceOutcome(spec=spec, ok=False)
    source_id = spec.extra.get("source_id")
    company_id = spec.extra.get("company_id")
    try:
        adapter = get_adapter(spec.provider, client)
    except KeyError as e:
        outcome.error = str(e)
        _record_failure(conn, source_id, outcome.error)
        return outcome
    if adapter.min_interval:
        from urllib.parse import urlparse

        host = urlparse(_probe_url(spec)).netloc
        if host:
            client.set_host_min_interval(host, adapter.min_interval)
    mode = choose_mode(conn, spec, adapter, force_full=force_full)
    outcome.mode = mode
    try:
        raw = await adapter.fetch(
            spec,
            mode=mode,
            etag=spec.extra.get("etag"),
            last_modified=spec.extra.get("last_modified"),
        )
    except (CircuitOpen, RetryLater) as e:
        outcome.error = str(e)
        _record_failure(conn, source_id, outcome.error)
        return outcome
    except Exception as e:
        outcome.error = f"{type(e).__name__}: {e}"
        log.warning("fetch failed for %s: %s", spec.key, outcome.error)
        _record_failure(conn, source_id, outcome.error)
        return outcome

    outcome.requests += raw.requests_made
    outcome.bytes += raw.bytes_downloaded
    now = utcnow_iso()

    if raw.not_modified:
        with db.transaction(conn):
            db.update(
                conn,
                "company_sources",
                source_id,
                {"last_fetched_at": now, "last_success_at": now, "consecutive_failures": 0},
            )
            conn.execute(
                "UPDATE postings SET last_seen_at = ? WHERE source_provider = ? AND source_slug = ? AND delisted_at IS NULL",
                (now, spec.provider, spec.slug),
            )
        outcome.ok = True
        outcome.not_modified = True
        outcome.requests = 1
        outcome.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return outcome

    # 1. store raw list payload (always, even on error — the evidence matters)
    payload_id, _unchanged = store.store(
        conn,
        provider=spec.provider,
        slug=spec.slug,
        url=_probe_url(spec),
        content=raw.combined_payload(),
        http_status=raw.http_status,
        run_id=run_id,
        source_id=source_id,
        kind="list",
        row_count=len(raw.jobs),
    )
    if raw.error and not raw.jobs:
        outcome.error = raw.error
        _record_failure(conn, source_id, raw.error)
        outcome.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return outcome

    # 2. drift guard
    src = db.one(
        conn,
        "SELECT typical_row_count, last_row_count FROM company_sources WHERE id = ?",
        (source_id,),
    )
    typical = (src["typical_row_count"] if src else None) or 0
    previously_empty = src_empty(conn, source_id) > 0
    collapsed = typical >= DRIFT_MIN_TYPICAL and len(raw.jobs) < typical * DRIFT_COLLAPSE_RATIO
    # A complete scan with ZERO rows is unexpected unless this source was already known to be
    # empty: a real board rarely goes from N to 0, and a brand-new source answering 0 is far more
    # often a wrong slug / changed API than an employer with no openings. Alarm (and suppress
    # delists) even with no historical baseline; once it is a known-empty source it stops alarming.
    unexpected_zero = raw.complete and not raw.jobs and (typical > 0 or not previously_empty)
    if raw.complete and (collapsed or unexpected_zero):
        outcome.drift = True
        with db.transaction(conn):
            conn.execute(
                "UPDATE company_sources SET last_drift_at = ?, drift_note = ? WHERE id = ?",
                (
                    utcnow_iso(),
                    f"{len(raw.jobs)} rows vs typical {typical:.0f}"
                    if typical
                    else "0 rows on a complete scan with no baseline (wrong slug or changed API?)",
                    source_id,
                ),
            )
        log.warning(
            "DRIFT %s: %d rows vs typical %.0f — delist detection suppressed",
            spec.key,
            len(raw.jobs),
            typical,
        )

    # 3. which jobs are new / changed? (decides detail fetching)
    existing = {
        r["source_job_id"]: r
        for r in db.all_rows(
            conn,
            "SELECT id, source_job_id, content_hash, raw_hash, delisted_at, description_fetched FROM postings WHERE source_provider = ? AND source_slug = ?",
            (spec.provider, spec.slug),
        )
    }
    need_detail = [
        j
        for j in raw.jobs
        if _should_fetch_detail(j, policy)
        and (
            j.source_job_id not in existing or not existing[j.source_job_id]["description_fetched"]
        )
    ]
    detail_pages: list[FetchedPage] = []
    if need_detail:
        try:
            detail_pages = await adapter.fetch_details(spec, need_detail)
        except Exception as e:
            log.warning("detail fetch failed for %s: %s", spec.key, e)
        outcome.details_fetched = len(detail_pages)
        outcome.requests += len(detail_pages)
        outcome.bytes += sum(len(p.body) for p in detail_pages)
        if detail_pages:
            doc = {
                "provider": spec.provider,
                "slug": spec.slug,
                "details": [
                    {
                        "url": p.url,
                        "status": p.status,
                        "body": p.body.decode("utf-8", errors="replace"),
                    }
                    for p in detail_pages
                ],
            }
            store.store(
                conn,
                provider=spec.provider,
                slug=spec.slug,
                url=_probe_url(spec) + "#details",
                content=json.dumps(doc, ensure_ascii=False).encode("utf-8"),
                http_status=200,
                run_id=run_id,
                source_id=source_id,
                kind="detail",
                row_count=len(detail_pages),
            )

    # 4. upsert (aggregator rows resolve their own company names)
    resolver = CompanyResolver(conn) if spec.provider == "github" else None
    res = upsert_jobs(
        conn,
        spec,
        raw,
        raw.jobs,
        existing,
        company_id=company_id,
        payload_id=payload_id,
        run_id=run_id,
        suppress_delist=outcome.drift or not raw.complete,
        company_resolver=resolver,
    )
    if resolver:
        outcome.unknown_companies = resolver.flush_unknown()
    outcome.rows = len(raw.jobs)
    outcome.new, outcome.changed, outcome.delisted, outcome.relisted = (
        res["new"],
        res["changed"],
        res["delisted"],
        res["relisted"],
    )
    outcome.new_posting_ids = res["new_ids"]

    # 5. source bookkeeping
    with db.transaction(conn):
        ewma = len(raw.jobs) if not typical else (0.7 * typical + 0.3 * len(raw.jobs))
        values: dict[str, Any] = {
            "last_fetched_at": now,
            "last_success_at": now,
            "last_row_count": len(raw.jobs),
            "consecutive_failures": 0,
            "consecutive_empty": 0 if raw.jobs else (src_empty(conn, source_id) + 1),
            "last_error": None,
        }
        if raw.complete and not outcome.drift:
            # a drifted scan must not erode the baseline it was judged against
            values["typical_row_count"] = ewma
            values["drift_note"] = None  # a healthy full scan clears the alarm (history stays)
        if (
            db.scalar(
                conn, "SELECT seed_completed_at FROM company_sources WHERE id = ?", (source_id,)
            )
            is None
        ):
            # first successful scan: everything it found is "new to us", not "new to the market"
            values["seed_completed_at"] = utcnow_iso()
        if raw.etag:
            values["etag"] = raw.etag
        if raw.last_modified:
            values["last_modified"] = raw.last_modified
        db.update(conn, "company_sources", source_id, values)
        if raw.complete and mode == "full":
            db.kv_set(conn, f"last_full_scan:{spec.key}", now)
    outcome.ok = True
    outcome.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return outcome


def src_empty(conn: sqlite3.Connection, source_id: int | None) -> int:
    return int(
        db.scalar(conn, "SELECT consecutive_empty FROM company_sources WHERE id = ?", (source_id,))
        or 0
    )


def _probe_url(spec: SourceSpec) -> str:
    if spec.provider == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{spec.slug}/jobs?content=true"
    if spec.provider == "workday":
        from radar.fetch.adapters.workday import base_url

        return base_url(spec.slug) + "/jobs"
    if spec.provider == "oracle":
        from radar.fetch.adapters.oracle import list_url

        return list_url(spec.slug, 200, 0)
    if spec.provider == "github":
        from radar.fetch.adapters.github_aggregators import aggregator_by_slug

        agg = aggregator_by_slug(spec.slug) or {}
        return agg.get("url") or spec.careers_url or f"github:{spec.slug}"
    if spec.provider == "lever":
        return f"https://api.lever.co/v0/postings/{spec.slug}?mode=json"
    if spec.provider == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{spec.slug}?includeCompensation=true"
    if spec.provider == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{spec.slug}?details=true"
    if spec.provider == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{spec.slug}/postings"
    if spec.provider == "recruitee":
        return f"https://{spec.slug}.recruitee.com/api/offers/"
    return spec.careers_url or f"{spec.provider}:{spec.slug}"


def _record_failure(conn: sqlite3.Connection, source_id: int | None, error: str) -> None:
    if source_id is None:
        return
    with db.transaction(conn):
        conn.execute(
            "UPDATE company_sources SET last_fetched_at = ?, consecutive_failures = consecutive_failures + 1, last_error = ? WHERE id = ?",
            (utcnow_iso(), error[:500], source_id),
        )


def upsert_jobs(
    conn: sqlite3.Connection,
    spec: SourceSpec,
    raw: FetchRaw | None,
    jobs: list[RawJob],
    existing: dict[str, sqlite3.Row],
    *,
    company_id: int | None,
    payload_id: int | None,
    run_id: int | None,
    suppress_delist: bool,
    company_name: str | None = None,
    force_derived: bool = False,
    company_resolver: CompanyResolver | None = None,
) -> dict[str, Any]:
    """Insert new postings, refresh existing ones, optionally delist the missing.

    force_derived=True (replay) rewrites every derived column even when the source content is
    unchanged — that's how rule/normalizer edits propagate via `radar rescore --replay`.
    """
    now = utcnow_iso()
    new = changed = relisted = delisted = 0
    new_ids: list[int] = []
    seen_ids: set[str] = set()
    with db.transaction(conn):
        for job in jobs:
            if job.source_job_id in seen_ids:
                continue
            seen_ids.add(job.source_job_id)
            prev = existing.get(job.source_job_id)
            rh = job.raw_hash()
            if (
                prev is not None
                and not force_derived
                and prev["raw_hash"] == rh
                and not prev["delisted_at"]
            ):
                # unchanged at the adapter level: presence refresh only (no re-parse)
                db.update(
                    conn,
                    "postings",
                    prev["id"],
                    {
                        "last_seen_at": now,
                        "url_status": "live",
                        "url_verify_method": "source_presence",
                        "url_last_verified_at": now,
                    },
                )
                continue
            company = company_resolver.resolve(job) if company_resolver else None
            values = build_posting_values(job, spec, company=company)
            values.pop("_primary_location", None)
            values["raw_hash"] = rh
            if prev is None:
                values.update(
                    {
                        "source_provider": spec.provider,
                        "source_slug": spec.slug,
                        "source_job_id": job.source_job_id,
                        "raw_payload_ref": payload_id,
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "url_status": "live",
                        "url_verify_method": "source_presence",
                        "url_last_verified_at": now,
                        "needs_rescore": 1,
                    }
                )
                pid = db.insert_posting(conn, values)
                db.add_event(
                    conn,
                    pid,
                    "first_seen",
                    {"provider": spec.provider, "title": values["title"]},
                    run_id,
                )
                new += 1
                new_ids.append(pid)
                continue
            pid = prev["id"]
            upd: dict[str, Any] = {
                "last_seen_at": now,
                "url_status": "live",
                "url_verify_method": "source_presence",
                "url_last_verified_at": now,
            }
            if prev["delisted_at"]:
                upd["delisted_at"] = None
                upd["needs_rescore"] = 1
                db.add_event(
                    conn, pid, "relisted", {"was_delisted_at": prev["delisted_at"]}, run_id
                )
                relisted += 1
            content_changed = prev["content_hash"] != values["content_hash"]
            if content_changed or force_derived:
                # Only overwrite the description if we actually have one this time (incremental
                # list scans without detail don't erase a previously fetched description).
                derived = {k: values[k] for k in POSTING_DERIVED_COLUMNS if k in values}
                if not values.get("description_md") and prev["description_fetched"]:
                    for k in (
                        "description_md",
                        "description_fetched",
                        "requires_clearance",
                        "requires_advanced_degree",
                        "min_years_experience",
                        "max_years_experience",
                        "sponsorship",
                        "graduation_window",
                        "content_hash",
                    ):
                        derived.pop(k, None)
                    # tech tags / comp may have come from the description; don't regress them
                    derived.pop("tech_tags_json", None)
                    if values.get("base_posted_min") is None:
                        for k in (
                            "base_posted_min",
                            "base_posted_max",
                            "base_posted_currency",
                            "base_posted_interval",
                            "comp_source",
                        ):
                            derived.pop(k, None)
                if "content_hash" in derived and content_changed:
                    upd["changed_since_first_seen"] = 1
                    upd["needs_rescore"] = 1
                    db.add_event(
                        conn, pid, "changed", {"fields": _diff_fields(conn, pid, derived)}, run_id
                    )
                    changed += 1
                doc_upd = {k: derived.pop(k) for k in list(derived) if k in db.DOC_COLUMNS}
                if "title" in derived or "company_name" in derived:
                    doc_upd.update(
                        title=derived.get("title", values["title"]),
                        company_name=derived.get("company_name", values["company_name"]),
                    )
                if doc_upd:
                    db.upsert_doc(conn, pid, **doc_upd)
                upd.update(derived)
                upd["raw_payload_ref"] = payload_id
                if force_derived:
                    upd["needs_rescore"] = 1
            upd["raw_hash"] = rh
            db.update(conn, "postings", pid, upd)
        if not suppress_delist:
            for job_id, prev in existing.items():
                if job_id in seen_ids or prev["delisted_at"]:
                    continue
                db.update(
                    conn,
                    "postings",
                    prev["id"],
                    {
                        "delisted_at": now,
                        "url_status": "unverified",
                        "url_verify_method": "source_presence",
                        "needs_rescore": 1,
                    },
                )
                db.add_event(
                    conn, prev["id"], "delisted", {"reason": "absent from source feed"}, run_id
                )
                delisted += 1
    return {
        "new": new,
        "changed": changed,
        "delisted": delisted,
        "relisted": relisted,
        "new_ids": new_ids,
    }


def _diff_fields(conn: sqlite3.Connection, pid: int, derived: dict[str, Any]) -> list[str]:
    row = db.one(conn, "SELECT * FROM postings WHERE id = ?", (pid,))
    if not row:
        return []
    out = []
    for k, v in derived.items():
        if k in ("content_hash", "parse_confidence") or k in db.DOC_COLUMNS:
            continue
        if row[k] != v and not (row[k] in (None, "") and v in (None, "")):
            out.append(k)
    return out
