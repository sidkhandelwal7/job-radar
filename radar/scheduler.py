"""`radar cycle`: the one command launchd / GitHub Actions / a human runs on a schedule.

Each cycle, in order, bounded by time:
  1. fetch every source that is due (cadence-aware; Tier-1 + aggregators every 15 min)
  2. link-rot sweep if the last one is older than links.sweep_every_hours (bounded batch)
  3. slug discovery if the queue has pending employers and the last run is > 24 h old
  4. score anything new/changed → LLM budget → notifications → system alarms
  (backups / snapshots / calibration run from `radar nightly`, 03:30, also catch-up on wake)

Catch-up on wake: a cycle that starts long after the previous one simply finds more sources due —
there is nothing to "replay", and summaries (Phase 5) consolidate what happened while closed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from radar import db
from radar.config import Config
from radar.util import parse_dt, utcnow, utcnow_iso

log = logging.getLogger("radar.scheduler")

DISCOVERY_EVERY_HOURS = 24
VERIFY_BATCH = 120  # every cycle (not every 6 h): 120 × ~96 cycles/day ≈ 11k checks/day, enough to
# keep the whole ranked queue (≈15k rows) inside the 48 h freshness window, top bands always first


@dataclass
class CycleReport:
    started_at: str
    fetched_sources: int = 0
    new: int = 0
    changed: int = 0
    delisted: int = 0
    failed_sources: int = 0
    drift_alarms: int = 0
    verified: int = 0
    link_changes: int = 0
    discovered: int = 0
    clustering: dict[str, Any] | None = None
    scored: Any = None
    llm: dict[str, Any] | None = None
    notified: dict[str, Any] | None = None
    gap_hours: float | None = None  # time since the previous cycle (catch-up indicator)
    alarms: list[str] = field(default_factory=list)
    skipped: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)  # (subsystem, error)
    status: str = "ok"  # ok | partial | failed — failed = a critical subsystem did not run

    def fail(self, subsystem: str, error: str, *, critical: bool) -> None:
        """Record a subsystem failure honestly: it is never a 'note', and it changes the cycle's
        status (partial for best-effort work, failed for fetch/score/notify)."""
        self.failures.append((subsystem, error[:500]))
        if critical:
            self.status = "failed"
        elif self.status == "ok":
            self.status = "partial"

    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


def _hours_since(conn: sqlite3.Connection, key: str) -> float | None:
    v = db.kv_get(conn, key)
    dt = parse_dt(v) if v else None
    return None if dt is None else (utcnow() - dt).total_seconds() / 3600


def _pull_actions_deltas(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """`git pull --ff-only` (bounded, only when a remote exists and the tree is clean) then ingest deltas."""
    import subprocess

    actions_dir = cfg.root / "actions"
    if not (cfg.root / ".git").exists():
        return None
    remotes = subprocess.run(
        ["git", "remote"], cwd=cfg.root, capture_output=True, text=True, timeout=10
    ).stdout.split()
    if remotes:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cfg.root,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
        if not dirty:
            subprocess.run(
                ["git", "pull", "--ff-only", "--quiet"],
                cwd=cfg.root,
                capture_output=True,
                text=True,
                timeout=60,
            )
    if not (actions_dir / "deltas").exists():
        return None
    from radar.actions_ingest import ingest

    res = ingest(conn, cfg, actions_dir=actions_dir, days=3)
    if res.get("backdated") or res.get("inserted"):
        return f"actions deltas: {res['backdated']} backdated, {res['inserted']} provisional rows"
    return None


async def run_cycle(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    host: str = "laptop",
    providers: set[str] | None = None,
    tier1_only: bool = False,
    skip_verify: bool = False,
    skip_discover: bool = False,
    skip_enrich: bool = False,
    progress: Any = None,
) -> CycleReport:
    from radar.fetch.pipeline import fetch_all
    from radar.fetch.registry import source_specs, sync_registry

    t0 = time.monotonic()
    rep = CycleReport(started_at=utcnow_iso())
    rep.gap_hours = _hours_since(conn, "last_cycle_at")
    if rep.gap_hours is not None and rep.gap_hours > 1.5:
        rep.notes.append(f"catch-up: {rep.gap_hours:.1f} h since the last cycle")
    sync_registry(conn, cfg)

    run_id = db.start_run(conn, "cycle", host=host)

    # GitHub Actions deltas (§4): pull the private remote if there is one, fold sightings in
    try:
        pulled = _pull_actions_deltas(conn, cfg)
        if pulled:
            rep.notes.append(pulled)
    except Exception as e:
        rep.fail("actions_pull", str(e), critical=False)
        log.exception("actions pull failed")

    # --- fetch (critical): an exception here, or any failed source, is not a green cycle
    try:
        specs = source_specs(conn, providers=providers, due_only=True)
        if tier1_only:
            specs = [s for s in specs if s.cadence == "15min"]
        if specs:
            summary = await fetch_all(conn, cfg, specs, host=host, progress=progress)
            st = summary.stats
            rep.fetched_sources = st["sources"]
            rep.new, rep.changed, rep.delisted = st["new"], st["changed"], st["delisted"]
            rep.failed_sources, rep.drift_alarms = st["sources_failed"], st["drift_alarms"]
            if st.get("sources_deferred"):
                rep.notes.append(
                    f"{st['sources_deferred']} sources deferred to the next cycle (fetch budget {cfg.fetch.cycle_budget_seconds:.0f}s)"
                )
            rep.clustering = st.get("clustering")
            if rep.failed_sources:
                failed = [o for o in summary.outcomes if not o.ok and o.mode != "deferred"]
                rep.fail(
                    "fetch",
                    f"{rep.failed_sources} source(s) failed: "
                    + "; ".join(f"{o.spec.key}: {o.error}" for o in failed[:5]),
                    critical=rep.failed_sources
                    == rep.fetched_sources,  # all failed = failed; some = partial
                )
    except Exception as e:
        rep.fail("fetch", f"{type(e).__name__}: {e}", critical=True)
        log.exception("fetch failed")

    # --- link sweep (best effort; the timestamp only advances on success)
    if not skip_verify:
        try:
            from radar.links import sweep

            vs = await sweep(conn, cfg, limit=VERIFY_BATCH)
            rep.verified, rep.link_changes = vs.checked, vs.changed
            db.kv_set(conn, "last_link_sweep_at", utcnow_iso())
        except Exception as e:
            rep.fail("link_sweep", f"{type(e).__name__}: {e}", critical=False)
            log.exception("link sweep failed")

    # --- slug discovery (best effort)
    if not skip_discover:
        try:
            pending = (
                db.scalar(conn, "SELECT COUNT(*) FROM discovery_queue WHERE status = 'pending'")
                or 0
            )
            h = _hours_since(conn, "last_discovery_at")
            if pending and (h is None or h >= DISCOVERY_EVERY_HOURS):
                from radar.discover import run_discovery

                ds = await run_discovery(conn, cfg, limit=200)
                rep.discovered = ds.resolved
                db.kv_set(conn, "last_discovery_at", utcnow_iso())
                if ds.resolved:
                    from radar.fetch.registry import load_registry

                    load_registry.cache_clear()
                    sync_registry(conn, cfg)
        except Exception as e:
            rep.fail("discovery", f"{type(e).__name__}: {e}", critical=False)
            log.exception("discovery failed")

    # --- descriptions (best effort) and scoring (critical)
    try:
        from radar.fetch.html_detail import fetch_missing

        hd = await fetch_missing(conn, cfg, limit=40)
        if hd.get("fetched"):
            rep.notes.append(f"fetched {hd['fetched']} descriptions from employer pages")
    except Exception as e:
        rep.fail("descriptions", f"{type(e).__name__}: {e}", critical=False)
        log.exception("description fetch failed")
    try:
        from radar.score.engine import score_all

        sc = score_all(conn, cfg, run_id=None, only_unscored=True)
        rep.scored = sc.get("scored")
    except Exception as e:
        rep.fail("score", f"{type(e).__name__}: {e}", critical=True)
        log.exception("scoring failed")

    # --- LLM enrichment (best effort, budgeted)
    if cfg.llm.enabled and not skip_enrich and cfg.llm.calls_per_cycle > 0:
        from radar.enrich.pipeline import enrich

        enrich_run = db.start_run(conn, "enrich", host=host)
        try:
            er = enrich(conn, cfg, max_calls=cfg.llm.calls_per_cycle, run_id=enrich_run)
            rep.llm = er.get("llm")
            db.finish_run(conn, enrich_run, stats=er)
        except Exception as e:
            db.finish_run(conn, enrich_run, status="failed", error=str(e))
            rep.fail("enrich", f"{type(e).__name__}: {e}", critical=False)
            log.exception("enrichment failed")
        else:
            # Enrichment can discover a posted range (posted_range_llm) or new requirements on rows
            # that were just scored off a lower-confidence prior. Rescore what it flagged NOW, before
            # notify — otherwise the alert and queue rank this cycle still carry the prior-based
            # verdict (the hourly-range case, D65: an LCA prior set the ranking a posted range would have changed).
            try:
                from radar.score.engine import score_all

                sc2 = score_all(conn, cfg, run_id=None, only_unscored=True)
                if sc2.get("scored"):
                    rep.scored = (rep.scored or 0) + sc2["scored"]
            except Exception as e:
                rep.fail("score", f"{type(e).__name__}: {e}", critical=True)
                log.exception("post-enrichment rescore failed")

    # --- notifications (critical: a cycle that fetched but could not alert is not a success)
    try:
        from radar.notify.digest import send_digest, wake_summary
        from radar.notify.engine import send_alerts
        from radar.notify.telegram_actions import drain_and_apply, listener_owns_queue
        from radar.notify.telegram_webhook import drain_webhook, reconcile

        # ONE consumer of Telegram updates at a time (D60): webhook when the Worker is healthy
        # (taps queue there while the laptop sleeps; drained here), else long-polling — by the
        # listener agent if it is alive, else a one-shot drain from this cycle.
        tmode = reconcile(conn)
        if tmode.get("mode") == "webhook":
            wd = drain_webhook(conn)
            if wd.get("applied"):
                rep.notes.append(f"applied {wd['applied']} Telegram tap(s) from the webhook queue")
            if wd.get("error"):
                rep.fail("telegram_webhook", wd["error"], critical=False)
        elif tmode.get("mode") == "polling" and not listener_owns_queue(cfg):
            drain_and_apply(conn)
        if rep.gap_hours is not None and rep.gap_hours >= 6 and rep.new:
            since = db.kv_get(conn, "last_cycle_at")
            p = wake_summary(conn, cfg, since=since) if since else None
            if p:
                from radar.notify.channels import all_channels

                delivered = False
                for ch in [
                    c
                    for c in all_channels(str(cfg.data_dir / "notifications.log"))
                    if c.available() and c.name in ("telegram", "file")
                ]:
                    delivered = bool(ch.send(p)) or delivered
                if delivered:
                    rep.notes.append("sent one consolidated catch-up summary instead of a burst")
                    db.kv_set(conn, "last_notify_at", utcnow_iso())  # the burst is folded in
        ns = send_alerts(conn, cfg, run_id=None)
        rep.notified = {"sent": ns.sent, "by_tier": ns.by_tier, "suppressed": ns.suppressed}
        if ns.failed:
            rep.fail("notify", f"{ns.failed} alert(s) could not be delivered", critical=True)
        now_et = datetime.now(UTC).astimezone(ZoneInfo("America/New_York"))
        digest_hour = int(cfg.notify.digest_time_et.split(":")[0])
        if (
            now_et.hour >= digest_hour
            and (db.kv_get(conn, "last_daily_digest_at") or "")[:10] != utcnow_iso()[:10]
        ):
            send_digest(conn, cfg, "daily")
            rep.notes.append("daily digest sent")
        if (
            now_et.strftime("%A").lower() == cfg.notify.weekly_digest_day
            and now_et.hour >= cfg.notify.weekly_digest_hour_et
            and (db.kv_get(conn, "last_weekly_digest_at") or "")
            < (utcnow() - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ):
            send_digest(conn, cfg, "weekly")
            rep.notes.append("weekly digest sent")
    except Exception as e:
        rep.fail("notify", f"{type(e).__name__}: {e}", critical=True)
        log.exception("notify step failed")

    # --- system alarms (§16 "no silent failures"): drift, failing Tier-1, stale backup, budgets
    try:
        from radar.ops import alarms

        al = alarms.evaluate(conn, cfg)
        if al:
            pushed = alarms.push(conn, cfg, al)
            rep.alarms = [f"{a.severity}: {a.title}" for a in al]
            if pushed:
                rep.notes.append(f"{pushed} system alarm(s) pushed")
    except Exception as e:
        rep.fail("alarms", f"{type(e).__name__}: {e}", critical=False)
        log.exception("alarms failed")

    # a failed cycle does not advance the cycle watermark: the gap stays visible to the next
    # cycle's catch-up logic and to the stale-cycle alarm
    if rep.status != "failed":
        db.kv_set(conn, "last_cycle_at", utcnow_iso())
    rep.elapsed_s = round(time.monotonic() - t0, 1)
    db.finish_run(
        conn,
        run_id,
        status=rep.status,
        stats={k: v for k, v in rep.__dict__.items() if k not in ("clustering",)},
        error="; ".join(f"{n}: {e}" for n, e in rep.failures) or None,
    )
    return rep


def run_cycle_sync(conn: sqlite3.Connection, cfg: Config, **kw: Any) -> CycleReport:
    """Run one cycle unless another cycle holds the lock (launchd fires a missed interval on wake
    while a long manual cycle may still be running); a skipped cycle is reported, not silent."""
    from radar.ops.launchd import single_instance

    with single_instance(cfg, "cycle") as mine:
        if not mine:
            rep = CycleReport(started_at=utcnow_iso())
            rep.skipped = True
            running = db.one(
                conn,
                "SELECT id, started_at FROM runs WHERE kind = 'cycle' AND status = 'running' ORDER BY id DESC LIMIT 1",
            )
            started = parse_dt(running["started_at"]) if running else None
            hours = (utcnow() - started).total_seconds() / 3600 if started else None
            rep.notes.append(
                f"skipped: another cycle is already running (lock held{f', run #{running["id"]} for {hours:.1f} h' if hours is not None else ''})"
            )
            if hours is not None and hours >= 2:
                # a cycle should take minutes; one that holds the lock for hours is stuck
                # (2026-08-21: a quadratic dedupe pass ran 8 h and every later cycle was skipped)
                rep.fail(
                    "cycle_watchdog",
                    f"run #{running['id']} has held the cycle lock for {hours:.1f} h — kill it and check its log",
                    critical=False,
                )
                try:
                    from radar.ops import alarms

                    alarms.push(
                        conn,
                        cfg,
                        [
                            alarms.Alarm(
                                "stuck_cycle",
                                f"a cycle has been running for {hours:.1f} h",
                                f"run #{running['id']} started {running['started_at']}; later cycles are being skipped. `pkill -f 'radar cycle'` and read data/logs/cycle.err.log",
                                "error",
                            )
                        ],
                    )
                except Exception:
                    log.exception("stuck-cycle alarm failed")
            return rep
        return asyncio.run(run_cycle(conn, cfg, **kw))
