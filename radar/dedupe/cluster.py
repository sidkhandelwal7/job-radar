"""Clustering (§7): block → score → union-find → canonical. Cluster, never delete.

Pass 1 — identity: two rows with the same `url_key` (normalized URL / ATS job id) are the same
          posting (an aggregator row pointing at the company's own req, or the same req on two
          boards).
Pass 2 — fuzzy: candidate pairs come from OVERLAPPING blocks inside each company — (a) rows that
          share a metro, where a row with unknown metro joins every metro block; (b) rows that share
          a distinctive title token, regardless of metro. Role family is never a blocking key: an
          aggregator copy with unknown metro/family is the most common duplicate and must still be
          compared with its company-direct twin (review finding 9). Pairs whose combined title /
          description / comp / date score ≥ THRESHOLD are merged.

Canonical preference: company_direct > aggregator > third_party; then has description > has comp
> earliest first_seen. Default views show canonical rows only; siblings stay queryable.

Repost detection: same company + normalized title + metro, first seen within REPOST_WINDOW_DAYS
after an older row's delisted_at → repost_of_id (linked, not clustered: a repost is a new req).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from radar import db
from radar.dedupe.similarity import pair_score, title_tokens, url_key
from radar.fetch.registry import normalize_company_name
from radar.util import parse_dt, utcnow_iso

log = logging.getLogger("radar.dedupe")

THRESHOLD = 0.80
MIN_TITLE_OVERLAP = 0.34
REPOST_WINDOW_DAYS = 90
MAX_BLOCK = 400  # larger blocks are sub-blocked by seniority/title tokens
SOURCE_RANK = {"company_direct": 0, "aggregator": 1, "third_party": 2}


class UnionFind:
    """Union-find with a cannot-link constraint: a cluster may never contain two different req ids
    from the same board (provider, slug). That stops transitive merges through an aggregator row
    from gluing distinct reqs together."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.boards: dict[int, dict[tuple[str, str], str]] = {}  # root → {(provider, slug): job_id}

    def add(self, x: int, board: tuple[str, str] | None, job_id: str | None) -> None:
        self.parent.setdefault(x, x)
        if board and board[0] != "github":
            self.boards.setdefault(x, {})[board] = job_id or ""

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def can_union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        ba, bb = self.boards.get(ra, {}), self.boards.get(rb, {})
        return all(bb[k] == v for k, v in ba.items() if k in bb)

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if not self.can_union(a, b):
            return False
        keep, drop = min(ra, rb), max(ra, rb)
        self.parent[drop] = keep
        merged = self.boards.get(keep, {})
        merged.update(self.boards.pop(drop, {}))
        if merged:
            self.boards[keep] = merged
        return True


@dataclass
class ClusterStats:
    postings: int = 0
    url_merges: int = 0
    fuzzy_merges: int = 0
    clusters: int = 0
    multi_clusters: int = 0
    siblings_hidden: int = 0
    reposts_linked: int = 0
    pairs_scored: int = 0
    oversize_blocks_skipped: int = 0
    truncated: bool = False  # fuzzy pass stopped at the deadline
    elapsed_s: float = 0.0
    examples: list[str] = field(default_factory=list)


def _load(conn: sqlite3.Connection) -> list[dict]:
    rows = db.all_rows(
        conn,
        "SELECT p.id, p.company_id, p.company_name, p.title, p.title_normalized, p.primary_metro, p.metros_json, p.role_family, p.seniority, "
        "p.source, p.source_provider, p.source_slug, p.source_job_id, p.apply_url, p.canonical_url, p.url_key, d.description_md, "
        "p.base_posted_min, p.base_posted_max, p.posted_at, p.first_seen_at, p.delisted_at, p.description_fetched "
        "FROM postings p LEFT JOIN posting_docs d ON d.posting_id = p.id",
    )
    out = []
    for r in rows:
        d = dict(r)
        d["posted_ts"] = (
            parse_dt(r["posted_at"] or r["first_seen_at"]) or parse_dt(r["first_seen_at"])
        ).timestamp()
        d["company_key"] = (
            f"id:{r['company_id']}"
            if r["company_id"]
            else f"name:{normalize_company_name(r['company_name'] or '')}"
        )
        out.append(d)
    return out


def ensure_url_keys(conn: sqlite3.Connection) -> int:
    """Fill postings.url_key for rows that lack it (idempotent)."""
    rows = db.all_rows(
        conn, "SELECT id, apply_url, canonical_url FROM postings WHERE url_key IS NULL"
    )
    n = 0
    with db.transaction(conn):
        for r in rows:
            k = url_key(r["apply_url"]) or url_key(r["canonical_url"])
            if k:
                conn.execute("UPDATE postings SET url_key = ? WHERE id = ?", (k, r["id"]))
                n += 1
    return n


def _ats_identity(key: str | None) -> tuple[tuple[str, str], str] | None:
    """'greenhouse:123' → (('ats:greenhouse', 'global'), '123'); 'workday:capitalone:R1' → (('ats:workday', 'capitalone'), 'R1')."""
    if not key or "://" in key:
        return None
    parts = key.split(":")
    if len(parts) == 2:
        return ("ats:" + parts[0], "global"), parts[1]
    if len(parts) == 3:
        return ("ats:" + parts[0], parts[1]), parts[2]
    return None


UNKNOWN_METROS = {None, "", "?", "us_unknown", "multiple", "unknown"}


def _metros(m: dict) -> set[str]:
    out: set[str] = set()
    if m.get("primary_metro") not in UNKNOWN_METROS:
        out.add(m["primary_metro"])
    raw = m.get("metros_json")
    if raw:
        try:
            import json

            out.update(x for x in json.loads(raw) if x and x not in UNKNOWN_METROS)
        except (ValueError, TypeError):
            pass
    return out


SMALL_COMPANY = 60  # below this, every pair is enumerated; above it, pairs must share a title token
FINAL_BLOCK_CAP = 400  # measured: raising this to 1500 cost 5× the time for zero extra merges (D58)
OVERSIZE_LOG: list[str] = []  # token groups too large to pair even after sub-blocking (for stats)


def candidate_pairs(members: list[dict], toks: dict[int, set[str]]):
    """Overlapping blocks within one company (review finding 9) — bounded.

    A pair is a candidate when the rows are metro-compatible (share a metro, or either side's
    metro is unknown) AND, for any company above SMALL_COMPANY rows, share a distinctive title
    token. The token condition costs no recall: the scorer already rejects pairs with no shared
    title tokens (MIN_TITLE_OVERLAP), so enumerating them was pure waste — and quadratic waste: a
    Workday tenant with 20k unknown-metro rows meant 200M tuple lookups and a cycle that never
    ended (2026-08-21). Token groups larger than MAX_BLOCK are sub-blocked by metro (unknown-metro
    rows join every sub-block) and then by seniority; anything still larger is skipped and counted.
    """
    metros = {m["id"]: _metros(m) for m in members}

    def compatible(a: dict, b: dict) -> bool:
        ma, mb = metros[a["id"]], metros[b["id"]]
        return not ma or not mb or bool(ma & mb)

    if len(members) <= SMALL_COMPANY:
        for a, b in combinations(members, 2):
            if compatible(a, b):
                yield a, b
        return

    seen: set[tuple[int, int]] = set()

    def pairs_of(group: list[dict]):
        for a, b in combinations(group, 2):
            if not compatible(a, b):
                continue
            key = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
            if key in seen:
                continue
            seen.add(key)
            yield a, b

    def sub_blocks(group: list[dict]) -> list[list[dict]]:
        """metro sub-blocks (unknown joins each), then seniority; drop what is still oversize."""
        by_metro: dict[str, list[dict]] = defaultdict(list)
        unknown: list[dict] = []
        for m in group:
            ms = metros[m["id"]]
            if not ms:
                unknown.append(m)
            for x in ms:
                by_metro[x].append(m)
        out: list[list[dict]] = []
        cands = [g + unknown for g in by_metro.values()] + ([unknown] if len(unknown) > 1 else [])
        for g in cands:
            if len(g) <= MAX_BLOCK:
                out.append(g)
                continue  # small enough without splitting by seniority
            by_sen: dict[str, list[dict]] = defaultdict(list)
            for m in g:
                by_sen[m["seniority"] or "?"].append(m)
            for g2 in by_sen.values():
                if len(g2) <= FINAL_BLOCK_CAP:
                    out.append(g2)
                elif len(OVERSIZE_LOG) < 50:
                    OVERSIZE_LOG.append(
                        f"{g2[0]['company_name']}: {len(g2)} rows share a token/metro/seniority block — not pairwise-compared"
                    )
        return out

    by_tok: dict[str, list[dict]] = defaultdict(list)
    tokenless: list[dict] = []
    for m in members:
        ts = toks[m["id"]]
        if not ts:
            tokenless.append(m)
        for t in ts:
            by_tok[t].append(m)
    for grp in by_tok.values():
        if len(grp) < 2:
            continue
        groups = [grp] if len(grp) <= MAX_BLOCK else sub_blocks(grp)
        for g in groups:
            yield from pairs_of(g)
    if (
        1 < len(tokenless) <= MAX_BLOCK
    ):  # titles with no distinctive words: compare among themselves
        yield from pairs_of(tokenless)


def _canonical(members: list[dict]) -> dict:
    return min(
        members,
        key=lambda m: (
            SOURCE_RANK.get(m["source"], 3),
            0 if m["description_fetched"] else 1,
            0 if (m["base_posted_min"] or m["base_posted_max"]) else 1,
            m["first_seen_at"] or "",
            m["id"],
        ),
    )


CLUSTER_DEADLINE_S = 600  # the fuzzy pass is time-boxed; identity merges always complete


def run_clustering(
    conn: sqlite3.Connection,
    *,
    embed=None,
    run_id: int | None = None,
    deadline_s: float = CLUSTER_DEADLINE_S,
) -> ClusterStats:
    import time

    t0 = time.monotonic()
    stats = ClusterStats()
    ensure_url_keys(conn)
    rows = _load(conn)
    stats.postings = len(rows)
    by_id = {r["id"]: r for r in rows}
    uf = UnionFind()
    for r in rows:
        uf.add(r["id"], (r["source_provider"], r["source_slug"]), r["source_job_id"])
        # A structured ATS identity parsed from the URL is a req id too: two different Greenhouse
        # ids are two reqs even when both rows came from aggregator repos.
        ident = _ats_identity(r["url_key"])
        if ident:
            uf.add(r["id"], ident[0], ident[1])

    # Pass 1: URL / job-id identity
    by_key: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r["url_key"]:
            by_key[r["url_key"]].append(r["id"])
        ck = url_key(r["canonical_url"])
        if ck and ck != r["url_key"]:
            by_key[ck].append(r["id"])
    for ids in by_key.values():
        ids = sorted(set(ids))
        for other in ids[1:]:
            if uf.find(ids[0]) != uf.find(other) and uf.union(ids[0], other):
                stats.url_merges += 1

    # Pass 2: fuzzy within overlapping blocks — score every candidate pair first, then merge
    # best-first under the cannot-link constraint so an aggregator row attaches to its single best
    # direct match.
    by_company: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_company[r["company_key"]].append(r)
    candidates: list[tuple[float, int, int, str]] = []
    OVERSIZE_LOG.clear()
    for members in sorted(
        by_company.values(), key=len
    ):  # small companies first; the long tail can be cut
        if time.monotonic() - t0 > deadline_s:
            stats.truncated = True
            break
        if len(members) < 2:
            continue
        toks = {m["id"]: title_tokens(m["title_normalized"] or m["title"]) for m in members}
        for a, b in candidate_pairs(members, toks):
            if (
                a["source_provider"] == b["source_provider"] != "github"
                and a["source_slug"] == b["source_slug"]
            ):
                continue  # two reqs on one board are two reqs
            ta, tb = toks[a["id"]], toks[b["id"]]
            if ta and tb and len(ta & tb) / max(1, min(len(ta), len(tb))) < MIN_TITLE_OVERLAP:
                continue
            stats.pairs_scored += 1
            ps = pair_score(a, b, embed=embed)
            if ps.score >= THRESHOLD:
                candidates.append((ps.score, a["id"], b["id"], ps.reason))
    stats.oversize_blocks_skipped = len(OVERSIZE_LOG)
    for line in OVERSIZE_LOG[:5]:
        log.warning("dedupe: %s", line)
    if stats.truncated:
        log.error(
            "dedupe: fuzzy pass hit the %.0f s deadline — largest companies not compared this run (identity merges are complete)",
            deadline_s,
        )
    candidates.sort(key=lambda c: -c[0])
    for score, ia, ib, reason in candidates:
        if uf.find(ia) == uf.find(ib):
            continue
        if uf.union(ia, ib):
            stats.fuzzy_merges += 1
            if len(stats.examples) < 25:
                a, b = by_id[ia], by_id[ib]
                stats.examples.append(
                    f"#{ia} ⇄ #{ib} {a['company_name']}: '{a['title'][:40]}' ~ '{b['title'][:40]}' ({reason}; {score:.2f})"
                )

    # Materialize clusters
    groups: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        groups[uf.find(r["id"])].append(r["id"])
    now = utcnow_iso()
    existing = {
        r["canonical_posting_id"]: r["id"]
        for r in db.all_rows(conn, "SELECT id, canonical_posting_id FROM posting_clusters")
    }
    with db.transaction(conn):
        for ids in groups.values():
            members = [by_id[i] for i in ids]
            canon = _canonical(members)
            size = len(ids)
            cid = existing.get(canon["id"])
            if cid is None:
                # reuse any member's existing cluster id to keep ids stable
                prev = db.one(
                    conn,
                    f"SELECT cluster_id FROM postings WHERE id IN ({','.join('?' * len(ids))}) AND cluster_id IS NOT NULL LIMIT 1",
                    ids,
                )
                if prev and prev["cluster_id"]:
                    cid = prev["cluster_id"]
                    db.update(
                        conn,
                        "posting_clusters",
                        cid,
                        {
                            "canonical_posting_id": canon["id"],
                            "size": size,
                            "updated_at": now,
                            "method": "url+fuzzy",
                        },
                    )
                else:
                    cid = db.insert(
                        conn,
                        "posting_clusters",
                        {
                            "canonical_posting_id": canon["id"],
                            "size": size,
                            "method": "url+fuzzy",
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
            else:
                db.update(conn, "posting_clusters", cid, {"size": size, "updated_at": now})
            for m in members:
                is_canon = int(m["id"] == canon["id"])
                conn.execute(
                    "UPDATE postings SET cluster_id = ?, is_cluster_canonical = ?, cluster_size = ? WHERE id = ?",
                    (cid, is_canon, size, m["id"]),
                )
            stats.clusters += 1
            if size > 1:
                stats.multi_clusters += 1
                stats.siblings_hidden += size - 1
    stats.reposts_linked = link_reposts(conn, rows, run_id=run_id)
    stats.elapsed_s = round(time.monotonic() - t0, 2)
    return stats


def link_reposts(
    conn: sqlite3.Connection, rows: list[dict] | None = None, *, run_id: int | None = None
) -> int:
    """Same company + normalized title + metro, new row first seen ≤ 90 days after an older row delisted."""
    rows = rows or _load(conn)
    by_sig: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_sig[(r["company_key"], r["title_normalized"], r["primary_metro"])].append(r)
    n = 0
    with db.transaction(conn):
        for members in by_sig.values():
            if len(members) < 2:
                continue
            delisted = [m for m in members if m["delisted_at"]]
            if not delisted:
                continue
            for newer in members:
                if newer["delisted_at"] and not any(
                    m["id"] != newer["id"]
                    and m["delisted_at"]
                    and m["delisted_at"] < newer["first_seen_at"]
                    for m in members
                ):
                    continue
                candidates = [
                    m
                    for m in delisted
                    if m["id"] != newer["id"] and m["delisted_at"] <= newer["first_seen_at"]
                ]
                if not candidates:
                    continue
                older = max(candidates, key=lambda m: m["delisted_at"])
                gap = (
                    parse_dt(newer["first_seen_at"]) - parse_dt(older["delisted_at"])
                ).total_seconds() / 86400.0
                if 0 <= gap <= REPOST_WINDOW_DAYS:
                    cur = db.one(
                        conn, "SELECT repost_of_id FROM postings WHERE id = ?", (newer["id"],)
                    )
                    if cur and cur["repost_of_id"] == older["id"]:
                        continue
                    conn.execute(
                        "UPDATE postings SET repost_of_id = ? WHERE id = ?",
                        (older["id"], newer["id"]),
                    )
                    conn.execute(
                        "UPDATE postings SET repost_count = repost_count + 1 WHERE id = ?",
                        (older["id"],),
                    )
                    db.add_event(
                        conn,
                        newer["id"],
                        "repost_detected",
                        {"repost_of": older["id"], "gap_days": round(gap, 1)},
                        run_id,
                    )
                    n += 1
    return n
