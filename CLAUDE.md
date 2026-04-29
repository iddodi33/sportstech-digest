# CLAUDE.md — sportstech-digest

*Last updated: 29 April 2026*

---

## What This Repo Is

A Python research and scraping pipeline that powers Sports D3c0d3d's intelligence operations. Three responsibilities:

1. **News pipeline**: scrapes Irish sportstech news, scores articles with Claude Sonnet 4.5, emails alerts and a monthly research markdown, writes scored articles to the hub Supabase.
2. **Jobs pipeline**: scrapes weekly job listings from 11 platforms (10 ATS + LinkedIn fallback), classifies via rule-based filters + Haiku 4.5, archives stale jobs, writes to the hub Supabase.
3. **Events pipeline**: scrapes weekly events from 5 sources, extracts structured event data via Claude Sonnet 4.5, writes pending events to hub Supabase for admin review.

Repo: `C:\coding_projects\sportstech-digest`
GitHub: https://github.com/iddodi33/sportstech-digest (branch: main)

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| HTTP | requests, httpx, cloudscraper (Cloudflare bypass for Business Post) |
| HTML parsing | BeautifulSoup4, lxml |
| Database | Supabase Python SDK / direct REST API |
| AI | Anthropic SDK: Sonnet 4.5 (news scoring, events extraction), Haiku 4.5 (jobs classification) |
| Email | SendGrid (free trial expires 29 May 2026) |
| Search | Serper API (Google SERP wrapper) for LinkedIn jobs adapter |
| Scheduling | GitHub Actions cron |

---

## Repo Structure
sportstech-digest/
daily_monitor.py                    News: daily 9am UTC alert with LinkedIn drafts
digest.py                           News: monthly 1st research markdown email
news_pipeline.py                    News: RSS + direct scraping (62 Google News + 9 site RSS feeds + Supabase company queries)
enhanced_sportstech_job_scraper_v3.py    Legacy job CSV scraper
supabase_client.py                  News: writes scored articles to hub Supabase
jobs_pipeline/                      Weekly jobs scraper (Friday 06:00 UTC)
init.py
supabase_jobs_client.py
classifier.py                     Rule-based pre-filter + Haiku classifier with job_function
run_classifier.py
run_reclassify_all.py
run_archive_sweep.py
run_weekly.py
adapters/
init.py
base.py
greenhouse.py, ashby.py, lever.py, personio.py, breezy.py
bamboohr.py, teamtailor.py, workday.py, rippling.py, phenom.py
linkedin.py                     Serper-based LinkedIn fallback
weekly/
init.py
runner.py
snapshot.py
email_builder.py
sendgrid_client.py
run_<platform>.py                 Per-platform entry points
events_pipeline/                    Weekly events scraper (Friday 06:00 UTC)
init.py
extractor.py                      HTML → cleaned HTML → Claude → structured event JSON
supabase_events_client.py         upsert_event RPC + fallback
test_extractor.py
run_weekly_events.py
adapters/
init.py
base.py
sport_for_business.py
eventbrite_ireland.py
meetup.py
irish_diversity_in_tech.py
ai_tinkerers_dublin.py
weekly/
init.py
runner.py
snapshot.py
email_builder.py
sendgrid_client.py
jobs_discovery/                     One-off discovery scripts (career_pages.csv seeding)
research/                           Monthly news markdown output
.github/workflows/
daily_monitor.yml                 News: daily 9am UTC cron
monthly.yml                       News: monthly 1st cron
jobs_weekly.yml                   Jobs: Friday 06:00 UTC cron
events_weekly.yml                 Events: Friday 06:00 UTC cron

---

## Environment Variables
ANTHROPIC_API_KEY                     Sonnet for news+events, Haiku for jobs
ADZUNA_APP_ID, ADZUNA_APP_KEY         Legacy job scraper
SENDGRID_API_KEY                      Email send (trial expires 29 May 2026)
ALERT_FROM=monitor@sportsd3c0d3d.ie   (hardcoded in jobs and events workflow files)
ALERT_TO=iddodiamant@gmail.com        (hardcoded in jobs and events workflow files)
ALERT_CC                              Optional comma-separated CC for daily news alerts
NEXT_PUBLIC_SUPABASE_URL=https://xwqmnofkvdwpagfweqmj.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY         Informational
SUPABASE_SERVICE_ROLE_KEY             Required for upserts to hub
SERPER_API_KEY                        LinkedIn jobs adapter (free tier 2,500/month)

GitHub Actions secrets must include all of the above. ALERT_CC is optional — if unset, no CC is added.

---

## News Pipeline

### Sources (as of 29 April 2026)

**Direct site RSS (9 feeds):**

- High quality (CAP_HIGH=15): siliconrepublic.com, sportforbusiness.com, thinkbusiness.ie, businessplus.ie, techcentral.ie, bebeez.eu (Ireland/UK title-filtered)
- Tech news tier (CAP_TECH_NEWS=10, sport-keyword-filtered): irishtechnews.ie (direct feed at https://irishtechnews.ie/feed/, switched away from Feedburner 28 April after Stegawave miss)
- Direct scrape: enterprise-ireland.com (custom parser), businesspost.ie (cloudscraper bypass)
- RSS-then-scrape: thinkbusiness.ie, sportireland.ie

**Google News queries (62 feeds):**

- Ireland-specific sportstech keywords (11 queries)
- Named Irish companies and ecosystem people (28 queries)
- Named entity sport+tech queries (12 queries)
- Europe sportstech (2 queries)
- Source-name keyword queries for Irish nationals (4 queries via Google News index)
- Business Post site queries (3 redundant coverage queries)
- Legal/regulatory governance queries (positions 50-53): DPC, EU AI Act, Project Red Card, sport governance technology

**Supabase company feeds:** queries the hub `companies` table for `is_irish_founded=true AND is_fdi=false`, returns up to 150 companies sorted alphabetically by name. Each becomes a Google News query with quote-wrapped name + Ireland. Skipped on Windows due to SSL slowness; runs full on ubuntu-latest CI. Supabase sub-timeout: 60s.

### Source quality tiers
HIGH_QUALITY_SOURCES   → CAP_HIGH=15
MEDIUM_QUALITY_SOURCES → CAP_MEDIUM=5
LOW_QUALITY_SOURCES    → CAP_LOW=3
TECH_NEWS_SOURCES      → CAP_TECH_NEWS=10 (sport-keyword filtered)
BROADSHEET_SOURCES     → CAP_BROADSHEET=5 (sportstech-keyword filtered)
GOOGLE_NEWS_FEEDS      → CAP_GOOGLE_NEWS=10
businesspost.ie        → CAP_BUSINESSPOST=10

### Scoring (Claude Sonnet 4.5)

| Score | Meaning |
|-------|---------|
| 5 | Irish sportstech company — funding, product launch, award, expansion |
| 4 | Irish sports org adopting tech, Irish sportstech person, Irish adjacent. Also: Irish legal/regulatory developments with direct impact on Irish sportstech (DPC guidance, AI Act enforcement, athlete data rulings, NGB tech regulation). |
| 3 | European sportstech news relevant to Irish audience. Also: Irish legal, regulatory, or governance commentary on sport (Law Society Gazette, DPC, EU AI Act, athlete data rights, Project Red Card) directly relevant to Irish sportstech compliance. |
| 2 | Irish sports without tech angle, operations roles |
| 1 | Off-topic, no sports angle, duplicate |

Email alerts fire for **score 3+**. Hub Supabase upserts at **score 3+**.

Scoring prompt returns JSON per article: score, score_reason, summary (2 sentences, 40-60 words), tags, verticals, mentioned_companies. Closed vertical list must match hub exactly.

### LinkedIn draft prompt

LINKEDIN_SYSTEM prompt has company-hallucination guardrails embedded directly (added 21 April 2026 after STATSports/concussion-tech false claim). Per-company capability facts: STATSports GPS only, Orreco biomarkers only, Output Sports IMU movement only, Kitman Labs software only, Hexis nutrition software only, Danu Sports smart textiles only. Verify-before-naming gate. Éanna Falvey person-vs-company distinction. Empty Irish-company reference is the correct default when nothing genuinely fits.

### Google News URL resolution

Google News RSS returns proxied URLs. Decoded via googlenewsdecoder. Fallback to original URL on failure.

### OG metadata extraction

`fetch_og_metadata(url)` in `supabase_client.py` extracts og:image and og:title. Used to enrich score 3+ articles being upserted.

### Publisher name extraction

`extract_publisher(url)` maps domains to clean publisher names via dictionary, with multi-part TLD handling (.co.uk, .com.au, .co.ie, etc.).

---

## Jobs Pipeline

Weekly Friday 06:00 UTC scrape. Adapters are dumb transport: fetch, normalise, write. Classification happens downstream via Haiku 4.5.

Adapters: greenhouse, ashby, lever, personio, breezy, bamboohr, teamtailor, workday, rippling, phenom, linkedin (Serper).

LinkedIn adapter: Serper API replaces Google CSE (closed) and Bing API (retired Aug 2025). Free tier 2,500 queries/month, ~55/week used. Domain filter for FDI vs indigenous Irish.

Classifier fields: seniority, employment_type, remote_status, vertical, location_normalised, sportstech_relevance, sportstech_relevance_reason, job_function (8 valid values + null), classification_reasoning.

Archive sweep marks status='archived' if `last_seen_in_scrape_run` is older than `(source.last_successful_scrape_at - 8 days)` AND source health gate passes (source scraped successfully in past 8 days).

Weekly orchestrator (`run_weekly.py`) runs all 11 adapters → classifier → archive sweep → SendGrid summary email. CLI flags: --skip-adapters, --skip-email.

Coverage: 73 sources across 11 platforms. ~640 jobs scraped per full run, ~30-50 added to pending after classifier filtering.

---

## Events Pipeline

Weekly Friday 06:00 UTC. 5 adapters discover URLs, orchestrator dedupes globally, extractor classifies each URL via Sonnet 4.5, relevant ones upsert to hub events with status='pending' for admin review.

| Adapter | URLs/run |
|---|---|
| sport_for_business | 10-20 |
| eventbrite_ireland | 30 (capped) |
| meetup | 5-15 |
| irish_diversity_in_tech | 10-20 (allowlist filtered to meetup, eventbrite, lu.ma, hopin, airmeet) |
| ai_tinkerers_dublin | 0 (Cloudflare 403 blocked) |

Total expected: 60-100 unique URLs per weekly run.

Extractor returns: name, date, end_date, start_time, location, area, format, organiser, description, image_url, recurrence, relevance_category (sportstech | ai_tech_ireland | startup_opportunity | not_relevant), relevance_reason, extraction_confidence.

---

## Hub Supabase Integration

Hub project: xwqmnofkvdwpagfweqmj.

### RPCs (read-only, never modify signatures)

- `upsert_news_item_if_higher_score` (12 args)
- `upsert_job` (10 args)
- `upsert_event_if_new` (14 args)

### Direct UPDATEs from this repo

- `jobs.job_function` (set by classifier and reclassify-all script)
- `jobs.last_seen_in_scrape_run` (set by adapters after successful upsert)
- `company_careers_sources.last_successful_scrape_at`, `last_scrape_run_at`

---

## GitHub Actions

| Workflow | Schedule | Purpose |
|---|---|---|
| daily_monitor.yml | 0 9 * * * | News alerts |
| monthly.yml | 0 9 1 * * | Monthly research |
| jobs_weekly.yml | 0 6 * * 5 | Jobs orchestrator |
| events_weekly.yml | 0 6 * * 5 | Events orchestrator |

All four also support workflow_dispatch for manual triggering.

---

## Run Patterns

```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1

# News pipeline
python daily_monitor.py
python digest.py

# Jobs pipeline (single adapter)
python jobs_pipeline/run_greenhouse.py
python jobs_pipeline/run_linkedin.py --dry-run --company "Hexis"

# Jobs classifier and archive sweep
python jobs_pipeline/run_classifier.py
python jobs_pipeline/run_archive_sweep.py --dry-run
python jobs_pipeline/run_archive_sweep.py

# Jobs full weekly orchestrator
python jobs_pipeline/run_weekly.py
python jobs_pipeline/run_weekly.py --skip-adapters --skip-email
python jobs_pipeline/run_weekly.py --skip-email

# Events pipeline (test single URL)
python events_pipeline/test_extractor.py "<url>"
python events_pipeline/test_extractor.py "<url>" --upsert

# Events full weekly orchestrator
python events_pipeline/run_weekly_events.py
python events_pipeline/run_weekly_events.py --skip-email
python events_pipeline/run_weekly_events.py --skip-email --limit 5
python events_pipeline/run_weekly_events.py --source meetup --skip-email
```

---

## Do Not Change

- The daily news email with LinkedIn draft (fires for score 3+)
- The monthly news email with markdown attachment
- `daily_monitor_seen.json` dedup logic
- 1-5 news scoring criteria for scores 1, 2, 5
- LINKEDIN_SYSTEM company-hallucination guardrails
- The upsert_job RPC signature (10 args)
- The upsert_news_item_if_higher_score RPC signature (12 args)
- The upsert_event_if_new RPC signature (14 args)

---

## Open Issues & Backlog

### Operational, time-sensitive

1. **SendGrid trial expires 29 May 2026.** All four workflows depend on it.
2. **Verify governance feed coverage on CI.** Local Windows runs hit the 300s timeout before reaching governance queries (positions 50-53 of 62). Linux ubuntu-latest is faster — confirm coverage in next 2-3 weekly runs.
3. **Direct RSS feeds for Law Society Gazette, DPC, Irish Legal returned 404 on 28 April fix attempt.** Currently relying on Google News fallback queries. Worth manually finding correct feed URLs if governance content stays thin after a few weeks.

### Engineering polish

1. Classifier prompt tuning for over-eager rejections.
2. Eventbrite events search URL tuning to reduce children's-sports-day noise.
3. AI Tinkerers Dublin Cloudflare bypass (Playwright or curl_cffi).
4. Eventbrite OG image post-processing (resolve Next.js proxy URLs to underlying CDN).
5. Fuzzy event dedup by name + date.
6. Periodic sweep for jobs with NULL job_function after Haiku credit exhaustion.
7. **Supabase company query timeout: 60s sub-timeout allows ~7-8 of 111 companies locally.** On CI more complete but probably not all 111. Consider raising sub-timeout to 180s on CI runner or batching queries.

---

## Recent Changes Log

### 29 April 2026, daily_monitor CC support

- `daily_monitor.yml` workflow now passes `ALERT_CC` secret to the runtime
- `daily_monitor.py` already parses `ALERT_CC` as comma-separated, calls `message.add_cc()` for each address
- Empty/unset `ALERT_CC` is a no-op via `if alert_cc:` guard

### 28 April 2026, news pipeline tuning (4 commits)

**Task 1 — irishtechnews.ie fix.** Switched from Feedburner to direct RSS feed (`https://irishtechnews.ie/feed/`). Created TECH_NEWS_SOURCES tier with sport-only keyword filter and CAP_TECH_NEWS=10. Resolves the Stegawave miss (13 April article that was invisible to scraper).

**Task 2 — Legal/governance feeds.** Added 4 Google News queries at positions 50-53: DPC + data protection, EU AI Act + Ireland sport, Project Red Card, sport governance technology. Direct RSS attempts for Law Society Gazette, DPC, Irish Legal returned 404 — Google News fallback used.

**Task 3 — Supabase company expansion.** `get_supabase_company_feeds()` limit raised from 30 to 150. Order changed from `total_funding.desc.nullslast` to `name.asc` for deterministic coverage. 111 indigenous Irish companies now queried, including Stegawave at position 97.

**Task 4 — Scoring prompt governance.** Score 3 expanded to include Irish legal/regulatory commentary on sport. Score 4 expanded to include Irish legal/regulatory developments with direct impact on Irish sportstech. Scores 1, 2, 5 unchanged.

### 26 April 2026, events orchestrator + adapters + cron

- 5 source adapters: sport_for_business, eventbrite_ireland, meetup, irish_diversity_in_tech, ai_tinkerers_dublin
- BaseEventAdapter pattern with shared URL utilities
- run_weekly_events.py orchestrator with global URL dedup
- weekly/ subpackage: runner, snapshot, email_builder, sendgrid_client
- .github/workflows/events_weekly.yml Friday 06:00 UTC cron

### 26 April 2026, jobs weekly orchestrator + cron

- run_weekly.py sequential orchestrator
- weekly/ subpackage with SendGrid HTML summary email
- --skip-adapters and --skip-email CLI flags
- Classifier credit-exhaustion handling
- jobs_weekly.yml Friday 06:00 UTC cron

### 26 April 2026, jobs archive sweep

- run_archive_sweep.py with --dry-run flag
- Schema additions: jobs.last_seen_in_scrape_run, company_careers_sources.last_successful_scrape_at, last_scrape_run_at
- Source health gate prevents broken adapters from archiving everything

### 26 April 2026, job_function classifier + backfill

- Classifier prompt extended with job_function field (8 valid values + null)
- run_reclassify_all.py for one-off backfill

### 25 April 2026, LinkedIn adapter (Serper) + jobs pipeline live

- Serper API replaces googlesearch-python and Google CSE
- linkedin_search_name override column for rebrand cases
- Domain filter for FDI vs indigenous Irish

### 21 April 2026, LinkedIn news draft prompt hardening

- Company hallucination guardrails added to LINKEDIN_SYSTEM prompt
- Per-company capability facts embedded inline

### 18 April 2026, news pipeline integration

- supabase_client.py with upsert_news_item, fetch_og_metadata, extract_publisher
- Daily/monthly news pipelines upsert score 3+ to hub
- SendGrid sportsd3c0d3d.ie domain authenticated