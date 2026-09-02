"""P2 daily and P3 weekly digests (§20), the `.ics` deadline feed, and the consolidated
"here's what happened while you were closed" summary (catch-up on wake, §4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from radar import db
from radar.config import Config
from radar.notify.channels import Channel, Payload, all_channels
from radar.notify.engine import NOT_SEED_JOIN, NOT_SEED_WHERE
from radar.util import parse_dt, utcnow, utcnow_iso


def _fmt_row(r: Any) -> str:
    base = (
        f"${r['base_posted_min'] / 1000:.0f}–{r['base_posted_max'] / 1000:.0f}k"
        if r["base_posted_min"] and r["base_posted_max"]
        else (f"~${r['base_est'] / 1000:.0f}k est" if r["base_est"] else "no comp")
    )
    verdict = {
        "clearly_better": "clearly better",
        "arguably_better": "arguably better",
        "worse": "worse",
    }.get(r["beats_baseline"] or "", "—")
    return f"• {r['company_name']} — {r['title']} · {(r['primary_metro'] or '?').replace('_', ' ')} · {base} · {verdict}\n  {r['apply_url']}"


def daily_digest(conn: sqlite3.Connection, cfg: Config, *, since_hours: int = 24) -> Payload:
    since = (utcnow() - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = db.all_rows(
        conn,
        f"SELECT p.* FROM postings p {NOT_SEED_JOIN} WHERE p.first_seen_at >= ? AND {NOT_SEED_WHERE} AND p.in_scope = 1 AND p.is_stretch = 0 "
        "AND p.floor_result IN ('pass','exempt') AND p.is_cluster_canonical = 1 AND p.delisted_at IS NULL ORDER BY p.target_category, p.priority DESC LIMIT 120",
        (since,),
    )
    lines: list[str] = []
    by_cat: dict[str, list[Any]] = {}
    for r in new:
        by_cat.setdefault(r["target_category"] or "other", []).append(r)
    lines.append(f"{len(new)} new postings cleared the floor in the last {since_hours}h.")
    for cat, rows in sorted(by_cat.items(), key=lambda kv: cfg.target_ranking.get(kv[0], 9)):
        lines.append(f"\n{cat.replace('_', ' ').upper()} ({len(rows)})")
        lines.extend(_fmt_row(r) for r in rows[:8])
        if len(rows) > 8:
            lines.append(f"  … and {len(rows) - 8} more in the dashboard")
    today = db.all_rows(
        conn,
        "SELECT * FROM postings WHERE queue_action = 'apply_today' ORDER BY apply_priority_rank LIMIT ?",
        (cfg.throughput.today_bucket_max,),
    )
    lines.append(f"\nTODAY'S QUEUE ({len(today)})")
    lines.extend(_fmt_row(r) for r in today)
    from radar.applications import suggestions

    sug = suggestions(conn)
    if sug["follow_ups_due"]:
        lines.append("\nFOLLOW-UPS DUE")
        lines.extend(
            f"• #{a['id']} {a['company_name']} — {a['title']} (applied {a['applied_at'][:10]})"
            for a in sug["follow_ups_due"]
        )
    return Payload(
        tier="p2",
        title=f"Job Radar daily — {date.today().isoformat()}",
        body_lines=lines,
        html=False,
    )


def weekly_digest(conn: sqlite3.Connection, cfg: Config) -> Payload:
    from radar.applications import funnel_stats
    from radar.notify.engine import precision_report
    from radar.score.engine import decision_calendar

    week_ago = (utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cal = decision_calendar(cfg)
    lines: list[str] = []
    # market trends
    new_week = (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM postings WHERE first_seen_at >= ? AND in_scope = 1",
            (week_ago,),
        )
        or 0
    )
    new_prev = (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM postings WHERE first_seen_at >= ? AND first_seen_at < ? AND in_scope = 1",
            ((utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"), week_ago),
        )
        or 0
    )
    delisted_week = (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM postings WHERE delisted_at >= ? AND in_scope = 1",
            (week_ago,),
        )
        or 0
    )
    lines.append(
        f"MARKET: {new_week} in-scope reqs opened this week (prev week {new_prev}); {delisted_week} closed."
    )
    by_cat = db.all_rows(
        conn,
        "SELECT target_category k, COUNT(*) n FROM postings WHERE first_seen_at >= ? AND in_scope = 1 GROUP BY k ORDER BY n DESC",
        (week_ago,),
    )
    lines.append(
        "  by category: "
        + " · ".join(f"{(r['k'] or 'other').replace('_', ' ')} {r['n']}" for r in by_cat)
    )
    # season tracker
    s = cal["season"]
    lines.append(
        f"\nSEASON: {s['elapsed_fraction']:.0%} of Sept–Dec elapsed. Baseline deadline {cal['baseline_decision_deadline']} ({cal['days_to_deadline']} days). Switching today ≈ ${cal['switching_window']['today']['total']:,.0f}; cheap zone ends {cal['switching_window']['cheap_zone_ends']}."
    )
    # closed before applied
    missed = db.all_rows(
        conn,
        "SELECT company_name, title, apply_url FROM postings WHERE delisted_at >= ? AND in_scope = 1 AND beats_baseline IN ('clearly_better','arguably_better') AND status IN ('new','shortlisted') ORDER BY composite_score DESC LIMIT 10",
        (week_ago,),
    )
    if missed:
        lines.append(f"\nCLOSED BEFORE YOU APPLIED ({len(missed)})")
        lines.extend(f"• {r['company_name']} — {r['title']}" for r in missed)
    # watchlist
    watch = db.all_rows(
        conn,
        "SELECT * FROM postings WHERE floor_result = 'fail' AND (career_capital_score >= 0.8 OR fit_score >= 0.7) AND delisted_at IS NULL AND is_cluster_canonical = 1 ORDER BY career_capital_score DESC LIMIT 8",
    )
    if watch:
        lines.append(f"\nWATCHLIST (failed the comp floor, strong otherwise) ({len(watch)})")
        lines.extend(_fmt_row(r) for r in watch)
    # funnel
    st = funnel_stats(conn)
    lines.append(
        f"\nAPPLICATIONS: {st['total']} total · {st['active']} active · response rate {(st['response_rate'] or 0):.0%} · by source {st['by_source']}"
    )
    # calibration + notification precision
    pr = precision_report(conn)
    lines.append(
        "\nNOTIFICATIONS (7d): "
        + (
            " · ".join(
                f"{t}: {v['sent']} sent, {v['engaged']} acted on"
                + (f" ({v['precision']:.0%} precision)" if v["precision"] is not None else "")
                for t, v in pr["by_tier"].items()
            )
            or "none sent"
        )
    )
    if pr["demotions"]:
        lines.append("  escalating suppression: " + "; ".join(pr["demotions"]))
    p0s = pr["by_tier"].get("p0", {}).get("sent", 0)
    if p0s > cfg.notify.p0_max_per_week_target:
        lines.append(
            f"  ⚠ {p0s} P0s this week exceeds your target of {cfg.notify.p0_max_per_week_target}; the thresholds are too loose — tighten p0_triggers in config.yaml."
        )
    # source health
    stale = db.all_rows(
        conn,
        "SELECT c.name, cs.provider, cs.consecutive_failures FROM company_sources cs JOIN companies c ON c.id = cs.company_id WHERE cs.enabled = 1 AND cs.consecutive_failures >= 3 LIMIT 10",
    )
    drift = (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM runs WHERE started_at >= ? AND stats_json LIKE '%\"drift_alarms\": %' AND stats_json NOT LIKE '%\"drift_alarms\": 0%'",
            (week_ago,),
        )
        or 0
    )
    lines.append(
        f"\nSOURCE HEALTH: {len(stale)} sources failing"
        + (": " + ", ".join(f"{r['name']} ({r['provider']})" for r in stale) if stale else "")
        + f" · {drift} runs with drift alarms"
    )
    cal_note = db.one(conn, "SELECT created_at FROM config_versions ORDER BY id DESC LIMIT 1")
    last_cal = db.one(
        conn,
        "SELECT month, labeled, positives, proposal_json FROM calibration_runs ORDER BY id DESC LIMIT 1",
    )
    if last_cal:
        try:
            prop = json.loads(last_cal["proposal_json"]).get("proposed_weights")
        except (json.JSONDecodeError, TypeError):
            prop = None
        lines.append(
            f"\nCALIBRATION ({last_cal['month']}): {last_cal['labeled']} labeled ({last_cal['positives']} kept) → "
            + (
                "proposed weights in CALIBRATION.md: "
                + ", ".join(f"{k.replace('_score', '')} {v:.2f}" for k, v in prop.items())
                if prop
                else "not enough signal yet — keep dismissing with a reason"
            )
            + f". Config last changed {cal_note['created_at'][:10] if cal_note else 'never'}."
        )
    else:
        lines.append(
            f"\nCALIBRATION: none yet (runs monthly from `radar nightly`). Config last changed {cal_note['created_at'][:10] if cal_note else 'never'}."
        )
    # velocity (weekly snapshots): learned time-to-close is the delist watcher paying off
    learned = (
        db.scalar(conn, "SELECT COUNT(*) FROM companies WHERE median_days_to_close IS NOT NULL")
        or 0
    )
    snap = db.all_rows(
        conn,
        "SELECT week_start, new_reqs, closed_reqs, in_scope_open FROM velocity_snapshots WHERE company_id IS NULL ORDER BY week_start DESC LIMIT 4",
    )
    if snap:
        lines.append(
            "\nVELOCITY: "
            + " · ".join(
                f"wk {r['week_start'][5:]}: +{r['new_reqs']} −{r['closed_reqs']} ({r['in_scope_open']} in-scope open)"
                for r in snap
            )
            + f" · learned time-to-close for {learned} companies"
        )
    return Payload(
        tier="p3",
        title=f"Job Radar weekly — week of {date.today().isoformat()}",
        body_lines=lines,
        html=False,
    )


def wake_summary(conn: sqlite3.Connection, cfg: Config, *, since: str) -> Payload | None:
    """One consolidated summary of what happened while the laptop was closed (instead of a burst)."""
    new = (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM postings WHERE first_seen_at >= ? AND in_scope = 1 AND is_cluster_canonical = 1",
            (since,),
        )
        or 0
    )
    if not new:
        return None
    top = db.all_rows(
        conn,
        "SELECT * FROM postings WHERE first_seen_at >= ? AND in_scope = 1 AND is_cluster_canonical = 1 ORDER BY priority DESC LIMIT 8",
        (since,),
    )
    dt = parse_dt(since)
    hours = (utcnow() - dt).total_seconds() / 3600 if dt else 0
    lines = [f"While you were closed ({hours:.0f}h): {new} new in-scope postings. Top of them:"]
    lines.extend(_fmt_row(r) for r in top)
    return Payload(tier="p2", title="Job Radar — while you were away", body_lines=lines, html=False)


def send_digest(
    conn: sqlite3.Connection,
    cfg: Config,
    kind: str,
    *,
    channels: list[Channel] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = daily_digest(conn, cfg) if kind == "daily" else weekly_digest(conn, cfg)
    chans = [
        c
        for c in (channels or all_channels(str(cfg.data_dir / "notifications.log")))
        if c.available()
    ]
    # digests go to email if configured, plus telegram (text only), always to the file log
    targets = [c for c in chans if c.name in ("email", "telegram", "file")]
    sent = []
    for ch in targets:
        ext = (
            None
            if dry_run
            else ch.send(
                Payload(
                    tier=payload.tier,
                    title=payload.title,
                    body_lines=payload.body_lines,
                    html=False,
                )
            )
        )
        if dry_run or ext:
            sent.append(ch.name)
    if (
        not dry_run
    ):  # dry runs persist nothing (else the scheduler would think today's digest went out)
        db.insert(
            conn,
            "notifications",
            {
                "posting_id": None,
                "tier": payload.tier,
                "trigger": kind,
                "channel": ",".join(sent),
                "sent_at": utcnow_iso(),
                "payload_json": "{}",
                "reason": kind,
            },
        )
        db.kv_set(conn, f"last_{kind}_digest_at", utcnow_iso())
    return {
        "kind": kind,
        "channels": sent,
        "lines": len(payload.body_lines),
        "text": payload.text(),
    }


def ics_feed(conn: sqlite3.Connection, cfg: Config) -> str:
    """Deadlines (queue), follow-ups, and the two baseline dates as an iCalendar feed."""

    def ev(uid: str, d: str, summary: str, desc: str = "") -> str:
        dd = d.replace("-", "")[:8]
        return f"BEGIN:VEVENT\r\nUID:{uid}@job-radar\r\nDTSTAMP:{utcnow().strftime('%Y%m%dT%H%M%SZ')}\r\nDTSTART;VALUE=DATE:{dd}\r\nSUMMARY:{summary}\r\nDESCRIPTION:{desc}\r\nEND:VEVENT\r\n"

    out = [
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//job-radar//EN\r\nX-WR-CALNAME:Job Radar\r\n"
    ]
    out.append(
        ev(
            "baseline-deadline",
            cfg.baseline.decision_deadline.isoformat(),
            "Baseline decision deadline",
            "Sign, then keep looking (no competing offer will exist by today).",
        )
    )
    out.append(
        ev(
            "baseline-start",
            cfg.baseline.start_date.isoformat(),
            "Baseline start date",
            "Switching friction peaks here.",
        )
    )
    out.append(
        ev(
            "cheap-zone-end",
            cfg.switching_friction.cheap_zone_ends.isoformat(),
            "Switching cheap zone ends",
            "Friction curve rises after this date.",
        )
    )
    for r in db.all_rows(
        conn,
        "SELECT id, company_name, title, application_deadline, apply_url FROM postings WHERE application_deadline >= date('now') AND apply_priority_rank IS NOT NULL ORDER BY application_deadline LIMIT 200",
    ):
        out.append(
            ev(
                f"deadline-{r['id']}",
                r["application_deadline"][:10],
                f"Deadline: {r['company_name']} — {r['title']}",
                r["apply_url"],
            )
        )
    for a in db.all_rows(
        conn,
        "SELECT id, company_name, title, follow_up_due FROM applications WHERE completed = 0 AND follow_up_due IS NOT NULL",
    ):
        out.append(
            ev(
                f"followup-{a['id']}",
                a["follow_up_due"][:10],
                f"Follow up: {a['company_name']} — {a['title']}",
            )
        )
    out.append("END:VCALENDAR\r\n")
    return "".join(out)
