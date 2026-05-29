# STATUS.md — sportstech-digest

*Last updated: 2026-05-28*

Rolling log of changes and open issues. Most recent session first.

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

**LinkedIn adapter does NOT call `mark_source_successful` / `mark_source_attempted`.** The `finally` block from `base.py`'s `run()` that handles these calls is absent from `linkedin.py`'s `run()` override. Effect: `last_successful_scrape_at` and `last_scrape_run_at` on `company_careers_sources` rows are never updated for LinkedIn sources. The archive sweep's health gate uses `last_successful_scrape_at` to decide which sources to process, so LinkedIn-sourced jobs are silently skipped by every sweep. This compounds with today's `last_seen_in_scrape_run` fix: data is now stamped correctly per job, but the sweep still won't fire against LinkedIn sources. Also explains why ggCircuit and Orreco show as "never scraped" in the weekly email despite the adapter clearly running against them. Fix pattern next session: copy `base.py`'s `finally` block into `linkedin.py`'s `run()` override, same approach as today's `last_seen` fix.

**Kitman Labs duplicate job insertion.** Same job titles appear with both `approved` and `rejected` status, timestamps within 1 second. Suggests the upsert key is not uniquely resolving on URL, or the RPC is not deduplicating correctly. Pre-existing, not related to today's work. Investigate before next Friday's run.

**`rejected_reason` inconsistency in older jobs.** The `rejected_reason` text column and the `rejected_reason` field inside `classification` JSONB appear inconsistent for some older rows (Kitman Labs jobs have null in both despite having classification data). The write path in `run_classifier.py` sets both, but older jobs may have been written before that field existed. Worth auditing.

**Pending queue composition skew.** After today's cleanup the queue is ~100% allowlisted FDIs. Indigenous Irish companies are not generating pending jobs — either no new scrapes this week or Haiku is too strict on `not_sportstech` for indigenous companies. Monitor over the next 2–3 Friday runs; if trend continues, consider Haiku prompt tuning.

**Boylesports Teamtailor returned 0 jobs this week.** Verified the endpoint is live at `careers.boylesports.com/jobs.json` — genuinely empty board, not a scraping failure. Monitor next Friday.

**ggCircuit and Orreco show as "never scraped" in email.** LinkedIn adapter ran and returned `serper_no_results` for both. The LinkedIn adapter does not call `mark_source_attempted` on Serper-no-results (only `_update_source_error` with `last_scrape_error`). `last_scrape_run_at` is never set, so the snapshot treats them as never scraped. Cosmetic issue — the adapter did run. Fix: call `mark_source_attempted` in the `_SerperNoResultsError` handler in `linkedin.py:run()`.

**Rippling adapter runs against 0 sources.** Logs a line each week. `rippling` is not in the `ats_platform` CHECK constraint — cannot add sources without `ALTER TABLE`. If Rippling support is needed (Thrive Global, others), add to constraint first.

**Phenom adapter also runs against 0 sources.** Same cleanup opportunity.

**Greenhouse Harvest API deprecation August 2026.** The Harvest API (v1/v2) is being deprecated. The Job Board API (`boards-api.greenhouse.io`) which this pipeline uses is NOT affected. Note for awareness only.

**Potential `_check_fdi_geography_allowlisted` URL parsing edge case.** The office normalisation uses `.replace('-', ' ').replace('---', ' ')` — the triple-dash replace runs after single-dash replace and is therefore a no-op (all dashes already converted to spaces). A Workday path like `/job/Remote---Bulgaria/` becomes `"remote   bulgaria"` (three spaces), which would NOT match the `'remote bulgaria'` reject marker. If DraftKings Bulgaria jobs slip through next Friday this is why. Cleaner fix: `re.sub(r'-+', ' ', office)`.

---

## Next Session Candidates

- Validate new geography function and LinkedIn staleness check on next Friday's run. Expected pending queue: 30–80 jobs.
- Fix `ggCircuit` / `Orreco` "never scraped" cosmetic issue — call `mark_source_attempted` in `_SerperNoResultsError` handler.
- Fix `remote---bulgaria` URL slug edge case — replace `.replace('-', ' ').replace('---', ' ')` with `re.sub(r'-+', ' ', office)`.
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
