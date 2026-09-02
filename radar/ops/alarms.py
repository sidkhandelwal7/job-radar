"""System alarms (§16): "no silent failures". Evaluated after every cycle and by `radar health`.

Each alarm has a stable key; it is pushed at most once per 24 h (dedupe_key `sys:<key>:<date>`)
through the same channels as P1 alerts and always lands in notifications.log and the dashboard's
Notifications panel. Alarms never break quiet hours — a broken adapter can wait until morning.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from radar import db
from radar.config import Config
from radar.notify.channels import Channel, Payload, all_channels
from radar.notify.engine import in_quiet_hours
from radar.util import parse_dt, utcnow, utcnow_iso


@dataclass
class Alarm:
    key: str
    title: str
    detail: str
    severity: str = "warn"  # warn | error


def evaluate(conn: sqlite3.Connection, cfg: Config) -> list[Alarm]:
    out: list[Alarm] = []
    # drifted sources (row count collapsed on a complete scan)
    drift = db.all_rows(
        conn,
        "SELECT c.name, cs.provider, cs.drift_note FROM company_sources cs JOIN companies c ON c.id = cs.company_id WHERE cs.drift_note IS NOT NULL AND cs.enabled = 1 ORDER BY cs.typical_row_count DESC LIMIT 10",
    )
    if drift:
        out.append(
            Alarm(
                "drift",
                f"{len(drift)} source(s) drifted — adapter or board change, not a quiet market",
                "; ".join(f"{r['name']} ({r['provider']}: {r['drift_note']})" for r in drift[:5]),
                "error",
            )
        )
    failing = db.all_rows(
        conn,
        "SELECT c.name, cs.provider, cs.consecutive_failures, cs.last_error FROM company_sources cs JOIN companies c ON c.id = cs.company_id "
        "WHERE cs.enabled = 1 AND cs.consecutive_failures >= 5 AND cs.cadence IN ('15min','hourly') ORDER BY cs.consecutive_failures DESC LIMIT 10",
    )
    if failing:
        out.append(
            Alarm(
                "failing_tier1",
                f"{len(failing)} Tier-1/Tier-2 source(s) failing repeatedly",
                "; ".join(
                    f"{r['name']} ({r['provider']}) ×{r['consecutive_failures']}: {(r['last_error'] or '')[:60]}"
                    for r in failing[:5]
                ),
                "error",
            )
        )
    # stale cycle: nothing ran for 3× the 15-min interval while the machine was awake is unknowable;
    # instead alarm when the last cycle is > 26 h old (a full day missed even with sleep)
    last = parse_dt(db.kv_get(conn, "last_cycle_at"))
    if last and utcnow() - last > timedelta(hours=26):
        out.append(
            Alarm(
                "stale_cycle",
                "no cycle in over 26 hours",
                f"last cycle {last.isoformat()[:16]}Z — launchd agent unloaded?",
                "error",
            )
        )
    # backups
    lb = parse_dt(db.kv_get(conn, "last_backup_at"))
    if lb is None or utcnow() - lb > timedelta(hours=48):
        out.append(
            Alarm(
                "backup_stale",
                "no verified backup in 48 h",
                f"last backup {lb.isoformat()[:16] + 'Z' if lb else 'never'} — run `radar backup`",
                "warn",
            )
        )
    # GitHub Actions invocation count (raw; minutes come from GitHub's billing page — D52)
    try:
        from radar.actions_runner import invocations_this_month

        n = invocations_this_month(cfg.root / "actions")
        if n >= cfg.actions.invocation_target_per_month:
            out.append(
                Alarm(
                    "actions_invocations",
                    f"GitHub Actions ran {n} times this month (target < {cfg.actions.invocation_target_per_month})",
                    "each job bills at least one of the 2,000 free minutes; check Settings → Billing and thin the schedule",
                    "warn",
                )
            )
    except Exception:
        pass
    # LLM accounting: refuse to stay silent if enrichment is burning more than 3× the per-cycle budget per day
    day_calls = (
        db.scalar(
            conn,
            "SELECT COALESCE(SUM(llm_calls),0) FROM runs WHERE started_at >= ?",
            ((utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        or 0
    )
    if cfg.llm.enabled and day_calls > max(50, cfg.llm.calls_per_cycle * 96):
        out.append(
            Alarm(
                "llm_volume",
                f"{day_calls} LLM calls in 24 h",
                "more than the per-cycle budget allows — check the cache and `radar health`",
                "warn",
            )
        )
    # dead-letter growth
    dl = (
        db.scalar(
            conn,
            "SELECT COUNT(*) FROM dead_letters WHERE created_at >= ?",
            ((utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        or 0
    )
    if dl >= 50:
        out.append(
            Alarm(
                "dead_letters",
                f"{dl} parse failures in 24 h",
                "review with `radar sources --dead-letters`",
                "warn",
            )
        )
    return out


def push(
    conn: sqlite3.Connection,
    cfg: Config,
    alarms: list[Alarm],
    *,
    channels: list[Channel] | None = None,
) -> int:
    if not alarms:
        return 0
    chans = [
        c
        for c in (channels or all_channels(str(cfg.data_dir / "notifications.log")))
        if c.available()
    ]
    quiet = in_quiet_hours(cfg)
    today = utcnow_iso()[:10]
    sent = 0
    for a in alarms:
        key = f"sys:{a.key}:{today}"
        if db.scalar(conn, "SELECT 1 FROM notifications WHERE dedupe_key = ?", (key,)):
            continue
        payload = Payload(
            tier="system",
            title=f"[Radar {a.severity}] {a.title}",
            body_lines=[a.detail],
            html=False,
        )
        delivered = []
        for ch in chans:
            if quiet and ch.name != "file":
                continue
            if ch.send(payload):
                delivered.append(ch.name)
        if delivered or quiet:
            db.insert(
                conn,
                "notifications",
                {
                    "posting_id": None,
                    "tier": "system",
                    "trigger": a.key,
                    "channel": ",".join(delivered) or "held:quiet",
                    "sent_at": utcnow_iso(),
                    "payload_json": json.dumps(
                        {"title": payload.title, "lines": payload.body_lines}
                    ),
                    "dedupe_key": key,
                    "reason": a.detail[:300],
                },
            )
            sent += 1
    return sent


def as_dicts(alarms: list[Alarm]) -> list[dict[str, Any]]:
    return [a.__dict__ for a in alarms]
