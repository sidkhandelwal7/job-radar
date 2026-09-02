"""Dedupe: URL identity, similarity, clustering with cannot-link, and the hand-labeled pair set."""

from pathlib import Path

import yaml

from radar import db
from radar.dedupe.cluster import run_clustering
from radar.dedupe.similarity import pair_score, url_key

FIX = Path(__file__).parent / "fixtures" / "dedupe_pairs.yaml"


def test_url_key_identities():
    assert url_key("https://jobs.dropbox.com/listing/8107794?gh_jid=8107794") == url_key(
        "https://job-boards.greenhouse.io/dropbox/jobs/8107794"
    )
    assert url_key("https://www.trlm.com/apply/5191098007?gh_jid=5191098007") != url_key(
        "https://www.trlm.com/apply/5191106007?gh_jid=5191106007"
    )
    assert (
        url_key(
            "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c/apply?lever-source=x"
        )
        == "lever:ac978161-6f46-4f6b-ad9e-a258e642751c"
    )
    assert url_key(
        "https://intel.wd1.myworkdayjobs.com/en-us/external/job/US-AZ/x_JR0285289"
    ) == url_key("https://intel.wd1.myworkdayjobs.com/External/job/US-AZ/Package_JR0285289")
    assert url_key("https://lifeattiktok.com/search/7669908897587824949") != url_key(
        "https://lifeattiktok.com/search/7669913085331409205"
    )
    assert url_key("https://jobright.ai/jobs/info/6a7c9f32d77e8156a8e32f9f").startswith("https://")
    assert url_key("https://x.example/jobs/1?utm_source=a&ref=b") == "https://x.example/jobs/1"


def test_pair_score_penalties():
    a = {"title_normalized": "software engineer", "seniority": "senior", "posted_ts": 0}
    b = {"title_normalized": "software engineer", "seniority": "new_grad", "posted_ts": 0}
    assert pair_score(a, b).score < 0.8
    c = {
        "title_normalized": "2026 early career software engineer",
        "seniority": "new_grad",
        "posted_ts": 0,
    }
    d = {
        "title_normalized": "2027 early career software engineer",
        "seniority": "new_grad",
        "posted_ts": 365 * 86400,
    }
    assert pair_score(c, d).score < 0.8


def _load_fixture(conn):
    doc = yaml.safe_load(FIX.read_text())
    with db.transaction(conn):
        # the labeled rows carry registry ids from the run they were sampled from; stub any that
        # the sample registry does not have so the FK holds (clustering keys on the id, not the row)
        for p in doc["postings"]:
            if p.get("company_id"):
                conn.execute(
                    "INSERT OR IGNORE INTO companies (id, slug, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        p["company_id"],
                        f"fixture-{p['company_id']}",
                        p["company_name"],
                        "2026-08-20T00:00:00Z",
                        "2026-08-20T00:00:00Z",
                    ),
                )
        for p in doc["postings"]:
            row = dict(p)
            row.setdefault("last_seen_at", row["first_seen_at"])
            desc = row.pop("description_md", None)
            conn.execute(
                "INSERT INTO postings (id, company_id, company_name, title, title_normalized, primary_metro, role_family, seniority, source, "
                "source_provider, source_slug, source_job_id, apply_url, canonical_url, base_posted_min, base_posted_max, "
                "posted_at, first_seen_at, last_seen_at, delisted_at, description_fetched) VALUES "
                "(:id, :company_id, :company_name, :title, :title_normalized, :primary_metro, :role_family, :seniority, :source, "
                ":source_provider, :source_slug, :source_job_id, :apply_url, :canonical_url, :base_posted_min, :base_posted_max, "
                ":posted_at, :first_seen_at, :last_seen_at, :delisted_at, :description_fetched)",
                row,
            )
            db.upsert_doc(
                conn,
                row["id"],
                title=row["title"],
                company_name=row["company_name"],
                description_md=desc,
            )
    return doc


def test_labeled_pairs_precision_recall(conn):
    doc = _load_fixture(conn)
    run_clustering(conn)
    cluster = {
        r["id"]: r["cluster_id"] for r in db.all_rows(conn, "SELECT id, cluster_id FROM postings")
    }
    tp = fp = fn = tn = 0
    errors = []
    for pair in doc["pairs"]:
        same = cluster[pair["a"]] == cluster[pair["b"]]
        if pair["is_duplicate"] and same:
            tp += 1
        elif pair["is_duplicate"]:
            fn += 1
            errors.append(f"MISSED  {pair['a']}/{pair['b']}: {pair['note']}")
        elif same:
            fp += 1
            errors.append(f"FALSE+  {pair['a']}/{pair['b']}: {pair['note']}")
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    msg = (
        f"precision={precision:.3f} recall={recall:.3f} tp={tp} fp={fp} fn={fn} tn={tn}\n"
        + "\n".join(errors)
    )
    print("\n" + msg)
    assert len(doc["pairs"]) >= 50
    assert precision > 0.95, msg
    assert recall > 0.90, msg


def test_blocking_covers_every_labeled_duplicate(conn):
    """Recall measured BEFORE scoring: does blocking even put each labeled duplicate pair in front
    of the scorer? The old (company, metro, family) exact block is measured alongside the new
    overlapping blocks so the improvement — and any remaining gap — is an honest number."""
    from collections import defaultdict

    from radar.dedupe.cluster import _load, candidate_pairs, ensure_url_keys
    from radar.dedupe.similarity import title_tokens, url_key

    doc = _load_fixture(conn)
    ensure_url_keys(conn)
    rows = _load(conn)
    by_id = {r["id"]: r for r in rows}
    dups = [(p["a"], p["b"]) for p in doc["pairs"] if p["is_duplicate"]]

    # pass-1 identity (URL / ATS id) covers a pair when both sides share a key
    def identity(a, b, *, legacy=False):
        ka = {by_id[a]["url_key"], url_key(by_id[a]["canonical_url"])} - {None}
        kb = {by_id[b]["url_key"], url_key(by_id[b]["canonical_url"])} - {None}
        if legacy:  # the pre-review url_key did not know Workable codes
            ka = {k for k in ka if not k.startswith("workable:")}
            kb = {k for k in kb if not k.startswith("workable:")}
        return bool(ka & kb)

    # old exact blocking
    def old_block(r):
        return (r["company_key"], r["primary_metro"] or "?", r["role_family"] or "?")

    # new overlapping blocking
    by_company = defaultdict(list)
    for r in rows:
        by_company[r["company_key"]].append(r)
    new_cands = set()
    for members in by_company.values():
        toks = {m["id"]: title_tokens(m["title_normalized"] or m["title"]) for m in members}
        for a, b in candidate_pairs(members, toks):
            new_cands.add((min(a["id"], b["id"]), max(a["id"], b["id"])))

    old_hit = new_hit = 0
    missed_new = []
    for a, b in dups:
        if identity(a, b, legacy=True) or old_block(by_id[a]) == old_block(by_id[b]):
            old_hit += 1
        if identity(a, b) or (min(a, b), max(a, b)) in new_cands:
            new_hit += 1
        else:
            missed_new.append((a, b))
    msg = f"blocking coverage of {len(dups)} labeled duplicates: old exact blocks {old_hit}/{len(dups)} · new overlapping blocks {new_hit}/{len(dups)} · missed {missed_new}"
    print("\n" + msg)
    assert new_hit >= old_hit, msg
    assert new_hit == len(dups), msg


def test_canonical_is_company_direct(conn):
    _load_fixture(conn)
    run_clustering(conn)
    rows = db.all_rows(
        conn,
        "SELECT p.source FROM postings p WHERE p.is_cluster_canonical = 1 AND p.cluster_size > 1 "
        "AND EXISTS (SELECT 1 FROM postings q WHERE q.cluster_id = p.cluster_id AND q.source = 'company_direct')",
    )
    assert rows and all(r["source"] == "company_direct" for r in rows)


def test_candidate_generation_is_bounded_for_huge_unknown_metro_companies():
    """2026-08-21 incident: one Workday tenant with 20k unknown-metro rows made the overlapping
    blocks enumerate ~200M pairs; a cycle ran for 8 hours at 76% CPU. Candidate generation must
    stay proportional to token-group sizes, never to company size squared."""
    import time

    from radar.dedupe.cluster import MAX_BLOCK, candidate_pairs
    from radar.dedupe.similarity import title_tokens

    members = []
    for i in range(20_000):
        # 200 distinct titles × 100 copies each, all metro-unknown, like a big Workday tenant
        members.append(
            {
                "id": i,
                "company_name": "BigCo",
                "title": f"Engineer {i % 200}",
                "title_normalized": f"engineer role{i % 200}",
                "primary_metro": None,
                "metros_json": None,
                "seniority": "unknown",
            }
        )
    toks = {m["id"]: title_tokens(m["title_normalized"]) for m in members}
    t = time.monotonic()
    n = sum(1 for _ in candidate_pairs(members, toks))
    elapsed = time.monotonic() - t
    # "engineer" is shared by all 20k → that group is sub-blocked and, still oversize, skipped;
    # "roleN" groups have 100 members each → 200 × C(100,2) = 990,000 pairs at most
    assert n <= 200 * (100 * 99 // 2)
    assert elapsed < 10, f"{elapsed:.1f}s for 20k rows"
    assert MAX_BLOCK == 400
