"""R1/R3 retention and the registry audit (D62). R2 is deliberately absent: posting rows are never
deleted by anything in radar.ops.retention."""

import inspect

from radar import db
from radar.ops import retention
from radar.util import utcnow_iso


def _post(conn, jid, **over):
    now = utcnow_iso()
    v = {
        "source": "company_direct",
        "source_provider": "greenhouse",
        "source_slug": "x",
        "source_job_id": jid,
        "apply_url": f"https://x/{jid}",
        "first_seen_at": now,
        "last_seen_at": now,
        "company_name": "Acme",
        "title": "SWE",
        "in_scope": 0,
        "floor_result": "pass",
        "is_cluster_canonical": 1,
        "status": "new",
        "description_md": f"long text {jid} " * 50,
    }
    v.update(over)
    return db.insert_posting(conn, v)


def test_r1_clears_only_untouchable_out_of_scope_descriptions(conn, tmp_project):
    out = _post(conn, "1")
    kept_scope = _post(conn, "2", in_scope=1)
    kept_short = _post(conn, "3", status="shortlisted")
    applied = _post(conn, "4")
    now = utcnow_iso()
    db.insert(
        conn,
        "applications",
        {
            "posting_id": applied,
            "company_name": "Acme",
            "title": "SWE",
            "apply_url": "https://x/4",
            "applied_at": now,
            "stage": "applied",
            "stage_changed_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    n = retention.prune_out_of_scope_descriptions(conn)
    assert n == 1
    got = {
        r["posting_id"]: r["description_md"]
        for r in db.all_rows(conn, "SELECT posting_id, description_md FROM posting_docs")
    }
    assert got[out] is None and got[kept_scope] and got[kept_short] and got[applied]
    # FTS stays consistent via the UPDATE trigger: the cleared row no longer matches its text
    hit = {
        r["id"]
        for r in db.all_rows(
            conn,
            "SELECT p.id FROM postings p WHERE p.id IN (SELECT rowid FROM postings_fts WHERE postings_fts MATCH ?)",
            ('"long text 1"',),
        )
    }
    assert out not in hit and kept_scope in {
        r["id"]
        for r in db.all_rows(
            conn,
            "SELECT p.id FROM postings p WHERE p.id IN (SELECT rowid FROM postings_fts WHERE postings_fts MATCH ?)",
            ('"long text 2"',),
        )
    }
    # no posting row was deleted (R2 held)
    assert db.scalar(conn, "SELECT COUNT(*) FROM postings") == 4
    assert "DELETE FROM postings" not in inspect.getsource(retention).replace(
        "DELETE FROM raw_payloads", ""
    )


def test_r3_prunes_only_old_unreferenced_payloads(conn, tmp_project):
    import os

    raw = tmp_project.raw_dir
    old_file, new_file, kept_file = raw / "a.json.gz", raw / "b.json.gz", raw / "c.json.gz"
    raw.mkdir(parents=True, exist_ok=True)
    for f in (old_file, new_file, kept_file):
        f.write_bytes(b"x" * 100)

    def mk(path, when):
        return db.insert(
            conn,
            "raw_payloads",
            {
                "provider": "greenhouse",
                "slug": "x",
                "url": "u",
                "fetched_at": when,
                "sha256": os.urandom(8).hex(),
                "byte_size": 100,
                "path": str(path),
                "kind": "list",
            },
        )

    mk(old_file, "2026-01-01T00:00:00Z")
    pid_new = mk(new_file, utcnow_iso())
    pid_kept = mk(kept_file, "2026-01-01T00:00:00Z")
    _post(conn, "r", in_scope=1, raw_payload_ref=pid_kept)  # referenced by an in-scope row → kept
    st = retention.prune_raw_payloads(conn, tmp_project)
    assert st["deleted"] == 1
    assert not old_file.exists() and new_file.exists() and kept_file.exists()
    left = {r["id"] for r in db.all_rows(conn, "SELECT id FROM raw_payloads")}
    assert left == {pid_new, pid_kept}


def test_registry_audit_disables_survive_sync_and_reprobe_undoes(conn, tmp_project):
    from radar.fetch.registry import sync_registry

    # a discovered tier-3 source whose only output is out of scope
    cid = db.insert(
        conn,
        "companies",
        {
            "slug": "dud-co",
            "name": "Dud Co",
            "tier": 3,
            "target_category": "other",
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        },
    )
    src_id = db.insert(
        conn,
        "company_sources",
        {
            "company_id": cid,
            "provider": "greenhouse",
            "slug": "dudco",
            "cadence": "6h",
            "enabled": 1,
        },
    )
    _post(conn, "d1", source_slug="dudco", company_id=cid)
    _post(conn, "d2", source_slug="dudco", company_id=cid)
    r = retention.registry_audit(conn, apply=True)
    assert any("Dud Co" in x for x in r["disabled"])
    row = db.one(
        conn, "SELECT enabled, disabled_reason FROM company_sources WHERE id = ?", (src_id,)
    )
    assert row["enabled"] == 0 and row["disabled_reason"] == "low_yield"
    sync_registry(conn, tmp_project)  # YAML sync must NOT resurrect it
    assert db.scalar(conn, "SELECT enabled FROM company_sources WHERE id = ?", (src_id,)) == 0
    # the queue guard: a source that ever fed the queue is never disabled
    cid2 = db.insert(
        conn,
        "companies",
        {
            "slug": "gem-co",
            "name": "Gem Co",
            "tier": 3,
            "target_category": "other",
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        },
    )
    db.insert(
        conn,
        "company_sources",
        {
            "company_id": cid2,
            "provider": "greenhouse",
            "slug": "gemco",
            "cadence": "6h",
            "enabled": 1,
        },
    )
    _post(conn, "g1", source_slug="gemco", company_id=cid2, apply_priority_rank=42)
    r2 = retention.registry_audit(conn, apply=False)
    assert not any("Gem Co" in x for x in r2["disabled"])
    # a target-category company with zero current in-scope rows is NEVER low-yield-disabled
    cid3 = db.insert(
        conn,
        "companies",
        {
            "slug": "quiet-fintech",
            "name": "Quiet Fintech",
            "tier": 3,
            "target_category": "fintech_infrastructure",
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        },
    )
    db.insert(
        conn,
        "company_sources",
        {
            "company_id": cid3,
            "provider": "greenhouse",
            "slug": "quietfintech",
            "cadence": "6h",
            "enabled": 1,
        },
    )
    _post(conn, "q1", source_slug="quietfintech", company_id=cid3)
    assert not any(
        "Quiet Fintech" in x for x in retention.registry_audit(conn, apply=False)["disabled"]
    )
    # the 14-day re-probe undo
    conn.execute(
        "UPDATE company_sources SET disabled_at = '2026-08-01T00:00:00Z' WHERE id = ?", (src_id,)
    )
    conn.commit()
    assert retention.reprobe_disabled(conn) == 1
    assert db.scalar(conn, "SELECT enabled FROM company_sources WHERE id = ?", (src_id,)) == 1
