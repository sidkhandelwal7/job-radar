"""Phase 6: backups with tested restore, workflow export/import, snapshots (learned time-to-close),
calibration proposals, sanitized public export, system alarms, single-instance lock, launchd plists."""

import gzip
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from radar import db
from radar.util import utcnow, utcnow_iso


def _post(conn, **over):
    now = utcnow_iso()
    v = {
        "source": "company_direct",
        "source_provider": "greenhouse",
        "source_slug": "x",
        "source_job_id": over.pop("jid", "1"),
        "apply_url": f"https://x/{over.get('jid', '1')}",
        "first_seen_at": now,
        "last_seen_at": now,
        "company_name": "Acme",
        "company_id": None,
        "title": "Software Engineer, New Grad",
        "role_family": "software_engineering",
        "seniority": "new_grad",
        "is_new_grad": 1,
        "in_scope": 1,
        "floor_result": "pass",
        "is_cluster_canonical": 1,
        "priority": 0.5,
        "beats_baseline": "arguably_better",
        "comp_score": 0.5,
        "career_capital_score": 0.5,
        "fit_score": 0.5,
        "winnability_score": 0.5,
        "location_score": 0.5,
        "culture_score": 0.5,
    }
    v.update(over)
    return db.insert_posting(conn, v)


def test_backup_restore_roundtrip(tmp_project, conn):
    from radar.ops.backup import backup, list_backups, restore, verify_backup

    pid = _post(conn, jid="b1")
    conn.execute("UPDATE postings SET status = 'shortlisted' WHERE id = ?", (pid,))
    conn.commit()
    info = backup(tmp_project, conn)
    assert (
        info.integrity == "ok"
        and info.tables["postings"] == 1
        and info.path.name.endswith(".db.gz")
    )
    assert list_backups(tmp_project.backups_dir) == [info.path]
    # damage the live DB: drop the row, then restore
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM postings WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    integrity, counts, tmp = verify_backup(info.path, tmp_project.data_dir / "restore-tmp")
    assert integrity == "ok" and counts["postings"] == 1
    tmp.unlink()
    r = restore(tmp_project, info.path)
    assert r["counts"]["postings"] == 1 and r["replaced"] and Path(r["replaced"]).exists()
    c2 = db.connect(tmp_project.db_path)
    assert db.scalar(c2, "SELECT status FROM postings WHERE source_job_id = 'b1'") == "shortlisted"
    assert c2.execute("SELECT COUNT(*) FROM postings_fts").fetchone()[0] == 1  # FTS rebuilt
    # refuses to roll back applications without --force
    db.insert(
        c2,
        "applications",
        {
            "company_name": "Acme",
            "title": "t",
            "apply_url": "https://x/app",
            "applied_at": utcnow_iso(),
            "stage": "applied",
            "stage_changed_at": utcnow_iso(),
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        },
    )
    c2.commit()
    c2.close()
    with pytest.raises(RuntimeError, match="applications"):
        restore(tmp_project, info.path)


def test_backup_prune_keeps_daily_and_weekly(tmp_path):
    from radar.ops.backup import KEEP_DAILY, prune

    d = tmp_path / "b"
    d.mkdir()
    for i in range(60):
        day = (utcnow() - timedelta(days=i)).strftime("%Y%m%d")
        (d / f"radar-{day}T033000.db.gz").write_bytes(b"x")
    removed = prune(d)
    left = sorted(d.glob("radar-*.db.gz"))
    assert len(removed) + len(left) == 60
    assert KEEP_DAILY <= len(left) <= KEEP_DAILY + 4
    # newest is always kept
    assert any(utcnow().strftime("%Y%m%d") in p.name for p in left)


def test_workflow_export_import(tmp_project, conn):
    from radar.ops.backup import export_workflow, import_workflow
    from radar.workflow import apply_action

    a = _post(conn, jid="w1")
    b = _post(conn, jid="w2")
    apply_action(conn, a, "dismiss", reason="too far", via="test")
    apply_action(conn, b, "applied", via="test")
    out = export_workflow(conn, tmp_project.backups_dir)
    with gzip.open(out, "rt") as f:
        data = json.load(f)
    assert {p["source_job_id"] for p in data["postings"]} == {"w1", "w2"} and len(
        data["applications"]
    ) == 1
    # wipe workflow state, re-import by natural key
    conn.execute("UPDATE postings SET status = 'new', dismiss_reason = NULL")
    conn.execute("DELETE FROM application_events")
    conn.execute("DELETE FROM applications")
    conn.commit()
    st = import_workflow(conn, out)
    assert st["postings"] == 2 and st["applications"] == 1
    assert db.scalar(conn, "SELECT dismiss_reason FROM postings WHERE id = ?", (a,)) == "too far"
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (b,)) == "applied"
    # never downgrades applied
    conn.execute("UPDATE postings SET status = 'applied' WHERE id = ?", (a,))
    conn.commit()
    import_workflow(conn, out)
    assert db.scalar(conn, "SELECT status FROM postings WHERE id = ?", (a,)) == "applied"


def test_snapshot_learns_time_to_close(tmp_project, conn):
    from radar.ops.snapshot import take_snapshot

    cid = db.scalar(conn, "SELECT id FROM companies WHERE slug = 'stripe'")
    now = utcnow()
    for i, days in enumerate([20, 25, 30, 35, 40, 90]):
        posted = (now - timedelta(days=days + 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        closed = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _post(
            conn,
            jid=f"s{i}",
            company_id=cid,
            company_name="Stripe",
            posted_at=posted,
            delisted_at=closed,
        )
    _post(conn, jid="open1", company_id=cid, company_name="Stripe")
    r = take_snapshot(conn)
    assert r["companies_with_learned_ttc"] == 1 and r["closed_samples"] == 6
    med = db.scalar(conn, "SELECT median_days_to_close FROM companies WHERE id = ?", (cid,))
    assert 32 <= med <= 33  # median of 20,25,30,35,40,90 = 32.5
    row = db.one(conn, "SELECT * FROM velocity_snapshots WHERE company_id = ?", (cid,))
    assert row["open_reqs"] == 1 and row["closed_reqs"] == 6
    market = db.one(conn, "SELECT * FROM velocity_snapshots WHERE company_id IS NULL")
    assert market["open_reqs"] == 1
    # idempotent for the same week
    take_snapshot(conn)
    assert (
        db.scalar(conn, "SELECT COUNT(*) FROM velocity_snapshots WHERE company_id = ?", (cid,)) == 1
    )


def test_calibration_proposes_but_never_applies(tmp_project, conn, tmp_path):
    from radar.ops.calibrate import fit_logistic, run_calibration

    # revealed preference: keeps have high fit + comp, dismissals have low fit
    for i in range(15):
        _post(
            conn,
            jid=f"k{i}",
            status="shortlisted",
            fit_score=0.8,
            comp_score=0.7,
            location_score=0.3 + i * 0.01,
        )
    for i in range(15):
        _post(
            conn,
            jid=f"d{i}",
            status="dismissed",
            dismiss_reason="weak fit" if i % 2 else "location",
            fit_score=0.2,
            comp_score=0.4,
            location_score=0.8 - i * 0.01,
        )
    from radar.config import config_path

    before = config_path().read_text()
    r = run_calibration(conn, tmp_project, out_path=tmp_path / "CALIBRATION.md")
    assert r["enough_signal"] and r["labeled"] == 30 and r["positives"] == 15
    pw = r["proposed_weights"]
    assert abs(sum(pw.values()) - 1) < 1e-6
    assert (
        pw["fit_score"] > r["current_weights"]["fit_score"]
    )  # fit is what separated keeps from dismissals
    assert (
        pw["location_score"] <= r["current_weights"]["location_score"]
    )  # negative direction is clamped, never flipped
    md = (tmp_path / "CALIBRATION.md").read_text()
    assert "Proposals only" in md and "weak fit" in md and "| fit_score |" in md
    assert config_path().read_text() == before
    assert db.scalar(conn, "SELECT applied FROM calibration_runs") == 0
    coefs = fit_logistic([[0.0], [1.0], [0.1], [0.9]], [0, 1, 0, 1])
    assert coefs[0] > 0


def test_public_export_scrubs_personal_data(tmp_project, tmp_path):
    from radar.ops.public_export import export_public, forbidden_terms

    dest = tmp_path / "public"
    r = export_public(tmp_project, dest)
    assert r["remaining_hits"] == [] and r["files"] > 50
    cfg = (dest / "config" / "example.config.yaml").read_text()
    assert "you@example.com" in cfg and "Baseline Employer" in cfg and "dream_list: []" in cfg
    terms = forbidden_terms(tmp_project)
    for path in dest.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".yaml", ".py", ".yml", ".toml", ".txt"):
            text = path.read_text(errors="ignore")
            for t in terms:
                assert t.lower() not in text.lower(), f"{t!r} survived in {path.relative_to(dest)}"
    assert not (dest / "data" / "radar.db").exists() and not (dest / "resume.pdf").exists()
    assert not (dest / ".git").exists()
    with pytest.raises(FileExistsError):
        export_public(tmp_project, dest)


def test_alarms_and_push_once_per_day(tmp_project, conn):
    from radar.notify.channels import Channel, Payload
    from radar.ops import alarms

    class Fake(Channel):
        name = "telegram"

        def __init__(self):
            self.sent = []

        def available(self):
            return True

        def send(self, p: Payload):
            self.sent.append(p)
            return "1"

    src_id = db.scalar(conn, "SELECT id FROM company_sources LIMIT 1")
    conn.execute(
        "UPDATE company_sources SET drift_note = '3 rows vs typical 400', last_drift_at = ? WHERE id = ?",
        (utcnow_iso(), src_id),
    )
    conn.commit()
    al = alarms.evaluate(conn, tmp_project)
    keys = {a.key for a in al}
    assert "drift" in keys and "backup_stale" in keys
    ch = Fake()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("radar.ops.alarms.in_quiet_hours", lambda cfg: False)
        assert alarms.push(conn, tmp_project, al, channels=[ch]) == len(al)
        assert alarms.push(conn, tmp_project, al, channels=[ch]) == 0  # once per day
    assert any("drifted" in p.title for p in ch.sent)
    assert db.scalar(conn, "SELECT COUNT(*) FROM notifications WHERE tier = 'system'") == len(al)


def test_single_instance_lock(tmp_project):
    from radar.ops.launchd import single_instance

    with single_instance(tmp_project, "cycle") as mine:
        assert mine
        with single_instance(tmp_project, "cycle") as again:
            assert not again
    with single_instance(tmp_project, "cycle") as after:
        assert after


def test_launchd_plists_and_systemd(tmp_project):
    from radar.ops.launchd import plists, systemd_units

    pl = plists(tmp_project, port=8787, interval_s=900)
    cyc = pl["com.jobradar.cycle"]
    assert cyc["StartInterval"] == 900 and cyc["ProgramArguments"][1:] == ["cycle", "--quiet"]
    assert pl["com.jobradar.nightly"]["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert pl["com.jobradar.serve"]["KeepAlive"] is True
    assert (
        "TELEGRAM_BOT_TOKEN" not in cyc["EnvironmentVariables"]
        or cyc["EnvironmentVariables"]["TELEGRAM_BOT_TOKEN"]
    )
    units = systemd_units(tmp_project)
    assert "Persistent=true" in units["jobradar-cycle.timer"]


def test_cycle_report_skips_when_locked(tmp_project, conn):
    from radar.ops.launchd import single_instance
    from radar.scheduler import run_cycle_sync

    with single_instance(tmp_project, "cycle"):
        rep = run_cycle_sync(conn, tmp_project)
    assert rep.skipped and "lock held" in rep.notes[0]


def test_kit_render_is_grounded(tmp_project):
    from radar.ops.kits import KIT_SCHEMA, render_kit

    out = {
        "resume_bullets_ordered": [
            {"bullet": "Migrated batch app to .NET 10", "why_first": "matches .NET stack"}
        ],
        "why_this_firm": "Because X.",
        "facts_used": ["X"],
        "referral_message": "Hi — saw the New Grad SWE role…",
        "interview_themes": [{"theme": "t1", "why": "w", "prep": "p"}] * 3,
        "honest_risks": [".NET vs Go"],
    }
    md = render_kit(
        out, {"company_name": "Acme", "title": "SWE", "apply_url": "https://x/1"}, "sonnet"
    )
    assert "ever sent automatically" in md and "Honest risks" in md and "https://x/1" in md
    assert KIT_SCHEMA["properties"]["interview_themes"]["minItems"] == 3
    _ = sqlite3
