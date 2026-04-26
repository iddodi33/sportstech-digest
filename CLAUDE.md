# CLAUDE.md — sportstech-digest

*Last updated: 26 April 2026*

---

## What This Repo Is

A Python research and scraping pipeline that powers Sports D3c0d3d's intelligence operations. Four responsibilities:

1. **News pipeline** — scrapes Irish sportstech news, scores articles with Claude Sonnet, emails alerts and a monthly research markdown, writes scored articles to the hub Supabase
2. **Jobs pipeline** — scrapes weekly job listings from 11 platforms (10 ATS + LinkedIn fallback), classifies via rule-based filters + Haiku, writes to the hub Supabase
3. **Events pipeline** — extracts structured event data from HTML using Claude Sonnet 4.5, writes to the hub Supabase events table. Session 1 (extractor + test CLI) shipped 26 April 2026; Session 2 (source adapters + orchestrator + cron) is next.
4. **Job scraping (legacy)** — `enhanced_sportstech_job_scraper_v3.py`, separate from the new pipeline, writes CSV

Repo: `C:\coding_projects\sportstech-digest`
GitHub: https://github.com/iddodi33/sportstech-digest (branch: main)

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| HTTP | requests, httpx |
| HTML parsing | BeautifulSoup4 |
| Database | Supabase Python SDK |
| AI | Anthropic SDK — Sonnet 4.5 (news), Haiku 4.5 (jobs) |
| Email | SendGrid (free trial expires 29 May 2026) |
| Search | Serper API (Google SERP wrapper) — used by LinkedIn adapter |
| Scheduling | GitHub Actions cron |

---

## Repo Structure

```
sportstech-digest/
  daily_monitor.py                    News: daily 9am UTC alert with LinkedIn drafts
  digest.py                           News: monthly 1st-of-month research markdown email
  news_pipeline.py                    News: RSS + direct scraping
  enhanced_sportstech_job_scraper_v3.py    Legacy job CSV scraper
  supabase_client.py                  News: writes scored articles to hub Supabase

  jobs_pipeline/                      Weekly job scraper + orchestrator
    __init__.py
    supabase_jobs_client.py           Singleton client, get_active_sources(), upsert_job() RPC, mark_job_seen(), mark_source_*()
    classifier.py                     Rule-based pre-filter + Haiku classifier (incl. job_function)
    run_classifier.py                 Entry point for classification pass
    run_reclassify_all.py             One-off backfill script for job_function on existing jobs
    run_archive_sweep.py              Archive stale jobs (2+ missed weekly runs, source health gate)
    run_weekly.py                     Weekly orchestrator: all adapters → classifier → sweep → email
    adapters/
      __init__.py
      base.py                         BaseAdapter (tracks last_seen_in_scrape_run + source timestamps)
      greenhouse.py, ashby.py, lever.py, personio.py, breezy.py
      bamboohr.py, teamtailor.py, workday.py, rippling.py, phenom.py
      linkedin.py                     Serper-based LinkedIn fallback adapter
    run_<platform>.py                 Per-platform entry points
    run_linkedin.py                   --dry-run, --company flags
    weekly/                           Orchestrator helper package
      __init__.py
      runner.py                       Step execution + result capture (adapters direct, classifier/sweep subprocess)
      snapshot.py                     DB snapshot queries (job counts, sources never scraped)
      email_builder.py                HTML email construction
      sendgrid_client.py              SendGrid send wrapper

  events_pipeline/                    Events extractor (Session 1: extractor + test CLI)
    __init__.py
    supabase_events_client.py         Singleton client, upsert_event() RPC + fallback
    extractor.py                      fetch_html, clean_html, extract_with_claude, extract_event()
    test_extractor.py                 CLI: python events_pipeline/test_extractor.py <url> [--upsert]

  jobs_discovery/                     One-off discovery scripts (career_pages.csv seeding)
    career_pages.csv                  74-row source-of-truth for company_careers_sources
    discover_career_pages.py
    discover_second_pass.py
    import_to_supabase.py
    README.md

  research/                           Monthly markdown output
  .github/workflows/
    daily_monitor.yml                 News: daily 9am UTC cron
    monthly.yml                       News: monthly 1st-of-month cron
    jobs_weekly.yml                   Jobs: Friday 06:00 UTC cron + workflow_dispatch
```

---

## Environment Variables

```
ANTHROPIC_API_KEY                     Sonnet for news, Haiku for jobs (auto-top-up enabled)
ADZUNA_APP_ID, ADZUNA_APP_KEY         Legacy job scraper
SENDGRID_API_KEY                      News email send
ALERT_FROM=monitor@sportsd3c0d3d.ie
ALERT_TO=iddodiamant@gmail.com
NEXT_PUBLIC_SUPABASE_URL=https://xwqmnofkvdwpagfweqmj.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY         Informational
SUPABASE_SERVICE_ROLE_KEY             Required for upserts to hub
SERPER_API_KEY                        LinkedIn adapter Google SERP queries (free tier 2,500/month)
```

GitHub Actions secrets must include all of the above.

---

## Jobs Pipeline (live as of 26 April 2026)

### Architecture

- Weekly scrape (Sunday/Monday UTC). Adapters are dumb transport: fetch, normalise, write. Classification happens downstream.
- All adapters write via `upsert_job` RPC (10 args). Idempotent: dedup by URL, preserves first_seen_at, updates last_seen_at, never regresses status.
- Coverage: 73 sources across 11 platforms. ~640 jobs scraped per full run, ~30-50 added to pending after classifier filtering.

### LinkedIn adapter (NEW 25 April 2026)

- Replaces googlesearch-python (rate-limited) and Google CSE (closed to new customers)
- Uses Serper API: free tier 2,500 queries/month; we use ~55/week (220/month)
- Two-stage flow:
  1. Serper SERP query: `site:linkedin.com/jobs/view "Company Name"` (FDI: append " Ireland")
  2. LinkedIn page fetch: rotating UA, full headers, throttle 1.5-2.5s between fetches, 60-90s pause + fresh Session every 25 fetches, abort on 3 consecutive 999/429
- Domain filter: indigenous Irish accept ie/www; FDIs accept ie only
- JSON-LD primary parser, BS4 fallback
- Name validation via _normalise_company_name (strips Ltd/Limited/Inc/etc); override bypass via `linkedin_search_name` column
- Currently overriding: Danu Sport → "Danu Sports", Clubforce → "Clubforce®"
- Acceptable expected failure modes: occasional 999/429 (skip and continue), serper_no_results (legitimate, skip)

### Classifier (`classifier.py` + `run_classifier.py`)

Rule-based pre-filter (run before Haiku):

1. **Junior keyword reject** — word-boundary regex
2. **FDI geography reject** — Ireland whitelist + reject patterns. **Numeric N Locations regex** `\b\d+ locations?\b` (case-insensitive, replaces fixed list)
3. **Sportstech relevance reject** (after Haiku) for `sportstech_relevance == 'not_sportstech'`

Haiku 4.5 classification fields:

- seniority, employment_type, remote_status, vertical, location_normalised, sportstech_relevance, sportstech_relevance_reason
- **job_function** (Workstream A2, 26 April 2026) — 8 valid values + null
- classification_reasoning

Field normalisation handles enum drift. job_function returns null for unmapped values with a warning log.

### Backfill script (`run_reclassify_all.py`)

- Idempotent: SELECT WHERE job_function IS NULL
- Skips rule-based filter (preserves existing status/rejected_reason)
- 0.5s sleep between Haiku calls
- Confirmation prompt before processing
- Last run: 569/688 successfully classified, 102 returned null, ~80 final NULL across DB (mostly FDI-rejected).

---

## News Pipeline (existing, unchanged 26 April 2026)

Daily flow (9am UTC): RSS scrape → Sonnet 4.5 score → upsert score 3+ to hub → email alert with LinkedIn drafts.
Monthly flow (1st of month, 9am UTC): scoring + research markdown + hub upsert + emailed attachment.
Closed vertical list matches hub. OG image extraction, publisher name extraction, Google News URL decoding via googlenewsdecoder.
LinkedIn draft prompt has company-hallucination guardrails (added 21 April after STATSports/concussion-tech false claim).

---

## Hub Supabase Integration

Hub project: xwqmnofkvdwpagfweqmj.

### RPCs (read-only, never modify signatures)

- `upsert_news_item_if_higher_score` (12 args)
- `upsert_job` (10 args) — preserves first_seen_at, updates last_seen_at, returns (id, was_inserted, was_reactivated)

### Direct UPDATEs from this repo

- `jobs.job_function` — set by classifier and reclassify-all script via direct UPDATE (not via RPC). Column added to hub schema 26 April 2026 by Workstream A1.
- `jobs.last_seen_in_scrape_run` — stamped by `base.py` after every successful upsert_job call (Workstream 3).
- `company_careers_sources.last_successful_scrape_at` — stamped by `base.py` when a source run upserts ≥1 job (Workstream 3).
- `company_careers_sources.last_scrape_run_at` — stamped by `base.py` after every source run, success or fail (Workstream 3).

### New columns on hub schema (Workstream 3, 26 April 2026)

Run `supabase/migrations/20260426_archive_sweep.sql` manually in the Supabase SQL editor.

```
jobs.last_seen_in_scrape_run                  timestamptz, nullable — when this job was last seen by any adapter
company_careers_sources.last_successful_scrape_at  timestamptz, nullable — last run that returned ≥1 job
company_careers_sources.last_scrape_run_at         timestamptz, nullable — last run attempted (pass or fail)
```

---

## GitHub Actions

- `.github/workflows/daily_monitor.yml` — `daily_monitor.py` at 9am UTC daily; commits `daily_monitor_seen.json`
- `.github/workflows/monthly.yml` — full news pipeline on 1st of month at 9am UTC
- `.github/workflows/jobs_weekly.yml` — `run_weekly.py` at 06:00 UTC every Friday; timeout 90 min; all 7 secrets injected

---

## Run Patterns

```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1   # Windows

# Run a single ATS adapter
python jobs_pipeline/run_greenhouse.py
python jobs_pipeline/run_linkedin.py
python jobs_pipeline/run_linkedin.py --dry-run --company "Hexis"
python jobs_pipeline/run_linkedin.py --company "Clubforce"

# Run classifier on pending jobs
python jobs_pipeline/run_classifier.py

# One-off backfill for job_function
python jobs_pipeline/run_reclassify_all.py

# Archive sweep — dry-run first, then live
python jobs_pipeline/run_archive_sweep.py --dry-run
python jobs_pipeline/run_archive_sweep.py

# Extract a single event URL (read-only, no DB write)
python events_pipeline/test_extractor.py <url>

# Extract and upsert to hub Supabase (source='test')
python events_pipeline/test_extractor.py <url> --upsert

# Full weekly orchestrator (adapters + classifier + sweep + email)
python jobs_pipeline/run_weekly.py

# Preview email only, skip sending (fast iteration)
python jobs_pipeline/run_weekly.py --skip-adapters --skip-email

# Full run, preview email without sending
python jobs_pipeline/run_weekly.py --skip-email
```

---

## Do Not Change

- The daily news email with LinkedIn draft (fires for score 3+)
- The monthly news email with markdown attachment
- `daily_monitor_seen.json` dedup logic
- 1-5 news scoring criteria
- `LINKEDIN_SYSTEM` company-hallucination guardrails (added 21 April)
- The `upsert_job` RPC signature (10 args)
- The `upsert_news_item_if_higher_score` RPC signature (12 args)

---

## Open Issues & Next Steps

### Immediate (next session — building on today's work)

1. ~~**Workstream 3 — Archive sweep**~~ — shipped 26 April 2026 (see Recent Changes Log)
2. ~~**Workstream 4 — Weekly orchestrator**~~ — shipped 26 April 2026 (see Recent Changes Log)
3. ~~**Workstream 5 — GitHub Actions weekly cron**~~ — shipped 26 April 2026 (see Recent Changes Log)

All five workstreams are now complete. The jobs pipeline is fully autonomous: adapters scrape Friday 06:00 UTC → classifier runs → archive sweep runs → summary email sent to iddodiamant@gmail.com. Subscribe to GitHub Actions failure email notifications in repo Settings → Notifications so silent crashes surface immediately.

### Backlog (post-orchestrator)

- **Classifier prompt tuning** — the `\d+ locations?` regex catches future numeric variants but Haiku's verbose responses still bin too aggressively. Tighten the strict-enum instruction for job_function so fewer responses get normalised to None.
- **Junior keyword regex over-eager**: "Data Architect | 3-Month Contract" rejected as too_junior; "HubSpot Specialist" rejected. Word-boundary tightening on suffix patterns.
- **Sportstech relevance over-strict**: "Bookkeeper at Sport Endorse" rejected as not_sportstech. Should accept back-office at sportstech companies.
- **17 jobs from first backfill failed Haiku** (credit exhaustion) — already retried successfully on top-up.
- **SendGrid free trial ends 29 May 2026** — paid upgrade needed.
- **Off The Ball override** is a blunt instrument: 6 of 10 jobs are Bauer Media parent-company false positives. Acceptable noise; document for admin review pattern.

---

## Recent Changes Log

### 26 April 2026 — Workstream E2 Session 1 (events extraction infrastructure)

- New package `events_pipeline/` with `supabase_events_client.py`, `extractor.py`, `test_extractor.py`
- `extractor.py`: `fetch_html` (OG image extraction), `clean_html` (BeautifulSoup, strips noise, targets main/article), `extract_with_claude` (Claude Sonnet 4.5, temperature=0, max_tokens=1500, JSON parse with fence-strip fallback), `extract_event` (public entry point, adds `url` to result)
- `supabase_events_client.py`: singleton Supabase client, `upsert_event()` calls `upsert_event_if_new` RPC then falls back to manual SELECT + INSERT/UPDATE (mirrors news upsert pattern)
- `test_extractor.py`: `<url> [--upsert]` CLI, read-only by default, validates env vars at startup, prints JSON + summary, skips upsert for `not_relevant` results
- New migration in hub repo: `supabase/migrations/20260426_events_rpc.sql` — `upsert_event_if_new` Postgres function (SECURITY DEFINER), updates content fields only when status='pending'
- 3 relevance categories: sportstech, ai_tech_ireland, startup_opportunity; not_relevant for everything else
- Session 2 remaining: source adapters (Sport for Business, Eventbrite Ireland, etc.) + orchestrator + cron

### 26 April 2026 — Workstream 5 (GitHub Actions weekly cron)

- New workflow: `.github/workflows/jobs_weekly.yml` — Friday 06:00 UTC cron + `workflow_dispatch`
- `runs-on: ubuntu-latest`, `timeout-minutes: 90`, Python 3.11, pip cache
- Actions versions: `actions/checkout@v4`, `actions/setup-python@v5`
- All 7 secrets injected as env vars on the run step: ANTHROPIC_API_KEY, SENDGRID_API_KEY, ALERT_FROM, ALERT_TO, NEXT_PUBLIC_SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SERPER_API_KEY
- Jobs pipeline is now fully autonomous end-to-end

### 26 April 2026 — Workstream 4 (weekly orchestrator)

- New package `jobs_pipeline/weekly/` with `runner.py`, `snapshot.py`, `email_builder.py`, `sendgrid_client.py`
- New entry point `jobs_pipeline/run_weekly.py` — `--skip-adapters` and `--skip-email` flags
- Adapter steps: direct import (structured stats returned, no parsing); LinkedIn abort + close handled
- Classifier + archive sweep: subprocess + regex parse of stdout/stderr for structured results
- Credit exhaustion detection: looks for "429", "rate_limit_error", "credit balance" in combined output → `credit_exhausted` status distinct from `failed`
- Email sections: headline, pipeline state snapshot, adapter table, adapter errors, classifier, archive sweep, companies needing attention
- Failure model: per-adapter exception caught + logged, run continues; SendGrid failure → stdout dump + exit 1
- DB snapshot queries: approved/pending/archived counts, pending-null-function count, sources-never-scraped list

### 26 April 2026 — Workstream 3 (archive sweep)

- New migration: `supabase/migrations/20260426_archive_sweep.sql` — adds `jobs.last_seen_in_scrape_run`, `company_careers_sources.last_successful_scrape_at`, `company_careers_sources.last_scrape_run_at`, and `idx_jobs_last_seen_in_scrape_run` partial index
- `supabase_jobs_client.py`: added `mark_job_seen()`, `mark_source_successful()`, `mark_source_attempted()`
- `adapters/base.py`: `run()` now records `run_started_at`, stamps `last_seen_in_scrape_run` after each successful upsert, and stamps source tracking columns in a `finally` block
- New script: `jobs_pipeline/run_archive_sweep.py` — `--dry-run` flag, source health gate (8-day window), 2-run grace period (8-day cutoff), NULL-row grace, per-source breakdown in summary
- Schema change: run the migration in Supabase SQL editor before the first adapter run

### 26 April 2026 — Workstream A2 (job_function classifier + backfill)

- Classifier prompt extended with job_function field (8 valid values + null)
- Field normalisation: invalid Haiku output → None with warning log
- New file: `run_reclassify_all.py` for one-off backfill of existing jobs
- Backfill processed 688 jobs (569 classified, 102 null, 17 credit-error → retried successfully on Anthropic credit top-up)
- Anthropic auto-top-up enabled

### 25 April 2026 — LinkedIn adapter (Serper) + jobs pipeline live

- Built LinkedIn fallback adapter using Serper API (after Google CSE / Bing API / Tavily proved unworkable)
- Added `linkedin_search_name` override column to company_careers_sources
- Override bypass logic in name validation
- Domain filter for FDI vs indigenous Irish
- First full live run: 54/54 companies, 70 jobs, 50 new inserted, zero 999s, zero aborts
- Workstream 1 (classifier regex tightening) shipped: `\d+ locations?` pattern replaces fixed numeric list

### 18 April 2026 — News pipeline integration (existing)

- supabase_client.py with upsert_news_item, fetch_og_metadata, extract_publisher
- Daily/monthly news pipelines upsert score 3+ to hub
- SendGrid sportsd3c0d3d.ie domain authenticated

### 21 April 2026 — LinkedIn draft prompt hardening (existing)

- Company hallucination guardrails added
