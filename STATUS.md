# STATUS.md — sportstech-digest

*Last updated: 2026-07-14*

Rolling log of changes and open issues. Most recent session first.

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
