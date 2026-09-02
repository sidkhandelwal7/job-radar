# Architecture

Job Radar is a single-process Python system around one SQLite file, run on a schedule by launchd
(or systemd, or GitHub Actions as a backstop). There is no queue, no daemon and no service mesh:
one command, `radar cycle`, does everything in a fixed order and is time-boxed. This document is
the map; `DECISIONS.md` holds the reasons.

```
   ATS feeds (8 providers)      aggregator repos (5)
            │                          │
            ▼                          ▼
   ┌────────────────────────────────────────────┐
   │ fetch: cadence-aware polling, ETags,       │   raw store: every body gzipped under its
   │ polite per-host limits, robots fail-closed │──▶ sha256, forever (data/raw/…)
   └────────────────────────────────────────────┘
            │ RawJob (provider-agnostic)
            ▼
   parse: titles · locations · posted comp · years · start-date signals   (deterministic)
            │
            ▼
   upsert by natural key (provider, slug, job_id): new / changed / relisted / delisted
            │                                                    events table, never deletes
            ▼
   dedupe: identity (URL → ATS job id) → fuzzy in overlapping blocks → union-find + cannot-link
            │
            ▼
   score: scope & qualification gates → comp cascade → location value → verdict → sub-scores
          → EV with switching cost → urgency → queue action → apply_priority_rank
            │
            ▼
   enrich (LLM, top of queue only, cached) ──▶ rescore only the rows it touched
            │
            ▼
   notify: P0/P1 push · digests · Telegram taps applied · system alarms
```

## Storage

- **One SQLite database**, WAL mode, foreign keys on, FTS5 over title + description. Migrations are
  numbered `.sql` files applied in order and recorded in `schema_migrations`.
- **`postings`** holds one row per (provider, board slug, job id) with every derived column the query
  language and the scorer need. **`posting_docs`** is a 1:1 side table for the big text
  (description, explanation JSON, requirements JSON, application kit). Splitting them took filtered
  facet queries from 2–3 s to 80–160 ms on 200k rows (D38).
- **`raw_payloads`** points at gzipped files on disk (`data/raw/<provider>/<slug>/<date>/…`),
  sha256-addressed; an unchanged payload records `unchanged=1` and reuses the file (D13). The store is
  append-only and is what makes `radar rescore --replay` possible with zero network.
- **`posting_events`** is the audit trail (first_seen, changed, delisted, relisted, repost_detected,
  alerted, applied, …). **`runs`** records every cycle with stats and LLM accounting.
  **`config_versions`** snapshots the config each time it changes. **`kv`** holds watermarks.
- Workflow state the operator owns (status, dismiss reason, notes, applications, saved filters) is
  never written by fetch or score, and is exported nightly as a few kilobytes keyed by natural
  posting key so a rebuilt database can be re-decorated (`radar restore-workflow`).

## Fetch layer

- `radar/fetch/adapters/*` implement `BaseAdapter`: list-page fetch (with pagination or facet
  partitioning for Workday's 2,000-result cap), optional detail fetch, and `parse()` into
  provider-agnostic `RawJob`s. Recorded real payloads under `tests/fixtures/recorded` drive the tests.
- `radar/fetch/registry.py` mirrors `config/companies.yaml` (plus the auto-discovered registry) into
  `companies` / `company_sources`, assigns cadence by tier, and computes what is due.
- `radar/fetch/http.py`: per-host concurrency and spacing, ETag/304 handling, descriptive
  User-Agent, `Retry-After` honoured up to 120 s then the source fails fast (D50), a circuit breaker
  after repeated failures, and a robots.txt check that fails closed (D55).
- Full vs incremental scans (D4): only a full scan may delist; incremental scans read the newest
  pages. A source whose row count collapses below 20% of its moving average is flagged as drift and
  cannot delist (D5). A source's first successful scan never alerts (D39).
- `upsert_jobs` (`radar/fetch/pipeline.py`) compares an adapter-level `raw_hash` first (unchanged
  rows get a presence refresh only) and a `content_hash` second (changed rows re-derive columns,
  emit a `changed` event and set `needs_rescore`). Descriptions are never regressed by a list scan
  that lacks them.
- The cycle stops starting new sources after `fetch.cycle_budget_seconds`; the rest stay due and
  run next cycle, ordered dream list → Tier 1 → name (D50).

## Normalization (`radar/parse`)

- `titles.py`: rule-driven (config/title_rules.yaml) title → role family, subfamily, seniority,
  program type, new-grad signal; tech words beat generic level words (D8).
- `locations.py`: raw location strings → canonical metros with COL index, premium bucket and tax
  jurisdiction (multi-state metros follow the parsed state, D10); primary metro is the operator's
  best office on a multi-office req (D9).
- `comp.py`: posted ranges from pay-transparency prose, annual or hourly, with sanity bands so
  "10–15 years" or a date range never becomes a salary (D65).
- `quals.py`: years-of-experience extraction with neutralizers ("0–2 years", "including internship
  experience", "new grads welcome"), hard title seniority (Senior/Staff/II/L4 …), and start-date
  compatibility from the body (D63, D64).
- `posting.py`: assembles the canonical row and a parse confidence.

## Dedupe (`radar/dedupe`)

Pass 1 merges rows whose URL resolves to the same ATS job id. Pass 2 scores candidate pairs inside
overlapping per-company blocks (shared metro, where unknown metro joins every block, or a shared
distinctive title token) on title, description, comp overlap and posting-date distance, then merges
best-first under a union-find whose cannot-link constraint forbids two different req ids from the
same board in one cluster (D22, D53). Candidate generation is bounded: above 60 rows per company a
pair must share a title token; token groups above 400 are sub-blocked by metro then seniority; what
is still over 400 is skipped and counted (D58). The fuzzy pass is time-boxed; identity merges always
complete. Reposts (same company/title/metro within 90 days of a delist) are linked, not clustered.

## Scoring (`radar/score`)

1. **Scope** (`scope.py`, rules only): seniority, family, employment type, clearance, degree,
   graduation window, international-only, blocked lists; then the qualification gate: ≤1 year or a
   new-grad title → eligible, 2 years → stretch (opt-in view, never the queue), ≥3 years or unknown
   with no new-grad signal → suppressed with a reason; start-date incompatibility from the body is a
   hard blocker.
2. **Comp** (`enrich/comp_model.py`): posted range → same company's recent ranges → DOL LCA prior
   for that employer and metro → peer model from our own posted data → wide prior; every estimate
   carries a source and a confidence and is rendered as an estimate (D32).
3. **Location value** (`location_value.py`): `nominal × (1 − tax_metro) × (COL_baseline / COL_metro,
   uplift capped) ÷ (1 − tax_baseline) + location premium`, all in baseline-metro pre-tax dollars
   (D28, D56). A separate "real terms" figure (no premium) is shown but never ranks.
4. **Verdict**: `clearly_better` (base ≥ instant-yes gate with posted or confident comp, or effective
   value ≥ parity at a materially better employer), `arguably_better` (floor → parity with a stated
   reason), `worse`; withheld when there is no comp signal at all.
5. **Six sub-scores** → composite; **EV** = P(offer) × (3-year delta + career premium) − prep cost
   − P(offer) × switching friction (D34); **urgency** from days open vs the company's learned
   time-to-close and stated deadlines; **queue action** (`apply_today`, `apply_this_week`,
   `get_referral_first`, `blocked_needs_prep`, `watch`, `verify_link`, `needs_review`).
6. Scoring is incremental: fetch sets `needs_rescore` only on insert, change, delist, relist and
   replay; scoring clears it (D49). Out-of-scope rows get scope fields only (D30).

## Enrichment (`radar/enrich`)

Local sentence embeddings (MiniLM on CPU, cached by content hash) supply resume similarity. The
LLM layer (`llm.py`) is one wrapper around a vendor CLI in headless mode: model pinned per task and
validated at startup, refuses API-key billing, caches every response by sha256(model + prompt +
schema), counts calls per resolved model, and returns `None` when disabled so every caller degrades
to deterministic-only. It runs after the free funnel, only on the top of the ranked queue, with a
per-cycle budget: requirement extraction (batched six postings per call) and an evidence-cited
resume gap analysis. The scheduler rescores what enrichment touched before notifying (D65).

## Notifications (`radar/notify`)

Four tiers (P0 push that breaks quiet hours, P1 push held during quiet hours, P2 daily digest, P3
weekly digest). Anti-noise is mechanical: one alert per dedupe cluster ever, per-company and
per-tier daily caps, a cooldown after a P0, no-change reposts never fire, a source's first scan
never fires, unenriched rows never fire (D64), escalating demotion after ignored P1s. Telegram
messages carry inline buttons; taps go through the same `apply_action` as a dashboard click. Exactly
one consumer drains Telegram updates: a Cloudflare Worker webhook when it is healthy, long-polling
otherwise (D60). Dry runs persist nothing (D41).

## Dashboard and API (`radar/serve`, `web/`)

FastAPI on localhost serves the React dashboard: Queue (today / this week / referral-first /
blocked buckets with one-tap actions), Table (virtualized master list with facets, a query
language, saved filters and visible suppression counts), Detail (the full decomposition with
evidence), Applications (kanban), Calendar (the two baseline dates and the switching-cost curve),
Config (edit → preview the top-30 before/after → commit + rescore), Health. The comp waterfall
draws every row against a fixed dollar scale so the baseline rule stacks into one line down the
table; posted comp is solid, inferred comp is hatched with its confidence printed (D51).

## Operations (`radar/ops`, `radar/scheduler.py`)

`radar cycle`: pull cloud deltas → fetch what is due → link sweep (priority-ordered, D61) → slug
discovery → description fetch → score → LLM budget → rescore touched rows → Telegram taps → alerts
→ digests → system alarms. A failed critical subsystem marks the cycle failed and does not advance
the watermark, so the gap stays visible. A cycle that finds the lock held for over two hours raises
a stuck-cycle alarm instead of skipping silently (D58). launchd fires a missed interval once on
wake, which is the catch-up mechanism (D42). Nightly: workflow export, full backup with integrity
check, weekly velocity snapshot (learned time-to-close), monthly calibration proposals, retention
(D62). Alarms: drift, failing Tier-1/2 sources, stale cycle, stale backup, Actions budget, LLM
volume, dead-letters; each pushed at most once a day and always on the Health view.

## Cloud backstop (`.github/workflows/fetch.yml`, `radar/actions_runner.py`)

A sparse GitHub Actions schedule (≈600 invocations a month) polls the Tier-1 boards and aggregator
repos, pre-scores deterministically, pushes P0/P1 with inline buttons, and commits JSONL deltas
that the laptop folds in on its next cycle, backdating `first_seen_at` to the cloud sighting (D52).
It never calls an LLM.

## Invariants the code enforces

Append-only raw store · postings are delisted, never deleted · workflow columns and the applications
table are never written by fetch or score · every filter is a view with a visible suppression count
· every recurring LLM call pins an allow-listed model (a test greps for violations) · nothing is ever
sent or submitted on the operator's behalf · secrets live only in the environment or
`data/secrets.env`.

## Module map

```
radar/
├── cli.py                 typer entry points (`radar …`)
├── config.py              typed config (pydantic), example fallback, model-pinning assertion
├── db.py  migrations/     SQLite access, numbered migrations
├── models.py              SourceSpec, RawJob, CompSnapshot
├── fetch/                 http client, registry, raw store, adapters/, html_detail, pipeline (upsert)
├── parse/                 titles, locations, comp, quals, posting builder
├── dedupe/                similarity (pair score, URL keys), cluster (blocks, union-find, reposts)
├── enrich/                embed (MiniLM), comp_model (cascade), lca (DOL prior), resume (fit), llm, pipeline
├── score/                 scope (gates), location_value (verdict), engine (sub-scores, EV, queue), views
├── notify/                engine (tiers, anti-noise), digest, channels, telegram_actions, telegram_webhook
├── serve/api.py           FastAPI
├── ops/                   launchd, backup, alarms, retention, snapshot, calibrate, kits, public_export
├── scheduler.py           `radar cycle`
├── actions_runner.py / actions_ingest.py   cloud backstop and delta ingestion
├── applications.py  links.py  discover.py  query.py  rescore.py  secrets.py  util.py
tests/                     264 tests; tests/fixtures/recorded are real payloads; dedupe_pairs.yaml is hand-labeled
web/                       React + Vite dashboard
config/                    example.config.yaml, companies.yaml (sample), metros, tax_tables, title_rules, comp_priors, aggregators, presets, quant_firms
deploy/cloudflare/         Telegram webhook Worker
```
