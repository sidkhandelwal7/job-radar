# DECISIONS.md — judgment calls, with reasons

Every call the spec left open or that deliberately deviates from it, as it was logged during the
build. Numbering is preserved from the working log so the `D##` references in code comments
resolve; gaps are entries that were personal to the operator or superseded. "You" = the operator.
Each entry: what, why, how to change it.

---

## Phase 1 — skeleton, links, tracker

### D2. Undocumented-but-public company APIs are allowed when robots.txt permits and no anti-bot work is needed
**What:** Workday CXS, Oracle HCM, and Amazon's `search.json` are JSON endpoints behind the
company's *own* careers page. They are not "documented" in the Greenhouse sense. They are allowed
when (a) robots.txt permits the path, (b) no auth/cookies/fingerprinting is needed, (c) the client
is polite (rate, ETags, descriptive User-Agent). **Meta is excluded** — its robots.txt prohibits
automated collection outright. Google has no JSON endpoint at all.
**Why:** The spec explicitly wants Workday, which is exactly this category; applying the rule
consistently is more honest than special-casing one vendor.
**Change it:** delete the `workday`/`oracle` adapters from `ADAPTERS` or disable those sources.
Coverage then comes only from the aggregator repos.

### D3. Detail pages are fetched only for titles that could be in scope (`detail_fetch: title_prefilter`)
**What:** Workday and Oracle need a second request per job for the description. One bank had
1,861 open reqs, another 7,427; fetching every description is ~10k requests per full scan. The
default fetches descriptions only for titles whose family is engineering-ish or unknown and whose
seniority is not senior/staff/principal/manager/executive/internship. Every posting still lands in
the master list (title, location, link, dates); only the description is skipped.
**Why:** A "Senior Director of Software Engineering" is out of scope no matter what the description
says. Measured: 100% of in-scope-ish rows (1,332 of 30,512) had descriptions; 2,439 detail requests
instead of ~20,000.
**Change it:** `fetch.detail_fetch: all_new`, then `radar fetch --full`.

### D4. Full vs incremental scans
**What:** Workday/Oracle lists are newest-first. A **full** scan runs at most every few hours per
source and is the only thing allowed to mark postings delisted. In between, **incremental** scans
read the newest items — enough to catch every new req at 15-minute cadence for a few cents of
requests. Greenhouse is one request with ETag support, so it is always "full" and usually a 304.

### D5. Drift guard: a collapsed source never delists
**What:** If a source that typically returns ≥20 rows returns fewer than 20% of its moving average,
the run is flagged `DRIFT`, new rows are still ingested, and delist detection is suppressed.
**Why:** "A source normally returning 400 rows returning 3 is a broken adapter, not a quiet
market." Mass-delisting on a broken scrape would poison time-to-close learning.

### D6. "Live" has two strengths, and the UI says which
`url_status = live` with method `source_presence` means "this job was in the company's own feed N
minutes ago" → *listed at source 12 min ago*. Method `api|http` means we asked the ATS for that job
id or fetched the page → *verified live 12 min ago*. Presence in the feed is stronger evidence than a
HEAD 200, but "verified" should mean we checked *that link*. Both are shown honestly.

### D8. Title → family rules: tech words beat generic level words
Rule order: explicit "software engineer/developer/SWE/SDE" → quant → IT support/QA (step-down) →
product/design/hardware/security/data/ML/SRE/solutions → *strong* non-tech words (marketing,
teller, counsel, …) → the generic `engineer(ing)`/`developer` catch-all → *weak* level words
(officer, lead, manager, analyst, associate, …). So "SW Engineer I – Full Stack, Officer" is an
engineer and "Technical Product Marketing Engineer" is not.
**Change it:** `config/title_rules.yaml`, then `radar rescore --replay`.

### D9. Primary metro = the operator's best office on a multi-office req
When a req lists several offices, `primary_metro` is the one you would pick (a fixed preference
order over premium buckets). All metros are kept in `metros_json`. Ranking on the worst office
would bury reqs you would happily take in the best one.

### D12. Duplicate guard checks four things
Same posting · same cluster · repost chain (either direction) · identical normalized apply URL
(tracking params stripped). `--force` overrides; the reason is always shown.

### D13. Raw payloads are gzipped files on disk, not BLOBs
**What:** `data/raw/<provider>/<slug>/<date>/<time>_<kind>_<sha8>.json.gz`, sha256 in
`raw_payloads`. Identical payloads point at the existing file (`unchanged=1`). Multi-page fetches
are stored as one document with each page body verbatim.
**Why:** Keeps the SQLite file small and backups fast; the payload store is append-only forever and
can be replayed with `radar rescore --replay` (verified: zero network calls, applications and
workflow columns untouched).

### D14. `radar rescore` is score-only by default; `--replay` re-parses raw
Replaying 30k postings through markdownify + normalizers takes ~85 s. Weight/config edits don't need
it. Rule edits (`title_rules.yaml`, `metros.yaml`) do.

### D15. Workday caps search results at 2,000
Several tenants report exactly 2,000. Facet partitioning recovers the full list where it matters;
otherwise the 2,000 newest are ingested, which is the part a 15-minute watch needs. Their full scans
are marked incomplete, so nothing is delisted by absence from the top 2,000 (D25).

### D18. Equity haircut
Private-company equity counts at **50%** of annualized grant value; public RSUs at 100% of the
annual vest; equity never feeds `base_est`. Reasons: illiquidity, preference stacks, 4-year vest /
1-year cliff vs a cash baseline received in year one, and the real share of Series B+ new-grad grants
that end up illiquid for 5+ years. `config/comp_priors.yaml`.

---

## Phase 2 — breadth and the always-on fetch layer

### D19. Simplify's `listings.json` is the primary aggregator feed; README tables are parsed for the others
Structured ids, degrees, sponsorship and `active` flags beat regex over markdown. The markdown
parser handles three table dialects and treats 🔒/"Closed" rows as absent. Rows whose link goes to
the aggregator's own site are `source = third_party` and lose to company-direct rows in clustering.

### D20. Aggregator rows resolve their company against the registry; unknown employers queue for discovery
Names that match nothing land in `discovery_queue`; `radar discover` detects the ATS from the row's
URL, probes the board once, and appends to `config/companies.discovered.yaml` (tier 3). First pass:
1,644 queued → 934 boards added, 641 unresolvable (still covered via the aggregator rows).
Registry: 52 → 1,092.

### D21. Cadence-aware polling; `radar fetch` polls only what is due
15-min sources (dream list, Tier-1, aggregators), hourly (Tier-2), 6 h (long tail). Failed sources
back off exponentially (cadence × 2^failures, capped at a day).

### D22. Clustering: identity first, then fuzzy, under a cannot-link constraint
Pass 1 merges rows whose URL resolves to the same ATS job id (Greenhouse `gh_jid`, Lever/Ashby
uuids, Workday `_R-123` tails, Oracle `/job/123`, SmartRecruiters ids, iCIMS, Workable `/j/<CODE>`,
and a generic "same host + same ≥5-digit id"). Pass 2 scores candidate pairs on title, description,
comp overlap and posting-date distance, with penalties for seniority mismatch and large date gaps.
Merges are applied best-score-first and **a cluster may never contain two different req ids from
the same board or ATS** — that constraint is what stops an aggregator row from transitively gluing
distinct reqs together. Measured on hand-labeled pairs from real data
(`tests/fixtures/dedupe_pairs.yaml`): precision 1.00, recall 1.00. Default views show the canonical
row per cluster (company-direct > aggregator > third-party); siblings are one click away.

### D23. The GitHub Actions job runs on "recruiter hours" — superseded by D52
Kept for history: the first schedule was ≈1,500 jobs/month with a self-computed minute guard. The
review pointed out that (a) 1,500 invocations is already 75% of the free tier before any job runs
long, and (b) the guard estimated its own minutes and so could only ever under-count.

### D24. Seed runs never alert
The first cloud run sees every open posting as "new". Those delta lines carry `seed: true` and the
notification layer skips them; the laptop ingests them only to backdate `first_seen_at`.

### D26. Meta, Google, Microsoft, Amazon, Apple, Netflix have no direct adapter
Meta is excluded on robots grounds (D2). The others expose only undocumented JSON on their own
domains and were deferred rather than shipped unvalidated against the guardrail. All six are in the
registry with `sources: []` so aggregator rows still carry their tier flags, and all six appear in
the aggregator feeds.

---

## Phase 3 — intelligence

### D27. `--bare` is NOT used for the headless LLM CLI — it would bill the API
The spec suggested `--bare` for scheduled runs. Probing showed it restricts auth to API keys — the
subscription login is ignored, so the call either fails or, if a key were present, bills metered
usage. That is the exact outcome the spec forbids. Isolation is achieved instead with
no-session-persistence, no setting sources, strict MCP config, plan permission mode and no tools
(~2.5 s round trip). The wrapper also **refuses to run when an API key is set** and raises if the
resolved model id in the response envelope is ever a premium-tier model. Requirements are batched
6 postings per call.

### D28. Cheap-metro COL uplift is capped at +25% of nominal base
The §9b formula deflates expensive metros and, symmetrically, inflates cheap ones: taken literally
a mid-tier job in a cheap city can out-rank a top employer in New York on comp alone.
`location_utility_premium.col_uplift_cap: 0.25` limits the uplift; deflation is never capped.

### D29. Verdict calibration: "materially better tier" means dream list, Tier-1, or target category #1
A mid-ranked category alone is not "materially better" — the baseline employer may itself sit
there. `arguably_better` requires a real stated reason: a location premium, a stronger employer
tier, or effective value at or above parity. The `instant_yes` rule fires only on posted ranges or
confidence ≥ 0.7.

### D30. Out-of-scope rows are not fully scored
Senior/staff/manager/executive seniority, internships, step-down and non-engineering families get
scope fields and `composite = 0`, nothing else. Hard-blocked rows that *look* like new-grad
engineering (clearance, MS/PhD, 3+ YoE, international-only) are scored in full so the Floor
Failures Audit view shows what they would have been. Rescore: ~48 s for 121k rows with cached
embeddings.

### D31. Title years set the graduation window when the description doesn't
"New Grad 2026: Software Engineer" posted in August 2026 is a 2026-start cohort; a May-2027
graduate cannot start before then, so it is a hard blocker. "2027 …" and "2026-2027" pass.
Description-derived windows take precedence.

### D32. Comp cascade and priors
posted (0.90 / 0.92 Ashby / 0.75 text) → same-company recent ranges (0.60) → DOL LCA Level I–II at
that employer (+metro) (0.45) → peer model from our own posted data (0.35 → 0.20 by bucket
specificity) → wide US prior (0.15). New-grad point estimate = 40th percentile of a range (55th for
stretch reqs). LCA: 237k certified computer-occupation filings from one quarter (1.03M rows scanned)
— a prior, never truth.

### D34. P(offer) = 2% + 18% × winnability; EV friction is paid only if you get the offer
EV = P(offer) × (3-year effective delta + career premium) − prep cost − P(offer) × switching
friction. Deliberately simple; every term is in `score_explanation_json`; the monthly calibration
proposes changes to them.

### D36. Workday `endDate` and Oracle `PostingEndDate` are not application deadlines
On live data 218 Workday and 51 Oracle postings "closed tomorrow" — every day. Those fields are
rolling posting-window ends; they are kept in the raw payload but never set `application_deadline`.
Real deadlines come from Greenhouse's explicit field, from the description, and from the learned
per-company time-to-close that drives urgency.

### D37. Unclassifiable titles need an engineering signal to stay in scope
"Facilitator", "Security Guard", "Commercial Real Estate Admin 2" match no title rule. Under pure
recall-at-ingest they stayed in scope and reached the queue. An `unknown` family now stays in scope
only when the description carries tech tags.

### D38. Big text lives in `posting_docs`, not `postings`
Descriptions and explanation JSON made each postings row several KB; every COUNT / GROUP BY over
200k rows became an I/O storm (2–3 s per facet request). Migration 0006 moved them to a 1:1 side
table (FTS external content points there) and materialized the few small values the query language
needs. Filtered views: 80–160 ms on 204k rows.

### D39. A source's first scan never alerts (`company_sources.seed_completed_at`)
The first live dry run wanted to send 1,032 P0s: every one of 408k backfilled rows was "first seen
in the last 6 hours". Rows found by a source's first successful scan are new to *us*, not to the
market. This also covers every company added by weekly discovery later.

### D40. `est_days_to_close == 0` is "older than typical", not "closing now"
The estimate is `median_days_to_close − days_open`, clamped at zero. 1,740 rows sat at zero because
they had outlived the median — that is *uncertainty*, not urgency. The `clearly_better_closing` P0
needs a real deadline within 5 days or `0 < est < 5`.

### D41. Dry runs persist nothing
The first implementation wrote `channel = "file:dry"` rows for dry-run alerts; those then counted
toward the daily caps and the cluster dedupe, so the real run right after a dry run was silently
capped. Dry runs keep cap accounting in memory and print the would-send list.

### D42. launchd `StartInterval` + `StartCalendarInterval`, not cron, not a daemon loop
Both launchd job types fire once on wake for a window the machine slept through — exactly the
"catch up, then one summary" behaviour wanted. A long-running daemon would need its own sleep/wake
handling; cron silently skips. An `flock` in `data/` makes a wake-triggered cycle a no-op (reported,
not silent) when a manual cycle is still running. Linux gets `systemd --user` timers with
`Persistent=true`.

### D43. Backups: full DB nightly at 7+4 retention, plus a tiny workflow export every night
The live DB reached 2.9 GB and gzips to ~530 MB in 60 s, so 14 daily + 8 weekly would be 12 GB of
laptop disk. Everything except what *you* did can be rebuilt from the append-only raw store, so the
nightly also writes a `workflow-*.json.gz` (statuses, dismiss reasons, notes, applications, saved
filters — ~1 KB) keyed by the natural posting key. `radar restore` refuses to roll back applications
without `--force`.

### D44. Time-to-close is learned only from postings with a real `posted_at`
`first_seen_at` understates age for anything found in a seed scan, which would bias the learned
median low and make urgency scream. The weekly snapshot uses `delisted_at − posted_at`, needs five
closures per company, and looks back 180 days.

### D45. Calibration is a six-feature logistic regression in plain Python, proposals only
Revealed preference is tiny (tens of rows a month) — anything fancier would overfit and need numpy
in the core install. Negative coefficients are clamped to zero (a sub-score you dislike should
shrink, not flip sign); proposed = 70% current + 30% implied. Needs ≥ 20 labeled rows with ≥ 5
positives before it says anything.

### D46. Docker files are shipped untested
No Docker on the build machine. The files follow the bare-metal path exactly and disable LLM work.

### D48. Presets never carry `alert: p0`; only filters *you* mark do
The shipped "Dream List" preset was marked `alert: p0`, so the first unattended cycle paged on a
"Software Engineer II". The built-in `dream_list_new_grad` trigger already covers the case the spec
describes; a saved-filter P0 is for filters you write yourself.

### D49. `needs_rescore` flag instead of "scored before last seen"
A full scan refreshes `last_seen_at` on every row it sees, so the old rule re-scored 121k rows in one
catch-up cycle. Fetch now sets `needs_rescore = 1` only on insert, content change, delist, relist
and replay; scoring clears it.

### D50. A cycle is time-boxed; one source can't hold it hostage
The second unattended cycle parked for 20+ minutes doing nothing: a host answered 429 with
`Retry-After: 3600` and the polite client honoured it literally. Now `Retry-After` above 120 s fails
the source fast (it stays due), every source runs under a 600 s timeout, and the fetch stage stops
*starting* sources after `fetch.cycle_budget_seconds` — the rest are "deferred", not failed. Sources
are ordered dream-list → Tier-1 → name, so the boards that matter always run first.

### D51. Dashboard design system: the baseline is the only pure-ink mark
Every screen measures a role against one fixed reference, so the interface draws that reference: a
dashed full-contrast rule at the baseline salary, at the same x in every row (fixed scale; the
floor-to-instant-yes decision band is a faint band, clipped values get a ›). Everything else is
tonal or one of three verdict hues paired with a shape — ▲ filled, ◆ half, ▽ hollow — so the verdict
survives colour-blindness and "worse" never reads as an error. Posted comp is a solid bar; inferred
comp is hatched, prefixed "~", with its confidence printed. Fonts are self-hosted because nothing on
this page may phone home.

### D52. Actions is a backstop at ≈600 invocations/month; the laptop is primary; no self-estimated guard
The arithmetic that matters: GitHub bills **every job rounded up to a whole minute** and private
repos get **2,000 free minutes/month**, so invocations are a floor on minutes. 24/7 every-15-min
= 2,976 jobs → over budget at the theoretical minimum. New schedule, weighted to when a reply
matters: weekdays 13:00–22:59 UTC every 30 min (≈440/month) · weekdays five overnight slots (≈110)
· weekends every 4 h (≈52) · **≈600 total**. The self-estimated "billed minutes" guard is deleted: it
trusted its own underestimate and could fail only after the damage. `radar actions-usage` reports the
raw invocation count (an honest floor) and, with a PAT, the real figure from GitHub's billing API.

### D53. Dedupe blocking is overlapping, not exact — measured before scoring
The old fuzzy pass blocked on exact (company, metro, role family). An aggregator row with unknown
metro or unknown family — the most common duplicate — was therefore never even compared with its
company-direct twin (review finding 9). Blocking is now per company with two overlapping blocks:
rows that share a metro (unknown metro joins every metro block) and rows that share a distinctive
title token; role family is not a blocking key.

Honest numbers on the extended labeled set (68 pairs, 27 duplicates, including six of the
previously missed class pulled from production and four matching negatives): blocking coverage
*before scoring* went from 25/27 to 27/27; of the 27, six were not clustered by the previous code on
the live DB — two never reached the scorer, four reached it and scored 0.60–0.75 because the
aggregator's posted date is months after the employer's (the days-apart penalty). Those four are
now caught by the Workable URL identity, **not** by fuzzy scoring — a non-ATS aggregator copy of an
old req with no shared identity would still be missed. That scoring gap is known and not fixed here.
End-to-end: precision 1.00, recall 1.00, 41 true negatives.

### D54. Link verification: the req id is the identity; a title match is supporting evidence
The HTTP verifier used to call a same-URL 200 `live` when the title appeared in the body, and even
when nothing did ("JS-rendered, treat as live-ish"). Both are soft-404 shaped: every page for a
closed "Software Engineer" req still contains "Software Engineer". Now `live` requires the req id in
the final URL or the body; a same-URL 200 with neither is `unverified`. Expect more `unverified` rows
on JS-rendered boards without an API verifier — that is the honest state.

### D55. robots.txt fails closed
`Robots.allowed` treated any failure to read robots.txt — DNS error, timeout, 401/403/429, 5xx — as
"no rules", i.e. permission (review finding 8). RFC 9309 §2.3.1.4 says the opposite for unreachable
files. Now only a 200 (parsed) or an explicit 404/410 can permit; everything else denies for the run
and is logged.

### D56. Effective value is unit-consistent: after-tax first, then purchasing power, then premium
The old form `COL-adjusted base + premium − tax_delta` applied the COL ratio to pre-tax dollars and
then subtracted a tax delta computed on nominal dollars — two different units summed (review
finding 10). Now: `nominal × (1 − rate_metro) × (COL_baseline / COL_metro, uplift capped) ÷
(1 − rate_baseline) + premium`. Dividing by the baseline's own tax factor keeps the result on the
parity / floor / instant-yes scale, so no gate moves. The old form over-penalised high-tax metros
slightly. Worked examples are generated from the code and pinned in `tests/test_location_value.py`.

### D57. Review findings deliberately deferred (known and accepted)
From the 2026-08-21 adversarial review. Each is real; each is parked on purpose.

- **Historical replay is not bit-exact.** `radar rescore --replay` re-parses the append-only raw
  store with *today's* rules, which is the point, but a replay cannot reproduce what the system
  believed on a past date (score versions and config are recorded per run, not snapshotted with the
  payloads). Accepted: the system optimises for "what should I do today", and `config_versions` +
  `runs.stats_json` keep enough history to explain past verdicts by hand.
- **UTF-8 handling in the raw store.** Payloads are stored as the bytes the server sent and decoded
  as UTF-8 with replacement on replay; a board that serves Latin-1 would lose accented characters
  after a replay. Accepted: every ATS in the registry serves UTF-8 JSON; the drift alarms would
  surface a board that changed encoding; the fix is a one-column migration when needed.
- **Rate-limit bursts at cycle start.** Up to 12 sources start simultaneously and the limiter is per
  host, so a provider whose hosts are many tenants can see a burst of first requests. Accepted:
  observed steady state is ~14 requests per 15-minute cycle across all hosts, `Retry-After` is
  honoured (D50), and the circuit breaker opens after five failures. A provider-level token bucket
  is the fix if a 429 ever shows up.

### D58. The stuck cycle: dedupe candidate generation must be bounded by token groups
Symptom: one `radar cycle` process ran 7 h 48 m at 76% CPU, 3 GB RSS, 188 CPU-minutes; every later
launchd cycle was (correctly) skipped by the lock, so the soak silently stalled for 4 hours. A
`sample` of the process put 97% of time in `set_contains` on int-tuple keys inside the
overlapping-block candidate generator from D53. Its "unknown metro joins every metro block, and
unknown rows pair among themselves" rule is quadratic in unknown-metro rows, and one Workday tenant
has 20,427 of them → ~2×10⁸ pairs for that company alone, ~5×10⁸ across the top five.

Fix: above 60 rows per company, a candidate pair must share a distinctive title token (the scorer
already rejected token-disjoint pairs via MIN_TITLE_OVERLAP, so this costs no recall — measured
27/27 before and after); token groups over 400 are sub-blocked by metro (unknown joins each) then
seniority, and what is still over 400 is skipped and counted (`oversize_blocks_skipped`, logged).
Raising that final cap to 1,500 was measured: 320 s instead of 64 s on 427k rows for zero extra
merges, so it stays at 400. The fuzzy pass is now also time-boxed (600 s, small companies first,
`truncated` flag + error log), and a cycle that finds the lock held by a run older than 2 h raises a
`stuck_cycle` alarm instead of skipping silently.

Ruled out while looking: the server used 1.5 CPU-minutes in 8.9 h — no busy-wait; the
sentence-transformers model is a per-process singleton, loaded once per cycle (≈3 s), not per call.

### D59. Confirmed-dead links leave Today for a "verify before dismissing" group; unverified stays
Only a *confirmed* dead link (404/410, redirect to a generic page, "no longer available") is
demoted — it keeps its rank and the queue shows it in a small group directly under Today with two
taps: **still live, restore** (re-verifies first; if the verifier still says dead, the human's
verdict is recorded and the next sweep can overrule it with evidence) and **closed, dismiss**.
`unverified` is explicitly *not* demoted: after D54 that status includes JS-rendered boards that are
genuinely open.

### D60. Telegram: one update consumer at a time — webhook (Cloudflare Worker) first, long-poll fallback
Telegram delivers a bot's updates to exactly one consumer: while a webhook is set, `getUpdates`
answers 409, and two pollers terminate each other. Long-polling only works while the laptop is
awake. The free fix: a Cloudflare Worker receives the webhook, checks the secret token, writes the
update to KV, answers the tap instantly, and returns 200. The laptop drains the queue every cycle
and applies each tap through the same `apply_action` as a dashboard click; updates are keyed by
`update_id`, so a lost ack cannot double-apply. Every cycle `reconcile` picks the consumer: Worker
healthy → webhook set, listener idles; Worker down → webhook deleted, the listener long-polls.

### D61. Link verification is priority-ordered, not FIFO
Six-day soak: 18,806 rows "unverified >3 days". Diagnosis, in order of blame: (1) the sweep's "due"
rule treated every `source_presence` row as needing an HTTP/API check — 494k of 494k listed rows were
permanently due; (2) the budget was 300 checks every 6 h ≈ 1,200/day (measured: 20 sweeps, 5,777
checks in 6 days — the sweep ran fine, it was just bottomless); (3) FIFO by oldest check meant the
budget went to rows nobody would ever open.

Now the sweep runs every cycle (120 links ≈ 11k/day) over only: application records → Today →
shortlisted → the ranked queue (dream-list/clearly_better first) → rows the source itself has not
confirmed in >3 days (possible ghosts). Everything else's verification IS source presence, which
every fetch refreshes with a timestamp. 500 links you trust beat 18,000 you don't.

### D62. Retention with a standing target-category exemption
Approved: out-of-scope descriptions dropped nightly (the raw store keeps replay whole) and raw
payloads >90 days pruned unless referenced by an in-scope or applied row. Deleting posting rows was
explicitly held — "never delete" stands. A registry audit disables low-yield sources with a persisted
reason and a 14-day re-probe as the undo.

**Standing rule (operator): a company in one of the target categories is exempt from ANY
yield-based pruning or disabling, now and in future policies.** Quiet is not irrelevant — a fintech
with zero current openings exists in the registry precisely for the day it posts one. The first audit
pass violated this and was corrected (44 sources re-enabled) the same day.

Also declined (operator): tightening P1 to posted/confident-comp-only. Comp confidence is a property
of OUR data quality, not the job's, and posted ranges skew toward pay-transparency states — the
filter would silently bias alerts toward CA/NY/CO/WA. P1 stays: any new qualified `clearly_better`
posting (the D63/D64 gates already brought volume from ~11/day to ~2/day).

### D63. Qualification is a gate, not a multiplier
Six-day soak verdict: 17,835 of 20,514 in-scope rows (87%) had `min_years_experience = NULL`, and
NULL passed every gate — a "SWE II, Early Career" ranked #6. Fixes: (1) deterministic years
extraction (`radar/parse/quals.py`) over title+description on every row — patterns for "X+ years",
"minimum/at least X", "X–Y years", "X or more", with neutralizers ("0–2 years", "including
internship experience", "no experience required", "new grads welcome") that must not gate a new
grad out; backfilled over all in-scope rows (12,830 of the nulls had a description; 744 yielded a
number — the rest state no requirement at all). (2) Bands, evaluated before scoring: ≤1 year or
null-with-new-grad-title → eligible · 2 years → stretch (opt-in view; never the queue, never a
notification) · ≥3 years or MS/PhD → suppressed with reason · null without a new-grad title signal →
suppressed as `qualification unknown` — missing data does not default to allowed. The ×0.85 stretch
winnability multiplier is deleted. (3) Title seniority is hard: Senior/Staff/Principal/Lead/Sr./
Manager/Director/Architect/II/III/IV/L4-style levels in the title suppress regardless of the
description ("Early Career" included — the ladder number wins). Effect: in-scope 20,514 → 5,702;
queue 11k → 4,101; the old top-500 split 118 eligible / 292 qualification-unknown / 62 stretch / 24
title-seniority / 4 three-plus-years. Spot-checks show the unknown bucket is real ambiguity (one
fintech asks for "deep experience", no number) — suppressing it is the point. Over-filtering
preferred, per the operator.

### D64. Start-date compatibility is its own hard gate; unenriched rows are never recommended
The #1 queue row (a well-known SaaS company's "Software Engineer, Early Career") said in bold: "This
is NOT a new grad role! … can start full time right away." It passed every YOE check — 0–2 years is
a range the operator qualifies for — because the disqualifier is the immediate-start requirement,
which nothing extracted. Two rules: (1) `start_flag` is extracted deterministically from the
description (disqualifiers: "not a new grad(uate) role", "start immediately/right away", "immediate
start", "currently enrolled students are not eligible", "must start by <date>" before the operator's
earliest start; qualifiers: "Class of 2027", "Summer/Spring/Fall 2027", 2027-dated "university
graduate"), stored on the row, and gated in scope — the BODY beats the title. A compatible signal
also satisfies the null-years new-grad requirement. (2) No unenriched posting enters the
Today/This-week buckets or fires a P0/P1 — `queue_action = needs_review` parks it, enrichment (which
works the queue strictly top-down by rank, as does description fetching) extracts its requirements
and flips `needs_rescore`. The exact sentence is a pinned test.

### D65. Hourly posted ranges were unextractable; a posted range now rescores its row the same cycle
A brokerage's "New Grad 2027 – Software Engineer" req sat at queue rank #2 with `base_est $139,585
(lca_prior, conf 0.45)` while its own fetched description said "Pay Range: $32.19-$53.68/hour". Two
findings, two fixes:

1. **Extraction bug (the root cause).** `_NUM` in `radar/parse/comp.py` only matched comma-grouped
   annuals, k-suffixed figures, and 5–6 plain digits — a plain hourly decimal (`32.19`) matched
   nothing, so the hourly branch (HOURLY context, `annualize`, the $7–$400 sanity band) was dead
   code and hourly ranges were never extracted anywhere. Added a `\d{2,3}(?:\.\d{1,2})?` alternative;
   the existing sanity bands reject prose pairs ("10-15 years", date ranges, "10 - 20%") because a
   sub-$1,000 pair without hourly context is skipped. Pinned test: the exact sentence
   (`tests/test_comp_text.py`), plus guard cases.
2. **Cycle ordering.** Fetch-discovered ranges and html-detail ranges were already rescored in the
   same cycle because scoring runs after both. But LLM enrichment runs *after* scoring and *before*
   notify, so a `posted_range_llm` discovery waited a full cycle while notify ran on the prior-based
   verdict. The scheduler now runs a second `score_all(only_unscored=True)` after enrichment — it
   touches only flagged rows.

Audit of the then-current top 500: **5 rows** scored off a prior with an extractable range in hand —
all five hourly (consistent with the dead path); **0 rows** where a stored posted range was ignored
by scoring (the `needs_rescore` machinery is sound). Applied to the 152 same-signature rows in the
rest of the ranked queue: 106 now floor-fail on real posted comp, 45 verdicts flip. Net: the queue
sheds prior-flattered rows; posted truth beats inference in both directions, which is the principle
"inferred comp never looks like posted comp" doing its job.
