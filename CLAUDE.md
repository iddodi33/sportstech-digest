# CLAUDE.md — sportstech-digest

*Last updated: 2026-09-26*

---

## Project Purpose

`sportstech-digest` is the scraping and intelligence pipeline for Sports D3c0d3d. It feeds the hub Supabase project (`xwqmnofkvdwpagfweqmj`, West EU / Ireland) which powers the admin panel at sd3-intelligence-hub. Four responsibilities:

1. **News pipeline** — scrapes Irish sportstech news, scores with Claude Sonnet 4.5, emails daily alerts and a monthly research markdown, upserts score 3+ articles to the hub.
2. **Jobs pipeline** — scrapes weekly job listings from 10 ATS platforms plus two LinkedIn paths (Apify for `linkedin_only` companies, Serper discovery for `none_found` companies), classifies via rule-based pre-filter + relevance filter + Haiku 4.5, archives stale jobs, upserts to the hub.
3. **Events pipeline** — scrapes weekly events from 5 sources, extracts structured data via Claude Sonnet 4.5, upserts pending events to the hub for admin review. A 6th source added 2026-09-14 searches LinkedIn by keyword and by curated organiser pages (Apify, `harvestapi/linkedin-post-search`) and lands raw posts in `public.event_leads` — a separate triage queue, no Claude step, never `public.events`.
4. **Weekly LinkedIn posts (Cowork-owned since 2026-07-24)** — the Friday news brief and Monday jobs post are drafted by Cowork scheduled tasks that pull news_items + social_posts / approved jobs straight from the hub and write Cockpit tasks (`ops.tasks`, source_schema `sd3-weekly-post`). This repo's part is `weekly_cover.yml` + `weekly_cover.py` + `carousel_slides.py`, which render the branded cover image AND the six-slide carousel (a cover slide plus one per pick, as PNGs and a six-page PDF) from the picks and attach them to the Cockpit task. `weekly_cover.yml`'s cron was retired 2026-08-29 and RESTORED 2026-09-05, because the Friday post is now a carousel and the render is load-bearing rather than optional. The old `weekly_linkedin_digest.py` email draft stays retired (manual dispatch only).

Repo: `C:\coding_projects\sportstech-digest`  
GitHub: https://github.com/iddodi33/sportstech-digest (branch: main)

---

## Repo Layout

```
sportstech-digest/
  daily_monitor.py               News: daily 9am UTC alert
  digest.py                      News: monthly 1st research email
  claude_budget.py               Shared cost metering + circuit breaker (RunCost, call_claude_with_retry)
  run_telemetry.py               Append-only JSONL telemetry: usage, cap drops, per-feed stats
  news_pipeline.py               News: RSS + Google News + Supabase company queries
                                 Holds GOOGLE_NEWS_FEEDS, SITE_RSS_FEEDS and REGIONAL_RSS_FEEDS.
                                 daily_monitor reads GOOGLE_NEWS_FEEDS + REGIONAL_RSS_FEEDS only;
                                 SITE_RSS_FEEDS is read solely by the monthly.yml path.
  weekly_linkedin_digest.py      RETIRED 2026-07-24 (manual dispatch only); replaced by Cowork scheduled tasks
  weekly_cover.py                Weekly cover image + carousel renderer (GH Actions, hourly Fri firings, picks-hash idempotent)
  carousel_slides.py             Carousel slide layouts and PDF assembly, imported by weekly_cover.py (added 2026-09-05)
  assets/cover/                  Brand assets for the cover (logo PNG, Bebas Neue TTF)
  supabase_client.py             News: upserts scored articles to hub
  email_client.py                Resend wrapper for all pipelines — 150ms send spacing, 429 retry x3
  test_daily_monitor.py          Offline tests: normalise_url, blocklist, Resend 429 retry, send loop
  jobs_pipeline/                 Weekly jobs scraper (Friday 06:00 UTC)
    classifier.py                Rule pre-filter + Haiku classifier
    relevance_filter.py          Rule-based title noise filter, shared by both LinkedIn adapters
    run_classifier.py            Classify pending unclassified jobs
    run_reclassify_all.py        Backfill job_function on existing jobs
    run_archive_sweep.py         Archive stale jobs
    run_weekly.py                Full weekly orchestrator
    supabase_jobs_client.py      DB helpers (upsert, mark_seen, mark_source_*)
    adapters/                    One file per ATS platform + base.py (linkedin.py=Serper/none_found, apify_linkedin.py=Apify/linkedin_only, custom_html.py=own-site careers pages)
    run_custom_html.py           custom_html entry point (--dry-run, --company)
    test_custom_html.py          Offline parser tests for custom_html
    weekly/                      runner.py, snapshot.py, email_builder.py, sendgrid_client.py
    run_<platform>.py            Per-platform entry points
  events_pipeline/               Weekly events scraper (Friday 06:00 UTC)
    extractor.py                 HTML → Claude → structured event JSON
    run_weekly_events.py         Full weekly orchestrator (the 5 structured sources)
    adapters/                    5 event source adapters + linkedin_keywords.py (6th, separate path)
    run_linkedin_leads.py        6th source entry point — LinkedIn keyword/seed-author leads + Haiku classification
    lead_classifier.py           Haiku relevance labelling for event_leads (added 2026-09-14)
    resolve_lsp_linkedin.py      One-off: resolve the 29 LSPs to LinkedIn pages (Serper, corroborated)
    data/                        linkedin_seed_authors.csv (live seeds), lsp_linkedin_resolved.csv (proposals)
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
- **Unattended cron scripts get an aborting cost ceiling, never a typed-confirmation
  gate.** The standing cost rule (real token-counted estimate + typed confirmation +
  hard-dollar breaker) read literally points the other way, but a confirmation prompt in
  a scheduled job has nobody to answer it and would simply hang or break the run. For
  cron scripts the confirmation half is waived and the breaker half is **not**: it aborts,
  keeps and processes work already paid for, logs the abort distinguishably, alerts, and
  exits non-zero. Typed confirmation still applies to anything a human runs by hand
  (e.g. `scripts/audit_alerts_vs_hub.py`).
- **One standing exception to the cost-ceiling rule, and only one.**
  `events_pipeline/run_linkedin_leads.py` (the LinkedIn event-lead step) runs
  unattended on Friday cron with **no aborting cost ceiling**, on both its Apify
  and its Anthropic budget. That is Iddo's explicit call, reaffirmed 2026-09-14
  after the first run came in under $1. It is recorded here so nobody "fixes" it
  by accident and nobody treats it as precedent. What it has instead: **warn-only**
  thresholds (`APIFY_WARN_ABOVE_USD` $3.00, `lead_classifier.WARN_ABOVE_USD`
  $1.00) that log and continue, `MAX_POSTS_PER_QUERY` capping every actor call, a
  fixed-length query table rather than anything derived at runtime, one classifier
  call per lead *ever* (`classified_at` is the guard), and real billed spend in
  `scripts/data/apify_spend.jsonl` + `scripts/data/anthropic_spend.jsonl`.
  Every *other* unattended script still gets a real aborting ceiling.
- **`run_telemetry.py` has three independent rate sections. Do not merge them.**
  (1) the Sonnet per-MTok table at the top, which **both news pipelines'
  `RUN_COST_CEILING_USD` are enforced against**; (2) `RATE_HAIKU_*` +
  `record_anthropic_run()`, used by the events LinkedIn classifier, which is 3x
  cheaper than Sonnet on both input and output — pricing Haiku tokens with
  `cost_usd()` overstates them threefold and tempts someone into "fixing" the
  shared constants and silently moving the news breakers; (3) the Apify per-event
  section writing `apify_spend.jsonl`, which is vendor-event spend with no tokens
  and no model. Apify costs come from the run object's own event counts x its own
  event prices, never from hardcoded rates — see `charged_cost_usd()` and the
  ARCHITECTURE.md note on why `usageTotalUsd` alone under-reports by ~2.7x.
- **The events LinkedIn classifier and the jobs classifier share a model on
  purpose.** Both are `claude-haiku-4-5-20251001`. If one moves, move both, and
  update `RATE_HAIKU_*` in the same change.
- **Model bumps must update `run_telemetry.py`'s rates in the same change.**
  `run_telemetry.py` hardcodes the current `MODEL`'s per-MTok rates, and **both pipelines'
  `RUN_COST_CEILING_USD` are enforced against them**. Bump the model without the rates and
  the breaker fires at the wrong real spend, in either direction. Existing log records are
  safe — each carries its own `rate_*` and `pricing_verified_on` — but new runs are not.
- **Which modules make billed Anthropic calls.** Verified 2026-09-04, not assumed:
  **billed** — `daily_monitor.py` (score + dedup), `digest.py` (score);
  **not billed** — `news_pipeline.py` (zero `anthropic` references; safe to run alone for
  measurement). `scripts/verify_discovery_coverage.py` is discovery-only by design.
- **Both cost ceilings are provisional.** $2.25 (`daily_monitor.py`) and $4.25
  (`digest.py`) were derived by scaling the 2026-09-04 audit run's measured per-article
  token figures to each pipeline's article count — neither comes from an observed run of
  its own. Retune both from `scripts/data/daily_monitor_usage.jsonl` (the `pipeline` field
  separates them) once real runs have accumulated.
- **Careers-page discovery is manual, and adding a company is not enough.**
  `jobs_discovery/` is an offline CSV pipeline run by hand; no workflow invokes it.
  A row in `companies` with no `company_careers_sources` row is scraped by nothing
  (58 of 135 companies were in that state on 2026-09-14). Adding a company means:
  insert into `companies`, run discovery, import the source row, and confirm the
  resulting `ats_platform` has an adapter.
- **Never activate an ATS source from an uncorroborated slug guess.** Discovery
  probes slugs derived from the company name/domain; a hit only proves that
  slug names *a* live board, not *this* company's. `onezero` matched oneZero
  Financial Systems' BambooHR board and put 10 misattributed jobs in the hub on
  2026-09-14 (same failure as EA Sports' `ea` slug in May). `_corroborate_ats()`
  now flags these `needs_manual_review=true` — check the board by hand before
  setting `is_active=true`. It fails closed, so some genuine boards are flagged
  too (Cloudflare sites 403 `aiohttp`); that is intended.
- **Check an ATS platform has an adapter before importing it.** The
  `ats_platform` CHECK constraint permits `workable`, `smartrecruiters` and
  `recruitee`, none of which have adapters; `rippling`/`phenom` have adapters but
  zero sources. A source row on a platform with no adapter is scraped by nothing
  and reports no error. `custom_html` was in exactly this state until 2026-09-14.
- **`serper_no_results` on a `none_found` row is usually correct.** Verified
  2026-09-14 against the live API: 34 of 38 such companies genuinely have no
  LinkedIn postings indexed in the past month. The key and query are fine. Treat a
  mass of these as evidence the company advertises somewhere else (its own site),
  not as a Serper fault — see ARCHITECTURE.md.
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
APIFY_TOKEN                        Two consumers: the LinkedIn *jobs* adapter (linkedin_only sources, curious_coder actor) and the LinkedIn *events* lead adapter (harvestapi/linkedin-post-search). Optional for jobs: a missing token degrades the linkedin_apify weekly step to a logged warning. Required for the events LinkedIn step, which exits 1 without it (continue-on-error in the workflow, so the events run still passes).
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
| Apify — jobs | `curious_coder/linkedin-jobs-scraper` actor, live LinkedIn job search (`linkedin_only` sources) |
| Apify — events | `harvestapi/linkedin-post-search` actor, LinkedIn post search by keyword + organiser page. PAY_PER_EVENT, $0.002/post at BRONZE tier (verified 2026-09-14) |

---

## GitHub Actions

| Workflow | Schedule | Purpose |
|---|---|---|
| `daily_monitor.yml` | `0 9 * * *` | News alerts |
| `monthly.yml` | `0 7 * * 0` | UN-RETIRED 2026-09-04 (weekly, Sun 07:00 UTC). Only path that reads `SITE_RSS_FEEDS` + `REGIONAL_RSS_FEEDS`; while its cron was off (2026-07-24 → 2026-09-04) the site-RSS half of news discovery ran nowhere. Still sends the research email. |
| `jobs_weekly.yml` | `0 6 * * 5` | Jobs orchestrator |
| `events_weekly.yml` | `0 6 * * 5` | Events orchestrator (5 structured sources), then the LinkedIn event-lead step, then commits `scripts/data/apify_spend.jsonl` |
| `weekly_cover.yml` | `20 9,10,11,12 * * 5` | Cron retired 2026-08-29, RESTORED 2026-09-05 — weekly LinkedIn cover image plus the six-slide carousel PDF; hash-idempotent, re-renders when PICKS_JSON changes. The Friday post depends on it. |
| `weekly_linkedin_digest.yml` | manual only | RETIRED 2026-07-24 — replaced by Cowork scheduled tasks (see STATUS.md) |
| `monthly_28th.yml` | manual only | RETIRED 2026-08-29 — newsletter-source export only (slimmed 2026-07-24; digest/jobs/events steps removed). Run manually ahead of the 29th newsletter build when needed. |

All scheduled workflows support `workflow_dispatch`.

---

## Run Patterns

```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1

# News pipeline
python daily_monitor.py
python digest.py
python test_daily_monitor.py            # offline, no network or billed calls

# Jobs — single adapter
python jobs_pipeline/run_greenhouse.py
python jobs_pipeline/run_linkedin.py --dry-run --company "Hexis"          # none_found, via Serper
python jobs_pipeline/run_linkedin_apify.py --dry-run --company "Hexis"    # linkedin_only, via Apify
python jobs_pipeline/run_custom_html.py --dry-run --company "TeamFeePay"  # custom_html, own-site careers page
python jobs_pipeline/test_custom_html.py                                 # offline parser tests, no network

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

# Events — LinkedIn leads (6th source; writes event_leads, not events)
python events_pipeline/run_linkedin_leads.py --dry-run --limit 1   # one actor call, ~$0.05
python events_pipeline/run_linkedin_leads.py --dry-run             # NB: still bills Apify
python events_pipeline/run_linkedin_leads.py --skip-classify       # discover only, no Haiku
python events_pipeline/run_linkedin_leads.py                       # discover + classify
python events_pipeline/resolve_lsp_linkedin.py                     # one-off LSP page resolution

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
- `daily_monitor_seen.json` dedup logic — keyed on `normalise_url()` since 2026-09-26 (changed at Iddo's request). `normalise_url()` must stay idempotent: entries are stored normalised and normalised again on load
- News scoring criteria for scores 1, 2, 5
- `upsert_job` RPC signature (10 args)
- `upsert_news_item_if_higher_score` RPC signature (12 args)
- `upsert_event_if_new` RPC signature (14 args)
- PICKS_JSON contract between the Cowork Friday news-brief trigger and `weekly_cover.py` (final line of the Cockpit task notes: `PICKS_JSON: [{"company","slug","news_url"}]`)
- `public.events` and the 5 structured event adapters — the LinkedIn event-lead source writes only to `public.event_leads`, and a lead reaches `events` only when a human promotes it
- The seed list in `events_pipeline/data/linkedin_seed_authors.csv` is hand-reviewed. Never add a LinkedIn page to it from a slug guess or an uncorroborated resolver row
- `event_leads.status` is the human's column — the Haiku classifier writes only the advisory fields beside it (`is_event_relevant`, `relevance_confidence`, `post_kind`, `rating_notes`) and never deletes or re-queues a lead
