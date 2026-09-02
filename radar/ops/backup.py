"""Nightly SQLite backup with a *tested* restore path (§16).

`backup()` uses the sqlite3 online-backup API (consistent even while the WAL is live), gzips the
copy, verifies it by opening and running `PRAGMA integrity_check`, and prunes to 7 dailies + 4
weeklies. `restore()` never overwrites the live DB blind: it restores to a temp file, checks
integrity + row counts, moves the live DB aside as `radar.db.replaced-<ts>`, then swaps.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from radar import db
from radar.config import Config
from radar.util import utcnow, utcnow_iso

KEEP_DAILY = 7
KEEP_WEEKLY = 4  # a 3 GB DB gzips to ~700 MB; 11 copies is the ceiling for a laptop
CORE_TABLES = (
    "postings",
    "applications",
    "posting_events",
    "companies",
    "company_sources",
    "raw_payloads",
)


@dataclass
class BackupInfo:
    path: Path
    bytes: int
    tables: dict[str, int]
    integrity: str
    elapsed_s: float


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in CORE_TABLES:
        try:
            out[t] = int(db.scalar(conn, f"SELECT COUNT(*) FROM {t}") or 0)
        except sqlite3.Error:
            out[t] = -1
    return out


def backup(cfg: Config, conn: sqlite3.Connection, *, dest_dir: Path | None = None) -> BackupInfo:
    t0 = utcnow()
    dest_dir = dest_dir or cfg.backups_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow_iso().replace(":", "").replace("-", "")[:15]
    raw = dest_dir / f"radar-{stamp}.db"
    dst = sqlite3.connect(raw)
    try:
        conn.backup(dst, pages=4096)
    finally:
        dst.close()
    # verify the copy before compressing
    check = sqlite3.connect(raw)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        tables = _counts(check)
    finally:
        check.close()
    gz = raw.with_suffix(".db.gz")
    with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    raw.unlink()
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    prune(dest_dir)
    db.kv_set(conn, "last_backup_at", utcnow_iso())
    db.kv_set(conn, "last_backup_path", str(gz))
    return BackupInfo(
        gz, gz.stat().st_size, tables, integrity, round((utcnow() - t0).total_seconds(), 1)
    )


def list_backups(dest_dir: Path) -> list[Path]:
    return sorted(dest_dir.glob("radar-*.db.gz"))


def _stamp_date(p: Path) -> date | None:
    try:
        return datetime.strptime(p.name[6:14], "%Y%m%d").date()
    except ValueError:
        return None


def prune(dest_dir: Path) -> list[Path]:
    """Keep the newest KEEP_DAILY dailies plus one per ISO week for KEEP_WEEKLY weeks; delete the rest."""
    files = list_backups(dest_dir)
    keep: set[Path] = set()
    by_date: dict[date, Path] = {}
    for p in files:
        d = _stamp_date(p)
        if d:
            by_date[d] = p  # newest of the day wins (sorted ascending)
    days = sorted(by_date, reverse=True)
    keep.update(by_date[d] for d in days[:KEEP_DAILY])
    weeks: dict[tuple[int, int], Path] = {}
    for d in days:
        wk = d.isocalendar()[:2]
        weeks.setdefault(wk, by_date[d])
    keep.update(list(weeks.values())[:KEEP_WEEKLY])
    removed = []
    for p in files:
        if (
            p not in keep
            and _stamp_date(p)
            and _stamp_date(p) < utcnow().date() - timedelta(days=1)
        ):
            p.unlink()
            removed.append(p)
    return removed


def verify_backup(path: Path, work_dir: Path) -> tuple[str, dict[str, int], Path]:
    """Decompress to work_dir, integrity-check, return (integrity, counts, restored_path)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / (path.name[:-3] if path.name.endswith(".gz") else path.name + ".restored")
    if path.name.endswith(".gz"):
        with gzip.open(path, "rb") as fi, open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
    else:
        shutil.copyfile(path, out)
    c = sqlite3.connect(out)
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _counts(c)
    finally:
        c.close()
    return integrity, counts, out


def restore(cfg: Config, path: Path, *, force: bool = False) -> dict[str, Any]:
    """Restore `path` over the live DB after verifying it. The previous live DB is kept beside it."""
    live = cfg.db_path
    work = live.parent / "restore-tmp"
    integrity, counts, restored = verify_backup(path, work)
    if integrity != "ok":
        restored.unlink(missing_ok=True)
        raise RuntimeError(f"refusing to restore: integrity_check = {integrity}")
    if counts.get("postings", 0) <= 0 and not force:
        restored.unlink(missing_ok=True)
        raise RuntimeError("refusing to restore a backup with zero postings (use --force)")
    replaced = None
    if live.exists():
        # compare with the live DB so a restore never silently loses applications
        lc = sqlite3.connect(live)
        try:
            live_counts = _counts(lc)
        finally:
            lc.close()
        if live_counts.get("applications", 0) > counts.get("applications", 0) and not force:
            restored.unlink(missing_ok=True)
            raise RuntimeError(
                f"live DB has {live_counts['applications']} applications, backup has {counts['applications']} — "
                "applied records are permanent; pass --force only if you mean to roll them back"
            )
        replaced = live.with_name(f"{live.name}.replaced-{utcnow_iso().replace(':', '')[:15]}")
        for suffix in ("", "-wal", "-shm"):
            p = live.with_name(live.name + suffix)
            if p.exists():
                p.rename(replaced.with_name(replaced.name + suffix))
    shutil.move(str(restored), str(live))
    shutil.rmtree(work, ignore_errors=True)
    # make sure it opens with our schema (migrations idempotent) and re-derive FTS
    c = db.connect(live)
    try:
        db.migrate(c)
        c.execute("INSERT INTO postings_fts(postings_fts) VALUES('rebuild')")
        c.commit()
        final = _counts(c)
    finally:
        c.close()
    return {
        "restored_from": str(path),
        "replaced": str(replaced) if replaced else None,
        "counts": final,
        "integrity": integrity,
    }


# --- workflow export: the irreplaceable few kilobytes -------------------------------------------
# Postings and scores can be rebuilt from the raw store; what cannot is what *you* did. Every
# nightly also writes data/backups/workflow-<stamp>.json.gz (applications, statuses, dismiss
# reasons, notes, saved filters) keyed by the natural posting key so it survives a DB rebuild.

WORKFLOW_KEEP = 90


def export_workflow(conn: sqlite3.Connection, dest_dir: Path) -> Path:
    import json

    dest_dir.mkdir(parents=True, exist_ok=True)
    postings = [
        dict(r)
        for r in db.all_rows(
            conn,
            "SELECT source_provider, source_slug, source_job_id, apply_url, company_name, title, status, status_changed_at, dismiss_reason, starred, snooze_until, tags_user_json, notes_md, "
            "referral_secured FROM postings WHERE status != 'new' OR starred = 1 OR (notes_md IS NOT NULL AND notes_md != '') OR (tags_user_json IS NOT NULL AND tags_user_json NOT IN ('[]', '')) OR snooze_until IS NOT NULL OR referral_secured = 1",
        )
    ]
    apps = [
        dict(r)
        for r in db.all_rows(
            conn,
            "SELECT a.*, p.source_provider, p.source_slug, p.source_job_id FROM applications a LEFT JOIN postings p ON p.id = a.posting_id",
        )
    ]
    events = [dict(r) for r in db.all_rows(conn, "SELECT * FROM application_events")]
    filters = [
        dict(r) for r in db.all_rows(conn, "SELECT * FROM saved_filters WHERE is_preset = 0")
    ]
    state = [dict(r) for r in db.all_rows(conn, "SELECT * FROM notify_company_state")]
    stamp = utcnow_iso().replace(":", "").replace("-", "")[:15]
    out = dest_dir / f"workflow-{stamp}.json.gz"
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(
            {
                "exported_at": utcnow_iso(),
                "postings": postings,
                "applications": apps,
                "application_events": events,
                "saved_filters": filters,
                "notify_company_state": state,
            },
            f,
            default=str,
        )
    files = sorted(dest_dir.glob("workflow-*.json.gz"))
    for old in files[:-WORKFLOW_KEEP]:
        old.unlink()
    return out


def import_workflow(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    """Re-apply statuses/notes/applications by natural key onto a rebuilt DB. Never downgrades an
    existing `applied` status and never deletes anything."""
    import json

    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    stats = {"postings": 0, "applications": 0, "saved_filters": 0, "skipped": 0}
    with db.transaction(conn):
        for p in data.get("postings", []):
            row = db.one(
                conn,
                "SELECT id, status FROM postings WHERE source_provider = ? AND source_slug = ? AND source_job_id = ?",
                (p["source_provider"], p["source_slug"], p["source_job_id"]),
            )
            if not row:
                stats["skipped"] += 1
                continue
            if row["status"] == "applied" and p["status"] != "applied":
                continue
            conn.execute(
                "UPDATE postings SET status = ?, status_changed_at = COALESCE(?, status_changed_at), dismiss_reason = ?, starred = ?, snooze_until = ?, tags_user_json = ?, notes_md = ?, referral_secured = ? WHERE id = ?",
                (
                    p["status"],
                    p.get("status_changed_at"),
                    p.get("dismiss_reason"),
                    p.get("starred") or 0,
                    p.get("snooze_until"),
                    p.get("tags_user_json"),
                    p.get("notes_md"),
                    p.get("referral_secured") or 0,
                    row["id"],
                ),
            )
            stats["postings"] += 1
        for a in data.get("applications", []):
            pid = None
            if a.get("source_provider"):
                r = db.one(
                    conn,
                    "SELECT id FROM postings WHERE source_provider = ? AND source_slug = ? AND source_job_id = ?",
                    (a["source_provider"], a["source_slug"], a["source_job_id"]),
                )
                pid = r["id"] if r else None
            exists = db.one(
                conn,
                "SELECT id FROM applications WHERE apply_url = ? AND applied_at = ?",
                (a.get("apply_url"), a.get("applied_at")),
            )
            if exists:
                continue
            cols = {
                k: v
                for k, v in a.items()
                if k not in ("id", "source_provider", "source_slug", "source_job_id")
            }
            cols["posting_id"] = pid
            db.insert(conn, "applications", cols)
            stats["applications"] += 1
        for fl in data.get("saved_filters", []):
            if not db.one(conn, "SELECT id FROM saved_filters WHERE name = ?", (fl["name"],)):
                db.insert(conn, "saved_filters", {k: v for k, v in fl.items() if k != "id"})
                stats["saved_filters"] += 1
    if stats["postings"]:
        from radar.score.views import stamp_view_flags

        stamp_view_flags(conn)
    return stats
