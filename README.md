# Job Radar

Job Radar is a job-discovery and ranking system that watched ~630,000 postings from ~1,000
employer feeds and community lists, ran unattended for two weeks, and put the handful worth
applying to on a phone with a one-tap workflow. It keeps every raw payload forever, deduplicates
across sources, and ranks each posting against one standing offer with an itemized,
unit-consistent compensation model. It runs on a laptop that sleeps, with a GitHub Actions job as
the backstop, at $0/month.

This is the public, sanitized copy: the full Python package, the dashboard source, the design log,
and 264 tests over recorded real payloads. Everything personal (the operator's identity, offer,
preferences, resume, credentials, application records) has been replaced with config-driven
placeholders and a synthetic sample profile, so the whole thing runs from a fresh clone. Numbers
below are from the real run.

## My role

- **I wrote the specification:** problem definition, data sources, scoring model, guardrails
  (robots.txt, no metered API calls, no LinkedIn/Indeed scraping), and six phases each with its own
  acceptance criteria. Four of its assumptions did not survive contact with the endpoints before any
  code was written (one dream-list employer was on Oracle Recruiting Cloud, not Workday; a
  15-minute cron cannot fit a private repo's free Actions tier because every job bills as a whole
  minute; Meta's robots.txt forbids automated collection outright; the effective-value formula was
  stated two different ways), and each correction became a numbered decision with evidence.
- **I set the policies the system enforces:** missing data never defaults to allowed; no source in
  a target category may be pruned for low yield; over-filtering beats a queue nobody trusts; comp
  confidence may never gate alerts, because posted ranges skew toward pay-transparency states and
  that would bias alerts geographically.
- **I made the modeling calls:** a three-state verdict rather than a boolean, because the middle
  band is where real decisions live; absolute comp anchors instead of a percentage premium; a
  location premium derived from my own stated indifference point rather than a guessed multiplier.
- **I ran the quality loop.** I caught a top-ranked posting whose description disqualified me in
  bold text, which produced the start-date gate (D64). I caught the system recommending roles
  requiring four years of experience, which produced the qualification gate (D63). I commissioned
  the adversarial audit, then triaged its eleven findings: fixed eight, deferred three with written
  reasons (D57). The two-week soak found the rest: a quadratic blow-up in dedupe candidate
  generation that ran one cycle for 7 h 48 m (D58), a link-verification sweep whose budget was
  "bottomless" (D61), and a compensation parser whose hourly-range path was dead code (D65). Each
  fix is pinned by a test that quotes the exact sentence from the real posting that exposed it.
- **I cut scope:** no direct adapters for the six employers whose careers-site JSON could not be
  validated against the robots guardrail, with coverage coming from the aggregator feeds instead
  (D26); no facet partitioning to lift Workday's 2,000-row cap, because the newest 2,000 is what a
  15-minute watch needs (D25); the self-estimated Actions minute guard deleted rather than tuned
  (D52); and a calibration loop that only proposes weight changes and never applies them (D45).
- **I rejected a proposed retention policy that would have deleted rows**, because append-only
  ingestion was a load-bearing invariant (D62).

Implementation was delegated to a coding agent working from the spec; I owned the architecture,
the policy decisions, and the review loop.

## Agentic architecture

The model is a component with a budget, not the system. Six rules make that true:

- **Two-stage funnel.** Free deterministic rules decide scope and qualification for every row and
  eliminate 96.3% of them before any model call, because model calls are the scarce resource and
  rules are free. The model sees only the top of the ranked queue.
- **Per-task model routing, configured, not hardcoded.** Three pins in config: a cheap model for
  classification and the batched requirement extraction, a stronger one for the resume gap
  analysis and for drafting application kits.
- **Cost safety as an invariant.** Every scheduled invocation must pass an explicit model flag; a
  startup assertion fails the process if a scheduled task resolves to a premium model; a test greps
  the tree for unpinned invocations. Non-interactive mode has no consent prompt, so an unpinned
  recurring call would bill silently, every 15 minutes, forever.
- **Batching and caching.** Six postings per call, twelve calls per cycle, and every response
  cached by a content hash of model, prompt and schema, so a reposted job is never re-analyzed.
- **Graceful degradation.** The entire layer is one flag away from off, and the system stays
  functional without it: scoring, verdicts, the queue and the alerts all run deterministically with
  no model at all.
- **$0/month marginal cost as a hard design constraint, not an outcome.** No metered API keys and
  no paid tier anywhere in the stack: a subscription login for the model, the employers' own free
  endpoints, a private repo's free Actions minutes, and a free-tier Worker for the webhook.

## Architecture in one paragraph

Eight ATS adapters (Greenhouse, Workday, Oracle HCM, Lever, Ashby, Workable, SmartRecruiters,
Recruitee) and five aggregator-repo parsers feed an **append-only** pipeline: every fetched body is
gzipped to disk under its sha256 before anything is parsed, so any rule can be re-applied to
history with zero network calls. Postings are upserted by natural key into SQLite (WAL, FTS5), never
deleted, only marked delisted. A deterministic normalization layer (titles, locations, posted comp,
years-of-experience, start-date signals) feeds a **two-stage funnel**: free rules decide scope and
qualification for every row; the LLM sees only the top of the ranked queue, batched and cached by
content hash. Scoring produces a three-state verdict against the baseline offer, six sub-scores, an
expected value with itemized switching cost, urgency from learned time-to-close, and a queue action.
A notification engine with mechanical anti-noise rules pushes P0/P1 alerts to Telegram with inline
buttons; a daily digest carries the rest. `radar cycle` runs all of it every 15 minutes under
launchd, catches up on wake, and is time-boxed so one bad source cannot hold it hostage. See
[ARCHITECTURE.md](ARCHITECTURE.md).

## The two-stage funnel, with real numbers

The design principle is that model calls are the scarce resource and rules are free, so rules run
first and are made strict enough that the model only sees what could plausibly be applied to.

| Stage | Rows | What decides |
|---|---|---|
| Master list (append-only, never deleted) | 551,000 | every posting from every source |
| In scope | 20,514 (3.7%) | rules: seniority in the title, role family, employment type, clearance, degree, graduation window, international-only, blocked lists |
| **Eliminated before any model call** | **96.3%** | |
| Qualified (D63/D64 gates) | 5,702 | deterministic years-of-experience and start-date extraction; unknown never defaults to allowed |
| Ranked queue | 4,101 | comp floor, verdict, six sub-scores, urgency |
| LLM enrichment | top of the queue only | 12 calls per cycle, 6 postings per call, cached by content hash |

Rescoring 121k eligible rows from stored data takes ~48 s with cached embeddings; a full replay of
every raw payload through today's rules takes ~14 minutes and zero network calls. Filtered API
views return in 80–160 ms over 200k+ rows after big text was split into a 1:1 side table (D38).

Dedupe: identity first (ATS job id parsed from the URL), then fuzzy pairs inside overlapping
per-company blocks, merged best-first under a cannot-link constraint (a cluster may never hold two
different req ids from the same board). Precision 1.00 / recall 1.00 on 68 hand-labeled pairs drawn
from production, including six that the first version missed.

## What a systematic review found

Before running it unattended, I ran a read-only adversarial audit of the codebase and design log
against the spec: assume competent-looking code that may contain confident mistakes; for each
finding give file, line, what breaks, and the minimal fix. It returned eleven numbered findings
ordered by consequence, an audit of every LLM invocation for model pinning (all pinned, no billing
leak), a secrets and SQL-injection check (clean), and a verdict on the test suite: roughly 65–70%
of the 137 tests then present were load-bearing, and the query-language tests only proved that
generated SQL executed. Row-level correctness cases were added to those tests in response.
Findings that changed code:

- **A cycle could report success while parts of it were broken** (findings 1 and 11). One healthy
  source masked every other adapter failing; notification and git-pull errors were discarded with
  `|| true`; the laptop cycle turned subsystem exceptions into notes and advanced its watermark
  anyway. Now a critical subsystem failure marks the cycle failed and keeps the watermark, and the
  Actions job exits non-zero if any source or any delivery fails.
- **A malformed feed looked like a quiet job market** (finding 3). Invalid JSON, or a payload
  missing its `jobs` array, parsed to zero rows and counted as a healthy scan, and a new source had
  no history for the drift guard to compare against. Adapters now raise on malformed payloads.
- **The cloud backstop's budget arithmetic was wrong** (finding 2 → D52). The first schedule was ≈1,500 jobs a
  month with a self-estimated minute guard. The review pointed out that 1,500 invocations is
  already 75% of the free tier before any job runs long, and that a guard that estimates its own
  minutes can only under-count. The job became a sparse backstop (≈600 invocations a month), real
  usage is read from GitHub's billing API, and the self-estimate was deleted.
- **Dedupe blocking silently excluded the most common duplicate** (finding 9 → D53). Blocking on an
  exact (company, metro, role family) key meant an aggregator row with unknown metro or family was
  never even compared with its company-direct twin. Overlapping blocks fixed it: coverage before
  scoring went from 25/27 to 27/27 on the labeled set.
- **Link verification accepted soft-404s** (finding 4 → D54). A same-URL 200 with the job title in the body
  counted as "live", but every page for a closed "Software Engineer" req still says "Software
  Engineer". The req id in the final URL or body is now the identity; a title match only decorates
  the reason.
- **robots.txt failed open** (finding 8 → D55). Any failure to read robots.txt (DNS, timeout, 403,
  5xx) was treated as permission. RFC 9309 says the opposite for unreachable files; now only a 200
  or an explicit 404/410 can permit.
- **The comp formula mixed units** (finding 10 → D56). It applied the cost-of-living ratio to
  pre-tax dollars and then subtracted a tax delta computed on nominal dollars. The reformulation is
  after-tax first, then purchasing power, then the location premium, all re-expressed on the
  baseline's pre-tax scale so no gate had to move. Worked examples are generated from the code.

Deferred on purpose, with the reasoning written down (D57): historical replay is not bit-exact
(rule edits apply retroactively, which is the point, but a past date's verdict cannot be reproduced);
a board that served Latin-1 would lose accents on replay; the per-host rate limiter can burst at
cycle start across a provider's many tenants.

One finding had a sequel. The overlapping blocks that fixed finding 9 were quadratic in
unknown-metro rows, and one Workday tenant had 20,427 of them; the first unattended week produced
a cycle that ran for 7 h 48 m before the soak, not the review, caught it (D58). The review's
closing list of three things to fix before running unattended (partial failures must fail the job,
reject malformed responses, replace the estimated Actions budget) and its one architectural
endorsement (raw evidence is kept strictly separate from operator workflow state) both held up.

## Honest engineering tradeoffs

- **Laptop-primary, cloud-backstop.** Detection is 15 minutes while the lid is open and on-wake
  otherwise; the Actions job fills the gap every 30 minutes on weekday business hours and every 3–4
  hours the rest of the time, because that is what a private repo's free tier affords. Actions cron
  also routinely fires 5–15 minutes late.
- **One SQLite file.** Simple, replayable, and it reached 4 GB in two weeks. The answer was a
  retention policy that drops out-of-scope descriptions nightly and prunes raw payloads older than
  90 days unless referenced (D62), not a database migration. Posting rows are never deleted.
- **Replay is not bit-exact** (D57). Score versions and config are recorded per run, not snapshotted
  with payloads. The system optimizes for "what should I do today".
- **The LLM layer shells out to a vendor CLI on a subscription login.** No API keys, no metered
  billing, models pinned per task and asserted at startup, a test greps for unpinned invocations, and
  everything degrades to deterministic-only when the CLI is absent. The coupling is deliberate and
  narrow: one wrapper module.
- **A known dedupe gap.** A non-ATS aggregator copy of an old req with no shared identity can still
  be missed, because the aggregator's posted date is months after the employer's and the date
  penalty wins. Documented, not fixed.
- **Over-filtering is the policy.** A posting with no stated experience requirement and no new-grad
  signal is suppressed as "qualification unknown" (D63). Spot checks showed the bucket is real
  ambiguity, and a missed posting costs less than a queue nobody trusts.
- **The quadratic fix caps what it compares.** Above 400 rows sharing a title token, metro and
  seniority, pairs are skipped and counted (D58). Raising the cap to 1,500 cost 5× the time for zero
  extra merges, so it stays.
- **One untested surface.** The Docker files follow the bare-metal path exactly but were never run
  (no Docker on the build machine). The dashboard builds cleanly with `npm run build`; the bundle is
  simply not committed.
- **Comp inference is labeled, never hidden**, and alerts are not gated on comp confidence:
  posted ranges skew toward pay-transparency states, so such a gate would quietly bias alerts by
  geography (D62).

## Run it

Requirements: macOS or Linux, `curl`, network. No Node or Docker needed for the CLI.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv manages Python 3.12
git clone <this-repo> job-radar && cd job-radar
uv python install 3.12
uv venv --python 3.12 && uv pip install -e ".[dev,serve,lca,ml]"

cp config/example.config.yaml config/config.yaml           # optional: edit your profile and baseline
.venv/bin/radar init                                       # creates data/radar.db, applies migrations, syncs the registry
.venv/bin/radar fetch -c stripe -c brex -c ramp            # three real boards; `radar fetch` polls everything that is due
.venv/bin/radar rescore                                    # score against the baseline in config
.venv/bin/radar filter 'seniority:new_grad family:swe'
.venv/bin/radar show <id>                                  # the full decomposition for one posting
.venv/bin/radar serve                                      # API + dashboard on 127.0.0.1:8787 (build web/ first, see below)
.venv/bin/pytest -q                                        # 264 tests, recorded fixtures, no network
```

Without a `config/config.yaml` the tool runs on `config/example.config.yaml` and the synthetic
resume in `examples/`. Everything it writes lives in `data/` (git-ignored).

LLM enrichment is optional. It runs only if the vendor CLI is on `PATH` and logged in; set
`RADAR_LLM_ENABLED=0` to force deterministic-only scoring. Notification channels are environment
variables or `data/secrets.env` only; with nothing configured, alerts go to `data/notifications.log`
and the dashboard.

Dashboard: `cd web && npm ci && npm run build` (Node 20+), then `radar serve`. Unattended operation
on macOS: `radar install-launchd`; on Linux it prints `systemd --user` units.

## What is and is not in this repository

In: the full Python package, the React dashboard source, the test suite with recorded fixtures,
the config tables (metros, tax rates, title rules, comp priors, aggregators), a sample registry of
public boards, the Cloudflare Worker for the Telegram webhook, and the GitHub Actions backstop job.

Not in: the original spec and phase plan (personal), the operator's config, resume, data, backups,
application records, and the auto-discovered registry. Three recorded aggregator fixtures had a
handful of employer names replaced with placeholders; nothing else in the fixtures was edited.

## Where to look first

- `radar/dedupe/cluster.py` — overlapping blocks, union-find with a cannot-link constraint, and the
  bounded candidate generator that ended the 7-hour cycle.
- `radar/score/scope.py` with `radar/parse/quals.py` — the qualification gate: title seniority is
  hard, the body beats the title on start dates, and missing data never defaults to allowed.
- `radar/score/location_value.py` — the unit-consistent effective value and the three-state verdict.
- `radar/scheduler.py` — cycle ordering, time-boxing, catch-up on wake, the post-enrichment rescore,
  and the stuck-cycle watchdog.
- `radar/fetch/pipeline.py` (`upsert_jobs`) with `radar/fetch/raw_store.py` — append-only ingestion:
  raw hash vs content hash, `needs_rescore`, delist and relist semantics.

`DECISIONS.md` is the design log; `ARCHITECTURE.md` is the map.
