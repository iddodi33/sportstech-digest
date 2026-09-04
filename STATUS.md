# STATUS.md — sportstech-digest

*Last updated: 2026-09-04*

Rolling log of changes and open issues. Most recent session first.

---

## Session 2026-09-04 — Google Alerts audit, then source-discovery calibration

### The audit

`scripts/audit_alerts_vs_hub.py` scored 1285 Google Alerts (2026-06-01 → 2026-09-03) through
production's own `score_articles()` and diffed the result against `news_items`. Cost $3.26
actual against a $3.32–3.78 pre-counted estimate. Output in `scripts/data/`:
`alerts_scored.csv` (all 1285), `alerts_missing_from_hub.csv` (the gap list),
`alerts_snippets.json` (fetch checkpoint — both scripts resume from these).

Result: 42 alerts scored 4-5, of which **15 unique stories never reached `news_items`**.

### Root cause

`daily_monitor.py` imports **only** `GOOGLE_NEWS_FEEDS`. It never reads `SITE_RSS_FEEDS`,
never applies `_cap_for`, never applies the keyword filters. The only path that reads
`SITE_RSS_FEEDS` is `news_pipeline.py` + `digest.py`, which runs solely from `monthly.yml` —
**whose cron was retired 2026-07-24**. So from 2026-07-24 the site-RSS half of news discovery
ran nowhere at all. Three of the 15 misses (thinkbusiness.ie, irishtechnews.ie,
businessplus.ie) were on feeds that were already configured and simply not being read.

A query-term theory was tested and rejected: 14 of the 15 contain "Ireland"/"Irish", so the
forced `+ireland` token is not the cause. The rest is Google News ranking depth for small
regional titles — fixed by direct feeds, not by broader query terms.

### Changes

1. `monthly.yml` — cron restored, **weekly** `0 7 * * 0` (not daily; this is a measurement,
   not a doubling of the scoring bill). Sunday 07:00 UTC clears the Fri 06:00 jobs/events
   runs and the 09:00 daily monitor.
2. `news_pipeline.py` — new `REGIONAL_RSS_FEEDS` (six Irish regional/niche feeds, all
   verified parsing with dated entries 2026-09-04), `REGIONAL_SOURCES` tier, and
   `CAP_REGIONAL = 12`. Kept as its own list, not merged into `SITE_RSS_FEEDS`, so the daily
   and monthly paths can diverge later. Also added to `news_pipeline`'s own fetch loop.
3. `daily_monitor.py` — site-RSS loop over `REGIONAL_RSS_FEEDS` alongside the Google News
   loop. Cap applied **after** the date filter in feed order (feeds are reverse-chronological,
   so capping first would discard the newest items). 20s socket timeout per feed —
   `feedparser.parse` has none of its own and would otherwise hang the daily cron.

Deliberately NOT done: the broadsheet and tech-news title filters were not ported into
`daily_monitor`. Tested against the gap list, the sport-keyword set kills 8 of the 15 and the
broadsheet set kills 5 — inheriting either would defeat the change. `daily_monitor` was also
not merged onto `news_pipeline`'s fetch path (incompatible time semantics: 72h vs 35/40 days,
plus a 300s timeout and cloudscraper paths that don't belong in a daily run).

### Measured (`scripts/verify_discovery_coverage.py`, discovery-only, no Anthropic calls)

| | before | after |
|---|---|---|
| feed entries seen (pre-window) | 1,882 | **1,991** |
| articles in 72h window | 31 | **83** |
| from Google News | 31 | 31 |
| from regional RSS | 0 | **52** |
| gap domains reached by daily path | 2/15 | **7/15** |

Both halves measured in a single run, so the Google News side is the same 31 in each
column rather than two samples taken minutes apart — these feeds are volatile enough that
that matters. Entry counts split 1,991 total minus 109 regional = 1,882 Google News.
Note the ratio: Google News yields 31 in-window articles from 1,882 entries (1.6%),
the regional feeds 52 from 109 (48%), because Google News feeds carry months of history
while these regional feeds carry days.

`CAP_REGIONAL = 12` binds on 3 of 6 feeds (offalyexpress, mayonews, dundalkdemocrat all at
cap). Uncapped those three would contribute 77 instead of 36. Tune from this figure.

Gap-list coverage, structurally: **10/15** — 7 via the new regional feeds, 3 via the
re-enabled monthly path. The runtime check shows 7/15 for the daily path alone, because
setantacollege published nothing inside the 72h window that run and the 3 site_rss domains
are monthly-only. 0/15 gap URLs are still live in-feed, as expected: RSS feeds carry 10-30
recent items, not an archive, so June-August stories cannot be recovered from any feed.

### Instrumentation added (same session, follow-up pass)

New `run_telemetry.py` (repo root, imported by `daily_monitor`). Two append-only JSONL
logs in `scripts/data/`, both failure-tolerant — instrumentation never breaks a run:

- **`daily_monitor_usage.jsonl`** — real billed usage read off `response.usage` for both
  billed calls per run. `score_articles` records per-batch input/output tokens plus run
  totals; `deduplicate_by_story` is recorded separately because it is a second billed call
  whose prompt grows with the number of surviving articles, so widening discovery grows it.
  Each record stores the per-MTok rates and `pricing_verified_on` alongside the numbers, so
  a later reader can tell which pricing was applied instead of guessing.
- **`regional_cap_drops.jsonl`** — every item `CAP_REGIONAL` truncates: feed, source label,
  title, pubDate, link, run timestamp. First real run logged **41 drops** (Offaly Live 13,
  Mayo Live 18, Louth Live 10). The cap truncates by recency, not relevance, so this is the
  evidence needed before tuning. `CAP_REGIONAL` deliberately unchanged.
- **`regional_feed_stats.jsonl`** — one record per regional feed per run, including feeds
  that yielded zero: `status` (ok / zero_entries / error), entries fetched, entries
  in-window, kept after cap. The cap-drop log is the numerator; this is the denominator.
  It also separates a feed that timed out from one that simply published nothing — those
  are otherwise identical silence in the telemetry, and `ilovelimerick.ie` already hit the
  10s `_SOCKET_TIMEOUT` once during the `news_pipeline` measurement run.

**Known limitation — regional feeds have no scrape fallback.** In
`news_pipeline.fetch_feed`, both the lxml path and the HTML scrape are gated on
`SCRAPE_FALLBACK` membership (line ~824), which holds only `thinkbusiness.ie` and
`sportireland.ie`. Confirmed against the file: none of the six regional domains is in it,
so a regional feed returning zero entries gets no fallback and contributes nothing that
run. Not fixed — `regional_feed_stats.jsonl` will show how often it actually happens.

`_REGIONAL_SOCKET_TIMEOUT = 20` in `daily_monitor` was removed in favour of importing
`news_pipeline._SOCKET_TIMEOUT` (10s) — one value for one concern. Measured: all six
regional feeds complete in 4.8s total, slowest ~1.6s, so the shared constant did not need
raising. **Qualified later the same day:** `ilovelimerick.ie` hit that 10s timeout during
the `news_pipeline` measurement run, so 10s is tighter than the isolated measurement
suggested. Left unchanged — the failure is graceful and `regional_feed_stats.jsonl` now
records `status: error` per feed, so the real frequency is measurable before acting.

**Cost circuit breaker** (`RUN_COST_CEILING_USD = 2.25`, `daily_monitor.py`; renamed
from `DAILY_COST_CEILING_USD` later the same session — see the shared-module section). An abort,
never a prompt — a 09:00 unattended cron has nobody to confirm, and an unattended job is
exactly where a runaway spend goes unnoticed. Metering lives in `_call_claude_with_retry`,
so every billed response is counted **including retries**: that helper can issue up to 3
requests for one logical call, and a breaker blind to retries would miss the failure mode
it exists to catch. Checked against accumulated `response.usage` after every batch.

On trip: scoring stops, but everything already scored is **kept and still processed** —
dedup, Supabase upserts and emails all run, since that spend is incurred either way.
The principle: the ceiling stops the run **expanding** its spend (no further scoring
batches); it does not abandon the **completion** path for work already paid for. Dedup is
completion — skipping it would let duplicate stories reach both the emails and the
Supabase rows, and those rows persist long after the run ends.

That exception is bounded and measured, not asserted: before the dedup call its real
prompt is priced with Anthropic's free token counter, worst-case output is taken as
`_DEDUP_MAX_TOKENS` (the API cannot bill more), and it proceeds only if accumulated +
projected stays under `RUN_COST_CEILING_USD + DEDUP_COMPLETION_ALLOWANCE_USD` ($0.25,
so a $2.50 hard stop). Past that, dedup is skipped and the reason logged. If the token
count itself fails, dedup proceeds when the breaker has not tripped and is refused when it
has — never spend unmeasured on the emergency path.

**Alert on trip:** a non-zero exit turns the Actions run red, but a red scheduled job can
sit unnoticed for days, and this failure mode repeats — whatever tripped the ceiling trips
it again at the same point next run, the backlog never clears, and the same articles are
re-scored daily. `send_cost_abort_alert()` sends one email per aborted run via the existing
`email_client` path, carrying accumulated cost, batches completed vs planned, articles
scored vs in-window, request count and token totals. Sent immediately after scoring so
exactly one goes out regardless of which of `run()`'s return paths follows. A failure to
send is logged and never masks the abort — the non-zero exit stands either way.

An `cost_ceiling_abort` record is written to
`daily_monitor_usage.jsonl` (with `aborted: true`, batches planned vs completed, and the
request count) so an aborted run is distinguishable from a merely short one, and `run()`
returns False so `__main__` exits non-zero and the workflow shows red.

Threshold basis: no observed daily run existed when this was set, so it is derived from the
real billed usage of the audit run (1285 articles, 169,239 in / 183,376 out, $3.2584 actual
— same MODEL, rubric and BATCH_SIZE) scaled to the measured 83-article daily volume:
~$0.213 for `score_articles` plus ~$0.009 for dedup ≈ **$0.22/run**, ceiling at ~10x.
**Provisional** — retune from `daily_monitor_usage.jsonl` after a week of real runs.
Verified by stub, both branches: a trip just over the ceiling ($2.3130) runs dedup as
completion and finishes at $2.3202 with 4 billed requests; a trip far over ($2.7180) skips
dedup because projected would breach the $2.50 hard stop. Both keep and process all 45
already-scored articles, send exactly one alert email, and exit 1 (normal path exits 0).

**Persistence:** the logs are committed back by `daily_monitor.yml`'s existing
"Commit seen URLs" step (renamed, `git pull --rebase --autostash` added before staging).
A runner's filesystem is discarded at job end, so a local append alone would have produced
nothing from scheduled runs. Chosen over a Supabase table — `supabase_client` is already
authenticated in `daily_monitor`, but a new table plus RLS plus a migration is not
proportionate for two low-volume append-only logs meant to be read by a human in a week —
and over workflow artifacts, which expire and cannot accumulate across runs.

### Shared cost module — `claude_budget.py` (both pipelines now guarded)

Restoring `monthly.yml`'s cron put a second unattended scorer into production, and the
breaker covered only one. `digest.py` was verified unguarded: `messages.create` called
directly, no metering, no ceiling, no telemetry, and **no retry logic at all**.

`_RunCost`, `_call_claude_with_retry` and the metering were lifted out of `daily_monitor`
into `claude_budget.py` (`RunCost`, `call_claude_with_retry`, `within_budget`) and both
pipelines now import it. `DAILY_COST_CEILING_USD` was renamed **`RUN_COST_CEILING_USD`** —
it is a per-run ceiling, not a daily one, and there are now two. The shared module owns no
ceiling constant: `RunCost` takes it as a constructor parameter so each pipeline sets its
own. Telemetry records carry a `pipeline` field so the two stay distinguishable in one log.

| | `daily_monitor.py` | `digest.py` |
|---|---|---|
| `RUN_COST_CEILING_USD` | $2.25 (unchanged) | **$4.25** (new) |
| nominal run | ~$0.22 (83 articles) | ~$0.42 (164 articles) |
| retries | already had them | **new — see below** |

**`digest.py` gains retries it never had.** That fixes a silent failure — a transient API
error previously dropped a whole batch with only a log line — but one logical call can now
issue up to `MAX_ATTEMPTS` (3) billed requests, so its ceiling was set with that multiplier
in mind. Verified by stub: a batch that raises `InternalServerError` once now retries and
still returns all 164 articles, where before it would have returned 149.

**Ceiling derivation (no unguarded run — that is the run that most needs a ceiling).**
`news_pipeline.py` was first confirmed to make zero Anthropic calls, then run alone: it
produced **164 articles**. That is a floor, not the natural corpus — the run hit
`TOTAL_TIMEOUT_SECS = 300` and stopped early, and a GH Actions runner without the local TLS
proxy gets through more feeds in the same 300s. Applying the audit-verified figures
(142.7 output tok/article, ~1,967 input tok/batch):

| corpus | batches | cost | x3 retries |
|---|---|---|---|
| 164 (measured floor) | 11 | $0.42 | $1.25 |
| 500 (plausible production) | 34 | $1.27 | $3.81 |
| 812 (sum of all caps, absolute max) | 55 | $2.06 | $6.19 |

$4.25 is ~10x the measured floor, the same multiple as the daily job. Sanity-checked
against the table: even the absolute-maximum corpus costs $2.06 at 1x, so this cannot
false-trip on volume alone; it trips on a maximum corpus compounded by a retry storm.
**Provisional** — retune from telemetry (`call: "score_articles_with_claude"`) after the
first real weekly runs.

Verified by stub, `digest.py`: normal 164-article run completes unaborted (11 requests, all
164 scored); a runaway trips at batch 3/11 and keeps the 45 already scored; a transient
error retries and still scores all 164.

### Open issues

- **`run()` re-scores everything, every run.** Line 628 scores all in-window articles; the
  `seen` filter is not applied until line 632, and line 668 only ever adds score-3+ articles
  that emailed successfully. So `daily_monitor_seen.json` suppresses duplicate emails and
  upserts but saves **zero** scoring cost — even high scorers are re-scored. Every in-window
  article is scored on ~3 consecutive runs at `LOOKBACK_HOURS = 72`, so the +52/run above
  lands roughly 3x in billing. Not fixed in this change.
- **`CAP_REGIONAL` review — due 2026-09-11** (one week of drops from the 2026-09-04 commit).
  Judge the 41-ish drops/run against `regional_cap_drops.jsonl` (numerator) and
  `regional_feed_stats.jsonl` (denominator). Unchanged at 12 until then.
- **`MODEL` bump — due before 2026-09-29.** `"claude-sonnet-4-5-20250929"` is legacy-priced,
  with tentative retirement not before that date. **`run_telemetry.py`'s rates belong to the
  same change:** it hardcodes this model's per-MTok rates as module constants, and both
  pipelines' `RUN_COST_CEILING_USD` are enforced against them — bump the model without the
  rates and each breaker fires at the wrong real spend, in either direction. Existing log
  records are safe (each carries its own `rate_*` and `pricing_verified_on`); new runs are
  not. Keying rates by model ID is the fix, at bump time.
- **5 gap stories have no discoverable feed** (irishexaminer.com, independent.ie, thesun.ie,
  southernstar.ie, farmersjournal.ie). Follow-up: narrow Google News `site:` queries
  following the businesspost.ie pattern. Deliberately deferred so the feed work can be
  measured on its own first.
- **thesun.ie soft-blocks all fetches** (JS "Verifying Device" interstitial, HTTP 200). It
  was 79.7% of the alerts export and 973 of its rows scored 1. Filter it out first if the
  audit is rerun on a later export.

---

## Session 2026-07-24 — weekly LinkedIn posts moved to Cowork; cover image pipeline

The Friday weekly LinkedIn digest left this repo. `weekly_linkedin_digest.yml` no longer has
a cron (manual `workflow_dispatch` fallback only); `weekly_linkedin_digest.py` stays for that
fallback but is otherwise retired. Replacements, both Cowork scheduled tasks writing Cockpit
tasks (`ops.tasks`, source_schema `sd3-weekly-post`):

- **Friday 10:00 Dublin — news brief** (source_table `news-brief`): picks 5 from hub
  `news_items` (score 3+, 7d) PLUS radar `social_posts` (surfaced_brand, real company news
  only, max 2). News-brief voice, no links in body, featured companies tagged instead,
  one newsletter link in the first comment. Notes end with a machine-readable
  `PICKS_JSON: [{"company","slug","news_url"}]` line — the contract for the cover renderer.
- **Monday 10:00 Dublin — jobs post** (source_table `jobs-brief`): 4-6 roles approved in the
  past 8 days only, indigenous Irish first, grouped per company, role URLs in first comment.

### Cover image pipeline (new in this repo)

The Cowork sandbox cannot fetch images from news CDNs (allowlisted egress), so covers render
here:

1. `mirror-news-images` edge function (hub project, v1) + pg_cron `sd3-weekly-cover-assets`
   (Fri 06:45 UTC): mirrors `image_url` for the week's score-3+ news_items into
   `public.sd3_cover_assets` as base64 (cap 1.5MB, 30-day retention, RLS on, service-role
   access only). NOTE: function source is deployed via MCP and not yet synced to the cockpit
   repo's supabase/functions folder.
2. `weekly_cover.yml` (Fri 09:20 + 10:20 UTC, both fire for DST safety; the script exits
   quietly when today's news-brief task doesn't exist yet or the cover is already attached):
   `weekly_cover.py` parses PICKS_JSON from today's Cockpit task, pulls mirrored images,
   builds the 1200x1200 new-brand cover (navy #0B1B2B, bars #C15BE6/#22D3A5/#F59E0B/#00B4D8,
   Bebas Neue, `assets/cover/`), renders with Playwright chromium, uploads to the public
   `radar` bucket at `weekly-covers/YYYY-MM-DD.png`, inserts an `ops.task_attachments` row on
   the task and appends the public URL to the task notes. Stories without a mirrored image get
   branded gradient tiles.

### Review loop and cron retirements (same session, later)

- Friday trigger now delivers the top 5 PLUS the week's full candidate pool, numbered; Iddo
  replies in the run's chat with swaps ("replace 3 with 7"), the session rewrites the post and
  the Cockpit notes' PICKS_JSON and strips the cover lines, and `weekly_cover.yml` (now
  `20 9,10,11,12 * * 5`, picks-hash idempotent) re-renders the cover within the hour.
- `monthly.yml` cron RETIRED (manual dispatch only): monthly research email superseded by the
  bi-weekly newsletter and the weekly brief.
- `monthly_28th.yml` slimmed to newsletter-source export + commit + email only; its digest/
  jobs/events steps duplicated the daily and Friday runs.

### Jobs pipeline observations (no code changed)

Apify path verified healthy 2026-07-24: EA Sports returned 7 jobs (4 approved, Galway AI
roles), no `apify_error` on any source; zero-yield linkedin_only companies are plausible
zero-listing cases. Two minor open items:

- Stats Perform (linkedin_only) upserted a job 2026-07-17 but
  `company_careers_sources.last_successful_scrape_at` still reads 2026-04-24 —
  `mark_source_successful` did not stick for that source; worth a look.
- `last_scrape_error` is never cleared on a later successful/attempted run, so stale
  `serper_no_results` values linger on linkedin_only rows from before the Apify split.
  Cosmetic, but misleading when auditing source health.

---

## Session 2026-07-18 — jobs-cleanup brief: premise corrections, dedupe, batch-reject

Brief: after the 2026-07-17 weekly run, 164 jobs landed in pending, 162 of them
non-allowlisted-FDI (Super Technologies 127, VALD 27, 2K 5, European Tour 3). User had
already deactivated Super Technologies' source and manually cleaned the queue by hand
before this session. Five things were requested; three of the five diagnoses didn't match
the live DB when checked, which changed the plan significantly.

### 0. Critical finding: 6 commits + 5 new files had never reached `origin/main`

Before touching any of the five items, `git status` showed local `main` 6 commits ahead of
`origin/main` (`b999d8f` through `97a7e5f` — the entire 2026-06-30 LinkedIn stale-job fix and
the 2026-07-14 classifier office-slug fix), plus five **untracked** files representing the
entire 2026-07-14 Apify-migration/relevance-filter/events-cleanup session
(`apify_linkedin.py`, `relevance_filter.py`, `run_linkedin_apify.py`, `test_apify_linkedin.py`,
`events_pipeline/run_archive_sweep.py`). Since GitHub Actions runs off `origin/main`, **every
scheduled run — including 2026-07-17 — executed pre-fix code.** This directly explains item 3
below (Hexis tracking) and means STATUS.md's own claims of "fixed" for the 06-30/07-14 work
had never actually shipped. Origin had also independently gained 30+ automated bot commits
(`daily_monitor_seen.json` / newsletter-source / jobs CSV updates from the daily/monthly
crons) that local didn't have — a clean, no-conflict merge (disjoint file sets). Merged and
pushed with the user's explicit go-ahead; all prior "uncommitted work" is now committed in
logical grouped commits and live on `origin/main`.

### 1. FDI null-location gate — dropped, premise didn't hold

Brief's diagnosis: non-allowlisted FDI jobs with `location_raw IS NULL` were defaulting to
"allow" and skipping the Haiku relevance verdict. Checked directly against the hub DB before
writing anything (per CLAUDE.md's "verify before destructive SQL"): **zero** non-allowlisted-FDI
jobs, ever, have a null `location_raw` (checked across all 377 such rows, all statuses). The
"no relevance verdict" claim also didn't hold — it was based on the top-level
`classification->>'sportstech_relevance'` path, which is always null by construction
(`build_classification_record()` nests Haiku's output under `classification->'haiku'`, not the
top level). The nested path shows Haiku ran and returned a verdict for every sampled row.

What actually let the ~127 Super Technologies (Romania-based iGaming/betting operator,
`vertical='Betting & Fantasy'`) roles through: `_check_fdi_geography` (non-allowlisted path)
has no reject entries for Gibraltar/Spain/Romania/Croatia/Brazil — real, known locations that
are simply absent from the list — so `geo_check` fell through to `"pending"` (correctly, by
current code, sending the job to Haiku rather than skipping it). Haiku then judged
role-fit-to-company-context ("is this role core to what the company does") rather than
"is this company genuinely sportstech" — since `Betting & Fantasy` is a legitimate vertical
in this system (DraftKings, Flutter are allowlisted FDI companies in the same vertical),
Haiku rated Football Trader/CRM Executive/Principal Product Manager as `"relevant"`. This is
inherent to the (protected, do-not-touch) Haiku prompt, not a code bug — and the brief itself
rules out the only clean geography-based fix (widening the reject-list) since VALD's genuine
postings sit in the same countries (see item 2). **No code change made.** The Super
Technologies incident was already correctly resolved by the user's own action (deactivate
source + manually reject rows) — a company-scope judgment call, which is the right lever for
"this business calls itself sportstech but isn't," not something geography or relevance rules
can systematically catch.

### 2. Duplicate listing collapse — implemented, doesn't fire on the motivating VALD case (by design)

Brief's diagnosis: VALD's 26 identical "Business Development Manager" postings should
collapse to 1, matching "title exact + location null/identical" only. Live DB check showed
all 25 (of the still-pending sample) have **distinct** locations (Jeddah, Riyadh, Marseille,
Warsaw, Astana, Schweiz, ...) — the conservative rule as scoped would never fire on this
data. Confirmed with the user to implement literally as scoped anyway (safe, narrow, guards a
real but different case — true same-title-same-location repeats). See ARCHITECTURE.md's new
"Duplicate Listing Collapse" section for the implementation and the live before/after check
(56 raw VALD jobs → 56 after dedupe, 0 dropped, confirming the rule leaves VALD's per-country
pattern alone as designed).

### 3. Hexis / `none_found` tracking — not a bug, confirmed live

100% of the 46 active `none_found` sources (not just Hexis) had `last_scrape_run_at = NULL`
as of this session's start — this was item 0's push gap, not adapter logic. With the fix now
live, ran `python jobs_pipeline/run_linkedin.py --dry-run --company "Hexis"` directly (via a
local Norton-TLS-proxy launcher, see CLAUDE.md workaround): Serper returned 10 URLs, all 10
were unrelated Tax Director/staffing-firm postings (Atlas Search, Levelociti, Jobot, Brewer
Morris, ...), all correctly rejected at Stage 4 name validation — none reached the
recency/ID-floor gate. **Hexis genuinely has no discoverable LinkedIn posting under this
query right now** — "Hexis" alone is a common enough term that Google surfaces unrelated
noise. Not a tracking bug; no code change needed beyond the item-0 push.

### 4. Hub LinkedIn-job visibility — not filtered, confirmed live in dev

Brief's diagnosis: 13 approved LinkedIn-sourced jobs (Stats Perform, EA Sports, Clubforce,
Hexis, Tixserve, Nutritics, Off The Ball) don't appear on the public `/jobs` board. Checked
`sd3-intelligence-hub`'s entire query surface (frontend queries, RLS policies, views,
functions) for any `source`/`url` filter — **none exists anywhere**, including the enforced
`"Public can read approved jobs"` RLS policy (`qual: status = 'approved'`, no source clause).
Confirmed live: started the hub's dev server (new `.claude/launch.json`, plus a Node
`NODE_EXTRA_CA_CERTS` launcher for the same local Norton-proxy issue affecting Node's fetch),
and all confirmed-`approved` LinkedIn jobs render on `/jobs` today — e.g. Hexis "Chief
Technology Officer" with a live `Apply →` link straight to
`linkedin.com/jobs/view/chief-technology-officer-at-hexis-4432256395/`, indistinguishable
from greenhouse/workday/ashby cards. No filter to remove, nothing to document as
intentionally hidden — the brief's premise didn't match current (or apparently ever) behaviour.

### 5. Batch-reject action — implemented in `sd3-intelligence-hub`

Added to `app/admin/jobs/AdminJobsClient.tsx`: per-row checkboxes (hidden on already-rejected
rows, matching the existing single-Reject button's visibility rule), a "select all in view"
checkbox, and a bulk action bar (reason picker + "Reject N" + "Clear") that appears once
anything is selected. Selection clears on tab switch. New
`app/api/admin/jobs/batch-reject/route.ts` mirrors the existing single-reject route exactly —
same `getAdminUser()` hardcoded-admin-email gate, same `jobs` update shape
(`status/rejected_reason/reviewed_at/approved_by=null/approved_at=null`), same one
`jobs_review_feedback` row per job — just batched via `.in('id', jobIds)` instead of looping
one at a time. Verified: `npx tsc --noEmit` clean, Next dev compiles with no errors, the
`/admin/jobs` auth gate correctly redirects to `/login`. **Could not click through the actual
authenticated UI** — `requireAdmin()` is a hardcoded check against a real Google/Supabase
login (`iddodiamant@gmail.com`) this session has no credentials for — so functional
verification is code-review + type-check only, not an in-browser click-through. Say so
explicitly rather than claiming full end-to-end verification.

**Found but not touched:** `sd3-intelligence-hub` has substantial unrelated in-progress work
sitting uncommitted (modified `CLAUDE.md`, `app/jobs/JobsClient.tsx`, `components/Sidebar.tsx`,
`types/job.ts`; untracked `app/directory/`, `ContactsSection.tsx`, `RelevantContacts.tsx`,
`lib/jobMatching.ts`) — none of it touched. The batch-reject commit was staged by explicit
file path (`AdminJobsClient.tsx` + the new route file only) to keep it isolated; `git diff`
confirmed the file's entire diff was this session's addition, nothing pre-existing mixed in.
**Not pushed** — pushing this repo (a live-deployed admin panel with a real auth gate) wasn't
covered by the push approval given for `sportstech-digest`, and the user should decide
separately given the other WIP sitting alongside it.

### Tests

- New `jobs_pipeline/test_dedupe.py` (10 assertions, no pytest dep) — exact-duplicate
  collapse, both-null-location collapse, VALD-style different-locations-both-kept,
  different-titles-same-location-both-kept, null-vs-populated-location not merged,
  case/whitespace normalisation, 3-way mixed dedupe. All existing test suites
  (`classifier.py` __main__, `test_linkedin_gate.py`, `test_apify_linkedin.py`) still pass —
  65 assertions, no regressions.

### Next session candidates

- Confirm next Friday's run picks up the now-pushed fixes (source tracking should populate
  for all `none_found`/`linkedin_only` sources; classifier office-slug fix should be live).
- Decide whether to push the `sd3-intelligence-hub` batch-reject commit, and separately
  triage the unrelated WIP sitting in that repo (directory feature, contacts section, job
  matching, `CLAUDE.md` updates) — none of it was reviewed this session beyond noting it exists.
- If Hexis's name-collision problem with generic Serper results recurs, consider a
  `linkedin_search_name` override with a more disambiguating term (not done this session —
  diagnostic only, per the brief's ask).

---

## Session 2026-07-14 (cont.) — Events pipeline cleanup

### Problem (measured against the live hub DB before this session)

123 total events: 86 pending, 32 rejected, 5 verified. Diagnosis surfaced four distinct
issues, only one a code bug:

1. **No archive-sweep equivalent existed at all.** Unlike jobs (`run_archive_sweep.py`),
   nothing ever removed a stale pending event from the review queue. 50 of 86 pending
   (58%) were already past-dated — oldest from **2025-02-24**, 17 months stale.
2. **`ai_tech_ireland` category ~1% real approval rate.** The extractor's system prompt
   treats `sportstech`/`ai_tech_ireland`/`startup_opportunity` as equally in-scope (only
   `not_relevant` is auto-filtered), but actual admin review behaviour told a different
   story: 68 pending + 26 rejected + 1 verified for `ai_tech_ireland` (~1% approval) vs.
   ~12–18% for the other two categories. This is 77% of everything ever captured, for a
   segment almost never approved.
3. **Zero audit trail on rejections** — all 32 rejected events had `rejected_reason IS
   NULL`. Not fixed this session (would require a hub admin-panel UI change, out of this
   repo's scope) — flagging for awareness.
4. **Recurring events re-enter the pending queue every run.** "Hack and Chill" (weekly
   Tog Hackerspace meetup) had 9 rows — confirmed each is a genuinely distinct Meetup URL
   per occurrence (not a de-dup bug), but a low-value recurring event floods the queue
   indefinitely with no mechanism to collapse to "just the next occurrence."

All four confirmed with the user before acting (`ai_tech_ireland` handling and recurring-
event handling were explicit judgment calls, not obvious bugs).

### Code changes — new file

- `events_pipeline/run_archive_sweep.py` — mirrors `jobs_pipeline/run_archive_sweep.py`'s
  CLI shape (`--dry-run` flag, same logging style) but far simpler: events have no
  `archived` status (no CHECK constraint on `events.status`; only pending/rejected/verified
  are used in practice), so this reuses `rejected` with `rejected_reason='event_date_passed'`
  rather than inventing a new status value the hub frontend may not render. Rejects pending
  events where `date < today`; leaves null-date pending events untouched (separate
  extraction-quality issue, not addressed this session). Exposes `run_sweep(dry_run: bool) ->
  dict` for direct import (no subprocess/log-parsing, unlike jobs' classifier/sweep steps —
  this is a fast pure-DB operation with no LLM call to isolate).

### Code changes — modified files

- `events_pipeline/supabase_events_client.py` — two new functions:
  - `mark_event_auto_rejected(event_id, reason)` — flips `pending` → `rejected`, guarded by
    `.eq("status", "pending")` so it never overwrites a human's prior decision on a
    re-scraped row.
  - `collapse_recurring_series(name, recurrence)` — for pending events sharing an exact
    `name` match with non-null `recurrence` and non-null `date`, keeps only the
    soonest-dated row and rejects the rest with `rejected_reason='recurring_series_superseded'`.
    Deliberately exact-match (no fuzzy matching) and date-gated (undated duplicates are left
    alone — can't determine ordering).
- `events_pipeline/weekly/runner.py` — `run_extractions()` now calls both new functions
  inline, right after a successful `upsert_event()`: auto-rejects `ai_tech_ireland` category,
  then (if not already auto-rejected) checks for a recurring-series collapse. `ExtractionResult`
  gained `auto_rejected_reason: str | None` so the email can distinguish "genuinely pending"
  from "inserted then immediately auto-rejected."
- `events_pipeline/run_weekly_events.py` — added an archive-sweep step (imports
  `run_sweep` directly) between extraction and the DB snapshot; docstring step list
  renumbered.
- `events_pipeline/weekly/email_builder.py` — `build_email()` gained a `sweep_result`
  param and a new "Archive Sweep" section; "New Events for Review" now excludes rows with
  `auto_rejected_reason` set (they were inserted then immediately flipped to rejected, not
  actually awaiting review); "Extraction Results" gained two breakdown rows for the two
  auto-reject reasons.

### One-off cleanup run this session (live, not just code-forward)

1. `python events_pipeline/run_archive_sweep.py --dry-run` → confirmed 50 candidates
   (exactly matching the diagnosis), then run live → **50 rejected**
   (`rejected_reason='event_date_passed'`).
2. Remaining pending `ai_tech_ireland` rows not already caught by the date sweep (37 of
   the original 68 were past-dated and already swept) — bulk SQL, previewed then committed
   → **31 rejected** (`rejected_reason='ai_tech_ireland_auto_reject'`).
3. Checked for remaining recurring-series duplicates after 1–2 → **0 found** (the date
   sweep had already caught every dated Hack-and-Chill instance; only its 3 null-dated
   copies remain pending, untouched by design).

**Net result: 86 pending → 5 pending.** Verified the final 5 by hand — all genuine,
future-dated, real categories (`The Sportstech Sessions`, `TechBrew: Founder Stories`,
`Irish Sport and Creativity 2026`, `WomenHack - Dublin`, `Galway Game Makers Meetup`).
Verified end-to-end via `python events_pipeline/run_weekly_events.py --skip-adapters
--skip-email` (exercises the new archive-sweep wiring + snapshot + email-build code paths
with zero adapter/Claude calls) — ran clean, correct new "Archive Sweep" email section
rendered.

### Not addressed this session

- Null-date pending events (23 originally, ~20 remain) — extraction couldn't parse a date;
  separate quality issue from staleness.
- `rejected_reason` audit trail for *human* rejections (all NULL) — hub admin-panel UI
  change, outside this repo.
- The `ai_tech_ireland` extractor prompt itself is unchanged — Claude still classifies and
  tags these events (for audit in `classification`/`extraction`), only the *runner* now
  auto-rejects. Revisit if the ~1% approval rate shifts.

---

## Session 2026-07-14 — Apify LinkedIn path for `linkedin_only` + relevance pre-filter

### Problem (measured against the live hub DB before this session)

199 all-time LinkedIn-sourced jobs: 139 rejected, 47 archived, 13 approved (~6.5% approval
rate). Rejection reasons dominated by `not_sportstech` (66), `too_junior` (26), plus a long
tail of stale/old-job free-text reasons and the `linkedin_stale_id_cleanup_2026_06_30` bulk
cleanup (19) — confirming the 2026-06-30 posted-age gate fix (`MIN_LINKEDIN_JOB_ID` /
`MAX_POSTED_AGE_DAYS`) was necessary but didn't retroactively clean the backlog.

Also found (not previously documented): of the 12 active `company_careers_sources` rows with
`ats_platform='linkedin_only'`, **10 had `last_scrape_run_at = NULL`** — they were essentially
never reached by the combined Serper query in practice. Only EA Sports and Stats Perform
showed any run timestamp, and Stats Perform's last *successful* scrape was from April. This is
independent evidence for moving `linkedin_only` off the Serper path entirely, not just adding a
stricter gate to it.

### Code changes — new files

- `jobs_pipeline/relevance_filter.py` — rule-based, denylist-driven title noise filter (street
  team, forum coordinator/moderator, community/content moderator, brand ambassador, generic
  customer-support/retail/cashier roles). Deliberately conservative: only a denylist hit causes
  a drop; there's no allowlist-driven rejection. **Assumption flag**: the original brief phrased
  this as "drop roles whose title clearly falls outside [a job-function allowlist]" — I judged
  that unsafe as a hard gate (many legitimate titles like "Backend Developer" don't contain an
  allowlist keyword) and implemented denylist-only rejection instead, with the allowlist kept
  for reference. Revisit if noise is still leaking through in practice.
- `jobs_pipeline/adapters/apify_linkedin.py` — new `ApifyLinkedInAdapter`, covers `linkedin_only`
  sources (12) via the Apify LinkedIn Jobs Scraper actor (`curious_coder/linkedin-jobs-scraper`),
  called via plain `requests` (no new dependency). Queries LinkedIn's own live `/jobs/search`
  directly, so closed postings structurally never appear in its output — the freshness gate
  (`MAX_JOB_AGE_DAYS`, default 30) only refines "how recently posted", it doesn't need to prove
  liveness the way the Serper adapter's `MIN_LINKEDIN_JOB_ID` floor does. Missing `APIFY_TOKEN`
  fails cleanly: `_ApifyTokenMissingError` → treated as an abort signal, `last_scrape_error`
  recorded, no crash. See `ARCHITECTURE.md` for full design detail.
- `jobs_pipeline/run_linkedin_apify.py` — CLI mirroring `run_linkedin.py`'s
  `--dry-run --company NAME` pattern.
- `jobs_pipeline/test_apify_linkedin.py` — 34 assertions covering `relevance_filter.py` and the
  freshness/URL-building helpers in the new adapter. No pytest dependency, same style as
  `test_linkedin_gate.py`.

### Code changes — modified files

- `jobs_pipeline/adapters/linkedin.py` — now covers `none_found` sources only (46). Added a
  Stage 5 relevance-filter gate (calls `relevance_filter.check_relevance()`) after the existing
  posted-age gate, before a job is added to the upsert list. Log line gained a `relevance=N`
  counter.
- `jobs_pipeline/supabase_jobs_client.py` — split `get_linkedin_sources()` into
  `get_serper_linkedin_sources()` (`none_found`) and `get_apify_linkedin_sources()`
  (`linkedin_only`, now also selects `fdi_classifier_allowlisted` for the Ireland/UK geography
  split).
- `jobs_pipeline/run_linkedin.py` — switched to `get_serper_linkedin_sources()`; docstring
  updated to reflect `none_found`-only scope.
- `jobs_pipeline/weekly/runner.py` — renamed `run_linkedin_adapter()` →
  `run_linkedin_serper_adapter()` (step name `linkedin_serper`); added
  `run_linkedin_apify_adapter()` (step name `linkedin_apify`). Both now show as separate rows in
  the weekly summary email.
- `jobs_pipeline/run_weekly.py` — calls both new functions in place of the old single call.
  `APIFY_TOKEN` is deliberately **not** in `_REQUIRED_ENV` — its absence degrades one step to a
  logged warning, not a pipeline-wide abort.
- `.env.example` — added `APIFY_TOKEN`; also fixed a pre-existing gap where `SERPER_API_KEY` was
  missing from this file despite being required by the weekly workflow.
- `.github/workflows/jobs_weekly.yml` — added `APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}`.
- `requirements.txt` — **no change**. Apify called via plain `requests`, matching how Serper is
  already called (no vendor SDK anywhere in this repo).

### Apify token added mid-session — three live bugs found and fixed

The user added a real `APIFY_TOKEN` to `.env` mid-session (kept private; never shared with
Claude). Full `--dry-run` across all 12 `linkedin_only` companies then surfaced three bugs that
`--company` smoke-testing with a missing token couldn't have caught:

1. **HTTP 201 treated as failure** (`adapters/apify_linkedin.py`) — Apify's
   `run-sync-get-dataset-items` endpoint returns `201 Created` on a successful synchronous run,
   not `200`. The original check (`if resp.status_code != 200`) rejected every successful call as
   `_ApifyRequestError`, discarding real data (visible in the raw error text — genuine LinkedIn
   job links came back on every one of the 12 companies). Fixed: accepts `(200, 201)`.
2. **`linkedin_search_name` override bypassed name validation** (`adapters/apify_linkedin.py`) —
   copied from `linkedin.py`'s Serper path, where skipping the equality check under `override=True`
   is safe because Serper's `site:linkedin.com/jobs/view "X"` is a precise quoted-phrase Google
   search. The Apify actor instead runs LinkedIn's own loose native keyword search
   (`keywords=X&location=Y`), which surfaces anything LinkedIn's relevance ranking associates with
   the term. Ungated, live dry-run results for the three sources with an override set
   (Danu Sport, EA Sports, Stats Perform) were 25/25, 24/25, and 22/25 **unrelated companies**
   (Sony, Rockstar, PayPal, Ryanair, Meta, Novartis...) that would have been written straight to
   the hub. Fixed: name validation now always runs; `override` only changes *which* name is
   compared (`linkedin_search_name` vs `company_name`), never *whether* it's compared. The
   now-unused `override` bool bypass was deleted.
3. **Parenthetical company-name suffixes not stripped** (`adapters/linkedin.py`,
   `_normalise_company_name()`, shared by both adapters) — after fixing #2, EA Sports' own genuine
   posting ("Core Agentic Solutions Lead Architect") was rejected as `name_mismatch` because
   LinkedIn returns EA's `companyName` as `"Electronic Arts (EA)"`, and the existing suffix-strip
   list only handled legal suffixes (Ltd/Inc/LLC/etc.), not bracketed abbreviations. Added
   `re.sub(r"\s*\([^)]*\)\s*$", "", s)`. Unit-verified: both normalise to `"electronic arts"`.

All 19 + 34 existing test assertions still pass after these fixes. Live dry-run behavior after
all three fixes: Blizzard Entertainment correctly found 1 genuine posting out of 25 candidates
(24 correctly rejected as unrelated gaming-industry noise); every other company in the 12 either
found 0 (honest — Apify's own keyword search is noisy run-to-run, same as Serper) or correctly
rejected 100% of non-matching candidates. No false positives observed in the final run.

### Boylesports Teamtailor adapter — silent 7-week zero-yield bug (found via user-supplied job URLs, then fixed)

User supplied 7 real LinkedIn/ATS job URLs that the pipeline had missed, prompting a fresh DB
audit. Finding: `careers.boylesports.com/jobs.json` (Teamtailor, the sole active `teamtailor`
source) has returned **zero new jobs since 2026-05-22** despite `last_scrape_run_at` advancing
every week through 2026-07-10 — 7+ weeks of silent zero-yield, never surfaced because the
endpoint was returning `200 OK` throughout (no exception → no HTML-fallback trigger, no visible
error anywhere).

Root cause, confirmed by fetching the live endpoint directly: it now serves **JSON Feed 1.1**
(`{"version": "https://jsonfeed.org/version/1.1", "items": [...]}`, each item carrying a
Teamtailor-specific `_jobposting` schema.org JobPosting object), not the JSON:API shape
(`data`/`included`/`relationships`) `adapters/teamtailor.py` was written against. Parsing JSON
Feed against JSON:API code reads `data.get("data") == []` every time — 0 jobs, no exception, and
since the HTML fallback only triggers on HTTP/connection errors, it never engaged either.

Fixed: `_fetch_all()` now auto-detects shape per response (`"items" in data and "data" not in
data` → JSON Feed) and routes to a new `_normalise_json_feed()` alongside the existing
`_normalise()` for JSON:API. Verified live: 47 jobs fetched (was 0), including the specific
"Head of Cyber Security" posting the user flagged, with structured location (from
`_jobposting.jobLocation.address`) and full HTML-stripped summary. JSON:API path is left intact
for any future Teamtailor source that might still serve that shape.

### Four new companies onboarded (found via user-supplied job URLs)

All four were completely absent from `companies` — no adapter of any kind was scraping them
before this session. All confirmed to use adapters this repo already supports (no new adapter
code needed); vertical/geography-scope classification confirmed with the user before writing:

| Company | Platform | Endpoint | Vertical | Geography |
|---|---|---|---|---|
| European Tour (DP World Tour) | `workday` (tenant=`europeantour`, pod=3) | `europeantour.wd3.myworkdayjobs.com` | Other / Emerging | Standard Ireland-only (not allowlisted) |
| 2K | `greenhouse` (slug=`2k`) | `boards-api.greenhouse.io/v1/boards/2k` | Esports & Gaming | Standard Ireland-only |
| VALD | `breezy` (custom domain) | `careers.vald.com/json` | Performance Analytics | Standard Ireland-only |
| Super Technologies (formerly Superbet) | `greenhouse` (slug=`super`) | `boards-api.greenhouse.io/v1/boards/super` — note: public board is under the `eu.greenhouse.io` display domain, but the API host is the same global `boards-api.greenhouse.io` regardless | Betting & Fantasy | Standard Ireland-only |

All flagged `is_fdi=true, is_irish_founded=false, fdi_classifier_allowlisted=false`. European
Tour is a genuine scope judgment call, not a clean sportstech vendor fit (it's a golf tour
operator; most of its 6 open Workday roles are tournament/events logistics) — onboarded at the
user's explicit choice, expect a high `not_sportstech` rejection rate from Haiku on this one.
Each source verified live via direct `adapter.fetch()` calls (no `.run()`, zero DB writes during
verification): European Tour 6 jobs, 2K 132, VALD 53, Super Technologies 186 — all globally-scoped
boards where the existing FDI-geography rule + Haiku classifier will do the Ireland/UK narrowing,
same as every other FDI company already in the pipeline.

### Doc-drift found (not part of this session's changes, flagging for awareness)

The live `company_careers_sources.ats_platform` CHECK constraint (queried directly) is:
`greenhouse, lever, workable, ashby, teamtailor, smartrecruiters, bamboohr, personio, recruitee,
breezy, workday, custom_html, linkedin_only, none_found` — this differs from the list documented
in `ARCHITECTURE.md` (which lists `phenom`, `gr8people`, `manual` as allowed and omits `workable`,
`smartrecruiters`, `recruitee`). Not corrected in this session since it wasn't the task at hand;
worth reconciling docs against the live schema next session.

### Pending cleanup (not run this session)

Historical LinkedIn rejected/archived rows (139 + 47) are untouched — this session only changes
what happens to *new* scrapes going forward. No bulk SQL cleanup was requested or run.

---

## Session 2026-06-30 — LinkedIn stale-job leak fix

Root cause of stale LinkedIn jobs reaching the hub as pending (every manual
free-text rejection in the hub was a LinkedIn job): the posted-age gate allowed
any job whose date couldn't be parsed, Serper discovery had no recency
constraint, and the LinkedIn `run()` override never stamped source-tracking so
the archive sweep never aged stale LinkedIn jobs out.

### Code changes — `jobs_pipeline/adapters/linkedin.py`

- **Serper recency:** new `_SERPER_RECENCY_TBS = "qdr:m"` (past month) added to
  the Serper POST payload, so discovery only returns recently-posted listings.
- **Strict posted-age gate:** when `_extract_posted_days_ago` returns `None`,
  the adapter now falls back to the LinkedIn job ID instead of allowing. New
  `_extract_job_id(url)` parses the trailing numeric ID (stripping any query
  string / `refId` / fragment so a refId's digits aren't misread). New
  `MIN_LINKEDIN_JOB_ID = 4_200_000_000` floor (~95% of current-era ~4.40e9):
  IDs below it reject as `stale_id`; no usable ID rejects as `posted_age_unknown`.
  Date-found-and-too-old still rejects as `posted_too_old` (unchanged). The
  default for LinkedIn flipped from allow to reject.
- **Empirical finding:** LinkedIn serves scraper IPs a stripped page with no
  parseable `datePosted` on ~100% of fetches, so `MIN_LINKEDIN_JOB_ID` is the
  *primary* recency gate in practice, not a backstop. Floor set accordingly.
- **Counters:** per-source summary log gains `stale_id` and `age_unknown`
  alongside the existing `stale_age`.
- **Source tracking:** `run()` now wraps its body in `try/finally` mirroring
  `BaseAdapter.run()` — always `mark_source_attempted` (last_scrape_run_at), and
  `mark_source_successful` (last_successful_scrape_at) when `upserted_count > 0`.
  Runs on every path including Serper-no-results, fixing both the archive sweep
  health gate skipping LinkedIn sources and the ggCircuit/Orreco "never scraped"
  cosmetic issue.

### Code changes — `jobs_pipeline/classifier.py`

- Fixed `_check_fdi_geography_allowlisted` Workday office-slug normalisation:
  `.replace('-', ' ').replace('---', ' ')` → `re.sub(r'-+', ' ', office)`, so
  `/job/Remote---Bulgaria/` collapses to `remote bulgaria` and matches the
  reject marker instead of leaking to `pending`.

### Tests

- New `jobs_pipeline/test_linkedin_gate.py` (19 assertions, no pytest dep):
  `_extract_posted_days_ago` (JSON-LD, Z-suffix, relative regex, None paths) and
  `_extract_job_id` (bare ID, title slug, `?refId`, refId-with-digits,
  trackingId, `#fragment`, legacy 8-digit, floor comparison).
- Extended `classifier.py` `__main__` harness with 5 allowlisted-geography
  office-slug cases (Remote---Bulgaria, Remote---London, Dublin, Berlin,
  Tokyo-Office). Run: `python jobs_pipeline/classifier.py`.

### Dry-run verification (live Serper + LinkedIn)

| Company | Would-upsert before | Would-upsert after |
|---|---|---|
| Stats Perform (FDI) | 9 (all undated, allowed) | 0 |
| Legitfit (indigenous) | 9 (all undated, allowed) | 0 |

### Pending cleanup (handed to operator, not run)

Historical stale rows already in the queue need a manual SQL cleanup — see the
`BEGIN; <preview SELECT>; UPDATE; COMMIT;` block provided this session (reject
LinkedIn pending rows with `classification->>'sportstech_relevance' IS NULL`
and a job ID below the floor). Run after reviewing the preview row count.

---

## Session 2026-05-28 — Jobs pipeline overhaul

### Schema changes

- Added `fdi_classifier_allowlisted` boolean to `companies` table. Set `true` for 18 FDI sportstech companies: Blizzard Entertainment, Catapult, DraftKings, EA Sports, Fanatics, Fitbit, Flutter Entertainment, Genius Sports, ggCircuit, Hudl, LiveScore, PFF (Pro Football Focus), Riot Games, Stats Perform, Strava, Teamworks, Thrive Global, WHOOP.

### Code changes — `jobs_pipeline/classifier.py`

- Removed `"graduate"` from `_JUNIOR_KEYWORDS`. Graduate-level engineer/PM roles at Irish sportstech companies were being silently rejected. Haiku now judges seniority on those titles.
- Added parenthetical-strip to `_norm_job_function`: `re.sub(r'\s*\([^)]*\)\s*$', '', v)` runs before the enum check. Fixes Haiku returning verbose values like `"Engineering (software/hardware/devops/QA/infrastructure roles)"` instead of `"Engineering"` — was silently setting `job_function=null` for hundreds of roles.
- Created `_check_fdi_geography_allowlisted(location_raw, url=None)` with Ireland+UK eligible list and a US/Asia/non-UK-EU reject list. Multi-location strings ("Multiple Locations", "N Locations") no longer auto-reject — they attempt a Workday URL fallback first, then fall through to "pending" for admin review.
- Modified the FDI block to route allowlisted FDIs to `_check_fdi_geography_allowlisted` and non-allowlisted to the original `_check_fdi_geography` (Ireland-only). Non-allowlisted FDI behaviour unchanged.
- Added `url` parameter to `_check_fdi_geography_allowlisted`: when location is ambiguous, parses the Workday `/job/{Office-CC}/` URL path as fallback. Checks office string against Ireland/UK pass-markers and US/Bulgaria/Asia reject-markers. Falls through to "pending" if URL doesn't help.
- Fixed `'bg'` to `' bg'` (leading space) in the office reject list to match word-boundary convention of US state codes and prevent false matches on slugs containing "bg" as a substring.

### Code changes — `jobs_pipeline/run_classifier.py`

- Added `fdi_classifier_allowlisted` to the `_fetch_companies` select list so the new column reaches `run_rules()`.

### Code changes — `jobs_pipeline/weekly/runner.py`

- `_aggregate()` now emits `status="warning"` when `scraped == 0` and no exception was raised. Previously showed as green "success", masking broken endpoints (e.g. Breezy with empty board, Stats Perform JS-rendering failure).

### Code changes — `jobs_pipeline/weekly/email_builder.py`

- `_status_cell()` handles `"warning"` with amber text (`#b85c00`, `font-weight:bold`), matching the existing `credit_exhausted` pattern.

### Code changes — `jobs_pipeline/adapters/linkedin.py`

- Added `MAX_POSTED_AGE_DAYS = 90` constant.
- Added `_extract_posted_days_ago(html)`: reads JSON-LD `datePosted` first (precise ISO timestamp); falls back to `"(?:Posted|Reposted)\s+(\d+)\s+(hour|day|week|month|year)s?\s+ago"` regex. Returns `None` if neither method finds a date.
- Added posted-age check after name-match validation. Rejects with reason `"posted_too_old (N days)"` when `days_ago > 90`. Allows when `days_ago is None` (lenient on missing data). Counts in new `failed_stale` counter.
- Added `stale_age=N` to per-source summary log line alongside `name_mismatch`.

### Source migrations (5 companies)

| Company | Before | After | Reason |
|---|---|---|---|
| EA Sports | BambooHR (`ats_slug=ea`) | `linkedin_only`, `linkedin_search_name='Electronic Arts'` | Wrong BambooHR slug belonged to a social-services org; 29 misattributed jobs deleted |
| Stats Perform | Teamtailor (HTML fallback, 0 jobs) | `linkedin_only`, `linkedin_search_name='Stats Perform'` | JS-rendered careers page, HTML fallback has never worked |
| Catapult | BambooHR | Greenhouse (`catapultsports`) | They moved ATS |
| PFF (Pro Football Focus) | `custom_html` | `linkedin_only` | `custom_html` has no adapter — was silently skipped every run |
| Thrive Global | `custom_html` | `linkedin_only` | Same; their ATS is Rippling but `rippling` not in CHECK constraint |

`linkedin_search_name` overrides added: `ggCircuit='ggCircuit'`, `Orreco='ORRECO'`.

### One-off data operations

1. Reset 909 historical fdi_geography-rejected jobs (only from the 18 allowlisted companies) back to `status='pending', classification=null, rejected_reason=null`.
2. Ran `run_classifier.py` to re-classify the 909: ended at 568 pending after Haiku filtered genuinely non-sportstech roles.
3. Bulk cleanup #1 (Ireland-only criterion): 522 jobs rejected with `rejected_reason='fdi_geography_cleanup_2026_05_28'` — jobs where `location_normalised` contained no Ireland or UK signal. 47 pending remaining.
4. Manual hub cleanup: rejected 24 stale EA Sports / Stats Perform LinkedIn jobs that were old/inactive postings (LinkedIn keeps old URLs live; Serper returned them by relevance not recency).
5. Bulk cleanup #2 (Multiple Locations leak): 15 Workday/Greenhouse jobs rejected where `location_normalised='Multiple Locations'` but URL or `location_raw` pointed to non-Ireland/UK offices (Bulgaria, US Midwest, etc.) with `rejected_reason='multiple_locations_no_ireland_uk_signal'`. Ended at 8 pending.
6. Ran `run_reclassify_all.py` to backfill `job_function` on 171 jobs (166 set, 5 genuinely ambiguous DraftKings operational roles returned null).

---

## Open Bugs and Observations

**Kitman Labs duplicate job insertion.** Same job titles appear with both `approved` and `rejected` status, timestamps within 1 second. Suggests the upsert key is not uniquely resolving on URL, or the RPC is not deduplicating correctly. Pre-existing, not related to today's work. Investigate before next Friday's run.

**`rejected_reason` inconsistency in older jobs.** The `rejected_reason` text column and the `rejected_reason` field inside `classification` JSONB appear inconsistent for some older rows (Kitman Labs jobs have null in both despite having classification data). The write path in `run_classifier.py` sets both, but older jobs may have been written before that field existed. Worth auditing.

**Pending queue composition skew.** After today's cleanup the queue is ~100% allowlisted FDIs. Indigenous Irish companies are not generating pending jobs — either no new scrapes this week or Haiku is too strict on `not_sportstech` for indigenous companies. Monitor over the next 2–3 Friday runs; if trend continues, consider Haiku prompt tuning.

**Boylesports Teamtailor returned 0 jobs this week.** Verified the endpoint is live at `careers.boylesports.com/jobs.json` — genuinely empty board, not a scraping failure. Monitor next Friday.

**Rippling adapter runs against 0 sources.** Logs a line each week. `rippling` is not in the `ats_platform` CHECK constraint — cannot add sources without `ALTER TABLE`. If Rippling support is needed (Thrive Global, others), add to constraint first.

**Phenom adapter also runs against 0 sources.** Same cleanup opportunity.

**Greenhouse Harvest API deprecation August 2026.** The Harvest API (v1/v2) is being deprecated. The Job Board API (`boards-api.greenhouse.io`) which this pipeline uses is NOT affected. Note for awareness only.

---

## Next Session Candidates

- Validate the strict LinkedIn posted-age gate + source-tracking fix on next Friday's run: expect far fewer LinkedIn pending jobs, and confirm `last_scrape_run_at` / `last_successful_scrape_at` now populate for LinkedIn sources (incl. ggCircuit/Orreco no-results).
- Re-check `MIN_LINKEDIN_JOB_ID` (4.2e9) against a known-recent posting after a few weeks and bump toward ~95% of current-era IDs if drift causes false `stale_id` rejects.
- Investigate Kitman Labs duplicate jobs and confirm whether other companies share the pattern.
- Decide whether to skip adapters with 0 active sources (rippling, phenom) to reduce log noise.
- Build proper Rippling adapter if EA Sports / Thrive Global volume justifies; requires `ALTER TABLE ats_platform CHECK` first.
- Build NGB jobs pipeline (analytics/tech/performance roles from National Governing Bodies and Irish sports clubs).
- Consider scheduling `run_reclassify_all.py` periodically to catch null `job_function` values that creep in after Haiku credit exhaustion.

---

## Earlier Sessions (condensed)

**2026-05-13** — Daily email restructure: removed LinkedIn draft generation from `daily_monitor.py`, added `relevance` field (email-only, score 3+4 only) in scoring prompt, new per-article format (linked heading, metadata block, summary, relevance line).

**2026-05-04** — Teamtailor/Workday/Personio summary extraction fixed (JSON-LD from HTML detail pages). Classifier `summary_excerpt` field added (10th Haiku output, max 400 chars, `max_tokens` bumped to 1224).

**2026-05-02** — `weekly_linkedin_digest.py` created (news-only, Friday 12:00 UTC, top-5 with diversity constraints, email-only).

**2026-04-29** — `ALERT_CC` support added to `daily_monitor.yml`.

**2026-04-28** — News pipeline tuning: irishtechnews.ie switched to direct RSS, legal/governance queries added (positions 50–53), Supabase company query limit raised to 150, score 3/4 governance criteria expanded.

**2026-04-26** — Events pipeline launched (5 adapters, orchestrator, weekly cron). Jobs weekly orchestrator and archive sweep shipped. `job_function` classifier field added. LinkedIn/Serper adapter replaced Google CSE.

**2026-04-21** — LinkedIn news draft prompt hardened with company-hallucination guardrails.

**2026-04-18** — News pipeline Supabase integration (upsert, OG metadata, publisher extraction). SendGrid `sportsd3c0d3d.ie` domain authenticated.
