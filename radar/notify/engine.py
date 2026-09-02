"""Notification engine (§20): four tiers, hard anti-noise gates, engagement tracking.

Triggers
  P0  dream-list company opened a new-grad engineering req · clearly_better with est. days-to-close < 5 ·
      a shortlisted role's deadline moved inside 72 h · a saved filter marked alert:p0 matches a new posting
  P1  clearly_better new postings · new reqs at watchlist companies (registry tag `watchlist`)
  P2  daily digest (08:00 ET) · P3 weekly digest (Sunday evening)

Anti-noise (all enforced here)
  per-company and per-tier daily caps · never twice on one cluster per tier · reposts don't re-fire
  without a material change · quiet hours with P0-only override · cooldown after any P0 · escalating
  suppression (three consecutive ignored P1s from a company → that company drops to P2, with a note)
  · seed rows never alert.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from radar import db
from radar.config import Config
from radar.notify.channels import Channel, Payload, all_channels
from radar.util import parse_dt, utcnow, utcnow_iso

log = logging.getLogger("radar.notify")
ET = ZoneInfo("America/New_York")

PER_COMPANY_DAILY_CAP = 3
PER_TIER_DAILY_CAP = {"p0": 3, "p1": 12}
P0_COOLDOWN_MINUTES = 30
IGNORED_AFTER_HOURS = 48
ESCALATION_IGNORED_P1S = 3


@dataclass
class Candidate:
    posting: dict[str, Any]
    tier: str
    trigger: str
    reason: str


@dataclass
class NotifyStats:
    candidates: int = 0
    sent: int = 0
    suppressed: dict[str, int] = field(default_factory=dict)
    by_tier: dict[str, int] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    would_send: list[str] = field(default_factory=list)  # dry-run only
    failed: int = (
        0  # alerts that no configured push channel could deliver (not suppressed — failed)
    )
    _by_company: dict[str, int] = field(default_factory=dict)  # dry-run cap accounting

    def suppress(self, why: str) -> None:
        self.suppressed[why] = self.suppressed.get(why, 0) + 1


def _et_now() -> datetime:
    return datetime.now(UTC).astimezone(ET)


def in_quiet_hours(cfg: Config, now: datetime | None = None) -> bool:
    now = now or _et_now()
    start, end = cfg.notify.quiet_hours_et
    h = now.hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def _company_key(p: dict[str, Any]) -> str:
    return (
        f"id:{p['company_id']}"
        if p.get("company_id")
        else f"name:{(p.get('company_name') or '').lower()}"
    )


def _watchlist_company_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        r["id"]
        for r in db.all_rows(
            conn, "SELECT id FROM companies WHERE tags_json LIKE '%\"watchlist\"%'"
        )
    }


def _p0_filters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.all_rows(conn, "SELECT * FROM saved_filters WHERE alert_tier = 'p0'")


# Rows first seen during a source's seed scan are new to us, not to the market (migration 0008).
NOT_SEED_JOIN = (
    "LEFT JOIN company_sources s ON s.provider = p.source_provider AND s.slug = p.source_slug"
)
NOT_SEED_WHERE = "(s.seed_completed_at IS NULL OR p.first_seen_at > s.seed_completed_at)"


def find_candidates(conn: sqlite3.Connection, cfg: Config, *, since: str) -> list[Candidate]:
    """New (first_seen ≥ since) canonical, in-scope, floor-passing postings, classified by trigger."""
    from radar.query import compile_query

    rows = db.all_rows(
        conn,
        f"SELECT p.* FROM postings p {NOT_SEED_JOIN} "
        f"WHERE p.first_seen_at >= ? AND p.is_cluster_canonical = 1 AND p.delisted_at IS NULL AND {NOT_SEED_WHERE} "
        "AND p.in_scope = 1 AND p.is_stretch = 0 AND p.floor_result IN ('pass','exempt') AND p.status NOT IN ('applied','dismissed') ORDER BY p.priority DESC",
        (since,),
    )
    watch = _watchlist_company_ids(conn)
    p0_filters = _p0_filters(conn)
    out: list[Candidate] = []
    for r in rows:
        p = dict(r)
        eng = p.get("role_family") in (
            "software_engineering",
            "ml_ai",
            "data_engineering",
            "devops_sre",
            "security",
        )
        new_grad = bool(p.get("is_new_grad"))
        est_close = p.get("est_days_to_close")
        if p.get("repost_of_id") and not p.get("changed_since_first_seen"):
            out.append(Candidate(p, "none", "repost", "repost without material change"))
            continue
        if not p.get("has_requirements"):
            # D64: unenriched = not apply-ready = never a page. It sits in needs_review until the
            # enrichment pass (which works the queue top-down) extracts its requirements.
            out.append(Candidate(p, "none", "unenriched_needs_review", ""))
            continue
        if p.get("is_dream_list") and eng and new_grad:
            out.append(
                Candidate(
                    p,
                    "p0",
                    "dream_list_new_grad",
                    f"{p['company_name']} opened a new-grad engineering req",
                )
            )
            continue
        dl = parse_dt(p.get("application_deadline"))
        dl_days = (dl.date() - utcnow().date()).days if dl else None
        closing = (dl_days is not None and 0 <= dl_days < 5) or (
            est_close is not None and 0 < est_close < 5
        )  # est == 0 means "older than the typical lifetime" — unknown, not imminent
        if p.get("beats_baseline") == "clearly_better" and closing:
            out.append(
                Candidate(
                    p,
                    "p0",
                    "clearly_better_closing",
                    f"clearly better and closes {dl_days}d from now"
                    if dl_days is not None
                    else f"clearly better and est. {est_close:.0f} days to close",
                )
            )
            continue
        matched_filter = None
        for f in p0_filters:
            try:
                c = compile_query(f["query"])
            except Exception:
                continue
            if db.scalar(
                conn,
                f"SELECT 1 FROM postings p WHERE p.id = ? AND ({c.where})",
                [p["id"], *c.params],
            ):
                matched_filter = f["name"]
                break
        if matched_filter:
            out.append(
                Candidate(p, "p0", "saved_filter", f"matches your P0 filter “{matched_filter}”")
            )
            continue
        if p.get("beats_baseline") == "clearly_better":
            out.append(
                Candidate(
                    p,
                    "p1",
                    "clearly_better",
                    p.get("beats_baseline_reason") or "clearly better than baseline",
                )
            )
            continue
        if p.get("company_id") in watch and eng:
            out.append(
                Candidate(
                    p,
                    "p1",
                    "watchlist_company",
                    f"new engineering req at watchlist company {p['company_name']}",
                )
            )
            continue
        out.append(Candidate(p, "none", "not_alert_worthy", ""))
    # shortlisted deadlines inside 72h (P0), independent of first_seen
    for r in db.all_rows(
        conn,
        "SELECT * FROM postings WHERE status = 'shortlisted' AND application_deadline IS NOT NULL AND julianday(application_deadline) - julianday('now') BETWEEN 0 AND 3",
    ):
        out.append(
            Candidate(
                dict(r),
                "p0",
                "shortlist_deadline_72h",
                f"shortlisted role's deadline is {r['application_deadline'][:10]}",
            )
        )
    return out


def payload_for(p: dict[str, Any], tier: str, reason: str, cfg: Config) -> Payload:
    base = (
        f"${p['base_posted_min'] / 1000:.0f}–{p['base_posted_max'] / 1000:.0f}k posted"
        if p.get("base_posted_min") and p.get("base_posted_max")
        else (
            f"~${p['base_est'] / 1000:.0f}k est ({(p.get('comp_confidence') or 0):.0%} conf)"
            if p.get("base_est")
            else "no comp signal"
        )
    )
    verdict = {
        "clearly_better": "clearly better than baseline",
        "arguably_better": "arguably better than baseline",
        "worse": "worse than baseline",
    }.get(p.get("beats_baseline") or "", "unscored")
    from radar.parse.locations import load_metros

    metros = load_metros().metros
    mk = p.get("primary_metro")
    loc = (metros.get(mk, {}).get("name") if mk else None) or (mk or "location ?").replace("_", " ")
    close = p.get("est_days_to_close")
    if close is None:
        closing = "close date unknown"
    elif close <= 0:
        closing = "past its typical lifetime — could close any day"  # D40: 0 means "older than median", not imminent
    else:
        closing = f"est. {close:.0f} days to close"
    lines = [
        f"{loc} · base {base} · {verdict}",
        f"why: {reason}",
        f"{closing} · queue #{p.get('apply_priority_rank') or '—'} · fit {p.get('fit_score') or 0:.2f}",
    ]
    pid = p["id"]
    buttons = [
        ("☆ Shortlist", f"act:shortlist:{pid}"),
        ("✓ Applied", f"act:applied:{pid}"),
        ("✕ Dismiss", f"act:dismiss:{pid}"),
        ("zz 7d", f"act:snooze:{pid}"),
    ]
    return Payload(
        tier=tier,
        title=f"[{tier.upper()}] {p['company_name']} — {p['title']}",
        body_lines=lines,
        url=p["apply_url"],
        posting_id=pid,
        buttons=buttons,
    )


def _already_sent(conn: sqlite3.Connection, key: str) -> bool:
    return bool(db.scalar(conn, "SELECT 1 FROM notifications WHERE dedupe_key = ? LIMIT 1", (key,)))


def _count_today(conn: sqlite3.Connection, where: str, params: list[Any]) -> int:
    return int(
        db.scalar(
            conn,
            f"SELECT COUNT(*) FROM notifications WHERE sent_at >= ? AND {where}",
            [utcnow_iso()[:10], *params],
        )
        or 0
    )


def _last_p0_at(conn: sqlite3.Connection) -> datetime | None:
    v = db.scalar(conn, "SELECT MAX(sent_at) FROM notifications WHERE tier = 'p0'")
    return parse_dt(v) if v else None


def mark_ignored_and_escalate(conn: sqlite3.Connection) -> int:
    """Notifications older than 48 h with no engagement count as ignored; three consecutive ignored
    P1s from one company demote that company to P2 (note recorded, surfaced in the weekly digest)."""
    cutoff = (utcnow() - timedelta(hours=IGNORED_AFTER_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.all_rows(
        conn,
        "SELECT n.id, n.tier, p.company_id, p.company_name FROM notifications n LEFT JOIN postings p ON p.id = n.posting_id WHERE n.engaged IS NULL AND n.sent_at < ? AND n.tier IN ('p0','p1')",
        (cutoff,),
    )
    demoted = 0
    with db.transaction(conn):
        for r in rows:
            db.update(conn, "notifications", r["id"], {"engaged": 0, "engagement": "ignored"})
            if r["tier"] != "p1":
                continue
            key = (
                f"id:{r['company_id']}"
                if r["company_id"]
                else f"name:{(r['company_name'] or '').lower()}"
            )
            st = db.one(conn, "SELECT * FROM notify_company_state WHERE company_key = ?", (key,))
            n = (st["consecutive_ignored"] if st else 0) + 1
            demote = n >= ESCALATION_IGNORED_P1S
            conn.execute(
                "INSERT INTO notify_company_state (company_key, consecutive_ignored, demoted_to, demoted_at, note) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(company_key) DO UPDATE SET consecutive_ignored = excluded.consecutive_ignored, demoted_to = COALESCE(excluded.demoted_to, notify_company_state.demoted_to), demoted_at = COALESCE(excluded.demoted_at, notify_company_state.demoted_at), note = COALESCE(excluded.note, notify_company_state.note)",
                (
                    key,
                    n,
                    "p2" if demote else None,
                    utcnow_iso() if demote else None,
                    f"{r['company_name']}: {n} consecutive P1 alerts ignored → demoted to daily digest"
                    if demote
                    else None,
                ),
            )
            demoted += int(demote)
    return demoted


def record_engagement(conn: sqlite3.Connection, posting_id: int, engagement: str) -> None:
    """A shortlist/applied/dismiss on a notified posting counts as engagement and resets escalation."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE notifications SET engaged = 1, engaged_at = ?, engagement = ? WHERE posting_id = ? AND engaged IS NULL",
            (utcnow_iso(), engagement, posting_id),
        )
        r = db.one(
            conn, "SELECT company_id, company_name FROM postings WHERE id = ?", (posting_id,)
        )
        if r:
            key = (
                f"id:{r['company_id']}"
                if r["company_id"]
                else f"name:{(r['company_name'] or '').lower()}"
            )
            conn.execute(
                "UPDATE notify_company_state SET consecutive_ignored = 0, demoted_to = NULL WHERE company_key = ?",
                (key,),
            )


def send_alerts(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    since: str | None = None,
    channels: list[Channel] | None = None,
    dry_run: bool = False,
    run_id: int | None = None,
) -> NotifyStats:
    """Evaluate triggers for postings first seen since `since` (default: last notify run) and send P0/P1."""
    stats = NotifyStats()
    if since is None and db.kv_get(conn, "last_notify_at") is None:
        # first run ever: set the watermark and alert on nothing — the backlog belongs in the dashboard
        if not dry_run:
            db.kv_set(conn, "last_notify_at", utcnow_iso())
        stats.channels = ["seeded"]
        return stats
    since = since or db.kv_get(conn, "last_notify_at") or ""
    mark_ignored_and_escalate(conn)
    chans = [
        c
        for c in (channels or all_channels(str(cfg.data_dir / "notifications.log")))
        if c.available()
    ]
    stats.channels = [c.name for c in chans]
    push_chans = [c for c in chans if c.name in ("telegram", "ios_shortcut")] or [
        c for c in chans if c.name == "file"
    ]
    quiet = in_quiet_hours(cfg)
    last_p0 = _last_p0_at(conn)
    demoted = {
        r["company_key"]
        for r in db.all_rows(
            conn, "SELECT company_key FROM notify_company_state WHERE demoted_to = 'p2'"
        )
    }
    toggles = cfg.notify.p0_triggers
    for c in find_candidates(conn, cfg, since=since):
        stats.candidates += 1
        if c.tier == "none":
            stats.suppress(c.trigger)
            continue
        p = c.posting
        if c.tier == "p0" and not toggles.get(c.trigger, True):
            stats.suppress(f"p0 trigger {c.trigger} disabled")
            continue
        ckey = _company_key(p)
        tier = c.tier
        if tier == "p1" and ckey in demoted:
            stats.suppress("company demoted to digest (ignored P1s)")
            continue
        dedupe = f"{tier}:cluster:{p.get('cluster_id') or p['id']}"
        if _already_sent(conn, dedupe) or _already_sent(
            conn, f"p0:cluster:{p.get('cluster_id') or p['id']}"
        ):
            stats.suppress("already alerted on this cluster")
            continue
        if quiet and tier != "p0":
            stats.suppress("quiet hours (P1 held for the digest)")
            continue
        if (
            tier == "p1"
            and last_p0
            and (utcnow() - last_p0).total_seconds() < P0_COOLDOWN_MINUTES * 60
        ):
            stats.suppress("cooldown after a P0")
            continue
        # dry runs persist nothing, so add this run's would-sends to the DB counts
        if _count_today(conn, "tier = ?", [tier]) + (
            stats.by_tier.get(tier, 0) if dry_run else 0
        ) >= PER_TIER_DAILY_CAP.get(tier, 99):
            stats.suppress(f"daily cap for {tier}")
            continue
        if (
            _count_today(
                conn,
                "posting_id IN (SELECT id FROM postings WHERE company_id IS ? OR company_name = ?)",
                [p.get("company_id"), p.get("company_name")],
            )
            + (stats._by_company.get(ckey, 0) if dry_run else 0)
            >= PER_COMPANY_DAILY_CAP
        ):
            stats.suppress("per-company daily cap")
            continue
        payload = payload_for(p, tier, c.reason, cfg)
        sent_any = False
        if dry_run:
            # never persist: dry-run rows would count toward caps/dedupe on the next real run
            stats.would_send.append(f"{tier} {p['company_name']} — {p['title']} ({c.reason})")
            sent_any = True
        for ch in [] if dry_run else push_chans:
            ext = ch.send(payload)
            if ext:
                sent_any = True
                db.insert(
                    conn,
                    "notifications",
                    {
                        "run_id": run_id,
                        "posting_id": p["id"],
                        "cluster_id": p.get("cluster_id"),
                        "tier": tier,
                        "trigger": c.trigger,
                        "channel": ch.name,
                        "sent_at": utcnow_iso(),
                        "payload_json": json.dumps(
                            {
                                "title": payload.title,
                                "lines": payload.body_lines,
                                "url": payload.url,
                            }
                        ),
                        "external_id": ext,
                        "dedupe_key": dedupe,
                        "reason": c.reason,
                        "quiet_hours_override": int(quiet and tier == "p0"),
                    },
                )
        if not sent_any and not dry_run and push_chans:
            stats.failed += 1
            log.error(
                "alert for posting %s not delivered on any channel %s",
                p["id"],
                [c.name for c in push_chans],
            )
        if sent_any:
            stats.sent += 1
            stats.by_tier[tier] = stats.by_tier.get(tier, 0) + 1
            if tier == "p0":
                last_p0 = utcnow()
            stats._by_company[ckey] = stats._by_company.get(ckey, 0) + 1
    if not dry_run:
        db.kv_set(conn, "last_notify_at", utcnow_iso())
    return stats


def precision_report(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    since = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.all_rows(
        conn,
        "SELECT tier, COUNT(*) n, SUM(COALESCE(engaged,0)) e, SUM(CASE WHEN engaged IS NULL THEN 1 ELSE 0 END) pending FROM notifications WHERE sent_at >= ? AND channel NOT LIKE '%:dry' GROUP BY tier",
        (since,),
    )
    out = {
        r["tier"]: {
            "sent": r["n"],
            "engaged": r["e"],
            "pending": r["pending"],
            "precision": (r["e"] / (r["n"] - r["pending"])) if (r["n"] - r["pending"]) else None,
        }
        for r in rows
    }
    notes = [
        r["note"]
        for r in db.all_rows(
            conn,
            "SELECT note FROM notify_company_state WHERE demoted_to = 'p2' AND note IS NOT NULL",
        )
    ]
    return {"window_days": days, "by_tier": out, "demotions": notes}
