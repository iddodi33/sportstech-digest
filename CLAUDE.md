# CLAUDE.md — sportstech-digest

*Last updated: 2026-07-14*

---

## Project Purpose

`sportstech-digest` is the scraping and intelligence pipeline for Sports D3c0d3d. It feeds the hub Supabase project (`xwqmnofkvdwpagfweqmj`, West EU / Ireland) which powers the admin panel at sd3-intelligence-hub. Four responsibilities:

1. **News pipeline** — scrapes Irish sportstech news, scores with Claude Sonnet 4.5, emails daily alerts and a monthly research markdown, upserts score 3+ articles to the hub.
2. **Jobs pipeline** — scrapes weekly job listings from 10 ATS platforms plus two LinkedIn paths (Apify for `linkedin_only` companies, Serper discovery for `none_found` companies), classifies via rule-based pre-filter + relevance filter + Haiku 4.5, archives stale jobs, upserts to the hub.
3. **Events pipeline** — scrapes weekly events from 5 sources, extracts structured data via Claude Sonnet 4.5, upserts pending events to the hub for admin review.
4. **Weekly LinkedIn digest** — pulls score 3+ news from the past 7 days, picks top 5 with source and topic diversity, drafts a LinkedIn post. Email-only, no hub writes.

Repo: `C:\coding_projects\sportstech-digest`  
GitHub: https://github.com/iddodi33/sportstech-digest (branch: main)

---

## Repo Layout

```
sportstech-digest/
  daily_monitor.py               News: daily 9am UTC alert
  digest.py                      News: monthly 1st research email
  news_pipeline.py               News: RSS + Google News + Supabase company queries
  weekly_linkedin_digest.py      News: weekly LinkedIn post draft, Friday 12:00 UTC
  supabase_client.py             News: upserts scored articles to hub
  jobs_pipeline/                 Weekly jobs scraper (Friday 06:00 UTC)
    classifier.py                Rule pre-filter + Haiku classifier
    relevance_filter.py          Rule-based title noise filter, shared by both LinkedIn adapters
    run_classifier.py            Classify pending unclassified jobs
    run_reclassify_all.py        Backfill job_function on existing jobs
    run_archive_sweep.py         Archive stale jobs
    run_weekly.py                Full weekly orchestrator
    supabase_jobs_client.py      DB helpers (upsert, mark_seen, mark_source_*)
    adapters/                    One file per ATS platform + base.py (linkedin.py=Serper/none_found, apify_linkedin.py=Apify/linkedin_only)
    weekly/                      runner.py, snapshot.py, email_builder.py, sendgrid_client.py
    run_<platform>.py            Per-platform entry points
  events_pipeline/               Weekly events scraper (Friday 06:00 UTC)
    extractor.py                 HTML → Claude → structured event JSON
    run_weekly_events.py         Full weekly orchestrator
    adapters/                    5 event source adapters
    weekly/                      runner, snapshot, email, sendgrid
  jobs_discovery/                One-off career page discovery scripts
  research/                      Monthly news markdown output
  .github/workflows/             5 cron workflows (see GitHub Actions below)
  ARCHITECTURE.md                Schema, adapter quirks, classifier internals
  STATUS.md                      Recent changes log and open bugs
```

---

## Key Principles

- **Python for scripts; SQL for one-off DB operations.** Don't write a Python script when a `BEGIN/COMMIT` transaction with a preview `SELECT` achieves the same thing more safely.
- **Never assume column names.** Always view the schema or run `\d tablename` before writing queries against tables you haven't touched in this session.
- **Verify before destructive SQL.** Wrap in `BEGIN; <preview SELECT> / <UPDATE>; COMMIT;` — confirm row count before committing.
- **Keep CLAUDE.md files current across sessions.** After any substantive code or schema change, update CLAUDE.md / ARCHITECTURE.md / STATUS.md before ending the session.
- **PowerShell environment on Windows.** Use `&&` chaining via `;` instead, backtick for line continuation, `$env:VAR` for env vars.
- **FDI allowlist pattern.** When adding a new FDI company to the pipeline, set `fdi_classifier_allowlisted=true` on the `companies` row AND verify an active source exists in `company_careers_sources`. Do not assume a company row alone is sufficient.

---

## Where to Find What

| Topic | File |
|---|---|
| DB schema, adapter quirks, classifier rules, LinkedIn/Serper detail | `ARCHITECTURE.md` |
| Recent changes, open bugs, next-session candidates | `STATUS.md` |
| Run commands | This file (below) |
| Do-not-touch list | This file (below) |

---

## Environment Variables

```
ANTHROPIC_API_KEY                  Haiku for jobs; Sonnet for news + events
RESEND_API_KEY                     Email send
ALERT_FROM=monitor@sportsd3c0d3d.ie
ALERT_TO=iddodiamant@gmail.com
ALERT_CC                           Optional comma-separated CC (daily news alerts)
NEXT_PUBLIC_SUPABASE_URL=https://xwqmnofkvdwpagfweqmj.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY      Informational only
SUPABASE_SERVICE_ROLE_KEY          Required for all hub upserts
SERPER_API_KEY                     LinkedIn jobs adapter — none_found sources only (free tier 2,500/month)
APIFY_TOKEN                        LinkedIn jobs adapter — linkedin_only sources only (Apify LinkedIn Jobs Scraper actor). Optional: missing token degrades the linkedin_apify weekly step to a logged warning, does not abort the pipeline.
ADZUNA_APP_ID, ADZUNA_APP_KEY      Legacy CSV scraper only
```

GitHub Actions secrets must mirror all of the above. `ALERT_CC` is optional — omitting it is a no-op.

---

## External Resources

| Resource | Detail |
|---|---|
| Supabase hub | `xwqmnofkvdwpagfweqmj`, West EU (Ireland) |
| SendGrid | Sender domain `sportsd3c0d3d.ie` authenticated |
| Anthropic — jobs | `claude-haiku-4-5-20251001` |
| Anthropic — news/events | `claude-sonnet-4-5-20250929` |
| Serper | google.serper.dev, free tier, LinkedIn job URL discovery (`none_found` sources) |
| Apify | `curious_coder/linkedin-jobs-scraper` actor, live LinkedIn job search (`linkedin_only` sources) |

---

## GitHub Actions

| Workflow | Schedule | Purpose |
|---|---|---|
| `daily_monitor.yml` | `0 9 * * *` | News alerts |
| `monthly.yml` | `0 9 1 * *` | Monthly research email |
| `jobs_weekly.yml` | `0 6 * * 5` | Jobs orchestrator |
| `events_weekly.yml` | `0 6 * * 5` | Events orchestrator |
| `weekly_linkedin_digest.yml` | `0 12 * * 5` | LinkedIn post draft |

All five support `workflow_dispatch`.

---

## Run Patterns

```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1

# News pipeline
python daily_monitor.py
python digest.py

# Jobs — single adapter
python jobs_pipeline/run_greenhouse.py
python jobs_pipeline/run_linkedin.py --dry-run --company "Hexis"          # none_found, via Serper
python jobs_pipeline/run_linkedin_apify.py --dry-run --company "Hexis"    # linkedin_only, via Apify

# Jobs — classifier and archive sweep
python jobs_pipeline/run_classifier.py
python jobs_pipeline/run_archive_sweep.py --dry-run
python jobs_pipeline/run_archive_sweep.py

# Jobs — reclassify existing (job_function backfill only, does not re-evaluate accept/reject)
python jobs_pipeline/run_reclassify_all.py

# Jobs — full weekly orchestrator
python jobs_pipeline/run_weekly.py
python jobs_pipeline/run_weekly.py --skip-adapters --skip-email
python jobs_pipeline/run_weekly.py --skip-email

# Events — test single URL
python events_pipeline/test_extractor.py "<url>"
python events_pipeline/test_extractor.py "<url>" --upsert

# Events — full weekly orchestrator
python events_pipeline/run_weekly_events.py
python events_pipeline/run_weekly_events.py --skip-email --limit 5
python events_pipeline/run_weekly_events.py --source meetup --skip-email

# Weekly LinkedIn digest
python weekly_linkedin_digest.py
```

### Local TLS workaround (Norton)

The local Norton TLS proxy (`nllMonFltProxy`) intercepts HTTPS with a CA that Python's OpenSSL 3.5 rejects under strict verification (`Basic Constraints of CA cert not marked critical`), so local runs of any pipeline script fail TLS to Supabase / Serper / LinkedIn. Workaround for local runs only: build a combined certifi + Norton CA bundle (`C:\Users\iddod\.certs\norton-ca.pem`), point `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` at it, and clear `VERIFY_X509_STRICT` on the httpx (`ssl.create_default_context`) and requests/urllib3 (`create_urllib3_context`) contexts — applied at runtime via a throwaway launcher, never committed. GitHub Actions does not see the Norton proxy, so scheduled runs are unaffected; this only blocks local execution.

---

## Do Not Change

- Daily news email format and score 3+ alert logic
- Monthly news email with markdown attachment
- `daily_monitor_seen.json` dedup logic
- News scoring criteria for scores 1, 2, 5
- `LINKEDIN_SYSTEM` company-hallucination guardrails in `weekly_linkedin_digest.py`
- `upsert_job` RPC signature (10 args)
- `upsert_news_item_if_higher_score` RPC signature (12 args)
- `upsert_event_if_new` RPC signature (14 args)
- Weekly LinkedIn digest picking rules (top 5, source diversity hard constraint, topic diversity hard constraint)
- Weekly LinkedIn post format: `Headline - relevance. read more-URL`
