# CLAUDE.md — sportstech-digest

*Last updated: 25 April 2026*

---

## What This Repo Is

A Python research and scraping pipeline that powers Sports D3c0d3d's intelligence operations. Three responsibilities:

1. **News pipeline** — scrapes Irish sportstech news, scores articles with Claude, emails alerts and a monthly research markdown, writes scored articles to the hub Supabase
2. **Jobs pipeline** — scrapes weekly job listings from ATS APIs and career pages, classifies via rule-based filters + Haiku, writes to the hub Supabase for admin review
3. **Job scraping** — scrapes Irish sportstech jobs from LinkedIn, WHOOP, Adzuna, writes CSV (legacy, separate from new jobs pipeline)

Repo location: `C:\coding_projects\sportstech-digest`

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| HTTP | requests, httpx |
| HTML parsing | BeautifulSoup4 |
| Database | Supabase Python SDK |
| AI | Anthropic SDK (Claude Sonnet 4.5 for news scoring, Haiku 4.5 for job classification) |
| Email | SendGrid (current trial expires 29 May 2026) |
| Scheduling | GitHub Actions cron |

---

## Repo Structure

```
sportstech-digest/
  daily_monitor.py                    News: daily 9am UTC alert with LinkedIn drafts
  digest.py                           News: monthly 1st-of-month research markdown email
  news_pipeline.py                    News: RSS + direct scraping, googlenewsdecoder
  enhanced_sportstech_job_scraper_v3.py    Legacy job CSV scraper (LinkedIn, WHOOP, Adzuna)
  supabase_client.py                  News: writes scored articles to hub Supabase
  
  jobs_pipeline/                      NEW 24-25 April 2026: weekly job scraper
    __init__.py
    supabase_jobs_client.py           Singleton client, get_active_sources(), upsert_job() RPC
    classifier.py                     Rule-based pre-filter + Haiku classifier
    run_classifier.py                 Entry point for classification pass
    adapters/
      __init__.py
      base.py                         BaseAdapter (fetch abstract, run concrete)
      greenhouse.py                   Greenhouse public API
      ashby.py                        Ashby JSON API
      lever.py                        Lever public API
      personio.py                     Personio search.json
      breezy.py                       Breezy /json
      bamboohr.py                     BambooHR /careers/list
      teamtailor.py                   Teamtailor JSON:API + HTML fallback
      workday.py                      Workday POST + pagination
      rippling.py                     Rippling /api/v2/board/{slug}/jobs
      phenom.py                       Phenom People standard REST (some tenants use widget API)
    run_greenhouse.py, run_ashby.py, run_lever.py, run_personio.py,
    run_breezy.py, run_bamboohr.py, run_teamtailor.py, run_workday.py,
    run_rippling.py, run_phenom.py
                                      Per-platform entry points
  
  jobs_discovery/                     One-off discovery + import (deprecated after 24 April)
    career_pages.csv                  74-row source-of-truth that seeded company_careers_sources
    discover_career_pages.py          Initial multi-platform discovery script
    import_to_supabase.py             One-off CSV → Supabase import (already run)
  
  research/                           Monthly markdown output destination
  .github/workflows/
    daily_monitor.yml                 News: daily 9am UTC cron
    monthly.yml                       News: monthly 1st-of-month cron
```

---

## Environment Variables

```
ANTHROPIC_API_KEY                     Sonnet for news, Haiku for jobs
ADZUNA_APP_ID                         Legacy job scraper
ADZUNA_APP_KEY                        Legacy job scraper
SENDGRID_API_KEY                      News email send
ALERT_FROM=monitor@sportsd3c0d3d.ie
ALERT_TO=iddodiamant@gmail.com
NEXT_PUBLIC_SUPABASE_URL=https://xwqmnofkvdwpagfweqmj.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY         Informational, pipeline uses service role
SUPABASE_SERVICE_ROLE_KEY             Required for upserts to hub
```

GitHub Actions secrets must include all of the above for both workflows.

---

## News Pipeline (existing, established 18 April 2026)

### Daily flow (`daily_monitor.py`, runs 9am UTC daily)
1. Fetches Google News RSS feeds for Irish sportstech keywords
2. Decodes Google News redirect URLs via `googlenewsdecoder`
3. Scores each article 1-5 via Claude Sonnet 4.5
4. Upserts score 3+ articles to hub `news_items` table via `upsert_news_item_if_higher_score` RPC
5. Sends alert email to Iddo with LinkedIn post drafts for all score 3+ articles
6. Persists `daily_monitor_seen.json` for dedup across runs (committed by GitHub Actions)

### Monthly flow (`digest.py` + `news_pipeline.py`, runs 1st of month 9am UTC)
1. Reads `news_raw_YYYY-MM.json` (collected by `news_pipeline.py` throughout the month)
2. Scores all articles via Claude Sonnet 4.5
3. Writes `research/YYYY-MM-research.md`
4. Upserts score 3+ to hub Supabase (same RPC)
5. Emails markdown as attachment to Iddo

### Scoring scale
- 5: Irish sportstech company — funding, product launch, award, expansion
- 4: Irish sports org adopting tech, Irish sportstech person
- 3: European sportstech news relevant to Irish audience
- 2: Irish sports without tech angle, operations roles
- 1: Off-topic, no sports angle, duplicate

### Claude scoring prompt fields
score, score_reason, summary (2 sentences, 40-60 words), tags, verticals (closed list), mentioned_companies.

### Closed vertical list (must match hub)
Performance Analytics | Wearables & Hardware | Fan Engagement | Media & Broadcasting | Health, Fitness and Wellbeing | Scouting & Recruitment | Esports & Gaming | Betting & Fantasy | Stadium & Event Tech | Club Management Software | Sports Education & Coaching | Other / Emerging

### OG metadata extraction
`fetch_og_metadata(url)` in `supabase_client.py` extracts og:image and og:title with realistic User-Agent and 10s timeout. Used to populate hub news cards. og:title preferred over RSS title when ≥15 chars and different.

### Publisher name extraction
`extract_publisher(url)` maps known Irish + international news domains to clean names via dictionary. Handles multi-part TLDs (.co.uk, .com.au, .co.ie). Falls back to title-cased domain stem. Rectified 12 incorrectly-cased values 25 April at hub data layer (Ucd → UCD, Sportsbusinessjournal → Sports Business Journal, etc.) — extractor logic also tightened.

### SendGrid status
Domain `sportsd3c0d3d.ie` authenticated via CNAME records at Blacknight (em7190, s1._domainkey, s2._domainkey). Free trial ends **29 May 2026**, requires paid upgrade for ongoing sending.

---

## Jobs Pipeline (built 24-25 April 2026)

### Architecture

Weekly scrape, not daily. Jobs change slower than news. Target run: Sunday night or Monday morning UTC. GitHub Actions cron not yet wired up.

Adapters are dumb transport: fetch, normalise, write. No filtering or classification at the adapter layer — that happens downstream in the classifier.

### Per-adapter notes (live spec corrections discovered during build)

**greenhouse.py** (4 companies: Hudl, Riot Games, Genius Sports, Fanatics)
- GET `boards-api.greenhouse.io/v1/boards/{slug}/jobs`
- Content is HTML-entity-escaped — used `html.unescape()` before BS4 strip

**ashby.py** (4 companies: WHOOP, Strava, Teamworks, STATSports)
- GET `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`
- Field name corrections: `shouldDisplayCompensationOnJobPostings` (not OnJobBoard), `compensation.compensationTierSummary` (not tierSummary)
- `descriptionPlain` preferred over descriptionHtml

**lever.py** (1 company: Kitman Labs)
- GET `api.lever.co/v0/postings/{slug}?mode=json`
- `description` field is HTML in current API — used `descriptionPlain + descriptionBodyPlain`

**personio.py** (1 company: Output Sports)
- GET `{slug}.jobs.personio.com/search.json`
- search.json doesn't include descriptions — set summary=null
- Response has no `url` field — URL constructed from slug+id

**breezy.py** (1 company: SportsKey)
- GET `{slug}.breezy.hr/json`
- Field is `id` not `_id`; /json only returns published jobs (no state filter needed)

**bamboohr.py** (2 companies: Catapult, EA Sports)
- GET `{slug}.bamboohr.com/careers/list`
- Returns JSON despite the URL pattern — initial spec said HTML

**teamtailor.py** (2 companies: Boylesports, Stats Perform)
- Both on custom domains (no slug)
- JSON:API at `careers.{domain}/jobs.json` blocked by section.io CDN (HTTP 406)
- HTML fallback used; Boylesports works, Stats Perform does not (their HTML is JS-rendered)

**workday.py** (2 companies: DraftKings, Flutter Entertainment)
- POST `{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
- Country filter via `appliedFacets.locationCountry` not supported by all tenants — DraftKings tenant doesn't expose country facet
- Stops paginating when page returns < limit jobs (in case `total` shifts mid-scrape)
- DraftKings: tenant=draftkings, pod=1, site=DraftKings (capital D matters)
- Flutter: tenant=flutterbe, pod=3, site=FlutterUKI_External

**rippling.py** (2 companies: PFF, Thrive Global)
- GET `ats.rippling.com/api/v2/board/{slug}/jobs?page=N&pageSize=50`
- Listings response is metadata only — descriptions require per-job detail fetch (skipped for now to keep adapter simple)
- Spec corrections: `officeLabel` is actually `locations[0].name`, `absolute_url` is actually `url`
- workplaceType/employmentType not in listings response

**phenom.py** (built but no working tenant currently)
- GET `careers.{tenant}.com/api/apply/v2/jobs?lang=en_us&pagesize=N&from=N`
- Pagination via `from` parameter, response shape `{status, data: {totalHits, results}}`
- Blizzard tenant uses non-standard widget API with private DDO session keys, raises clear RuntimeError
- Adapter would work for any tenant on standard public REST

### Classifier (`classifier.py` + `run_classifier.py`)

Rule-based pre-filter (run before Haiku):

1. **Junior keyword reject** — word-boundary regex `\b(junior|intern|internship|graduate|entry[\s-]level|trainee|apprentice)\b`. 'Associate' allowed through (too ambiguous). Bug fixed 25 April: `\bintern\b` boundary prevents 'Internal Auditor' false positive.

2. **FDI geography reject** — for `is_fdi=true AND is_irish_founded=false`. Ireland-eligible whitelist: dublin, cork, galway, limerick, belfast, waterford, ireland, ', ie', 'co. ', plus 'remote - emea/europe/eu'. Reject patterns: 'remote - us/canada/latam/apac/anz', US state suffixes, named non-Ireland European cities. **Tightened 25 April** to also reject 'multiple locations', '2 locations', '3 locations', 'multiple cities', 'various locations'.
   - **Known gap**: pattern is fixed list, not regex. '6 locations' wasn't caught in Flutter scrape. Update needed: switch to `\d+ locations?` regex.

3. **Sportstech relevance reject** (after Haiku) — auto-rejects roles where `sportstech_relevance == 'not_sportstech'` (back-office: general accounting, HR ops, facilities, admin). 'Ambiguous' stays pending.

Haiku classification (Haiku 4.5):
- seniority (mid|senior|lead|executive)
- employment_type
- remote_status
- vertical (12 closed-list values, defaults to company's existing vertical)
- location_normalised
- sportstech_relevance

Field normalisation layer handles Haiku enum drift (`fixed_term_contract` → null, `permanent` → null, `office` → null, `graduate` → null).

### Run pattern

```
.venv\Scripts\Activate.ps1                       (Windows PowerShell)
source venv/bin/activate                         (bash)

python jobs_pipeline/run_greenhouse.py
python jobs_pipeline/run_ashby.py
python jobs_pipeline/run_lever.py
python jobs_pipeline/run_personio.py
python jobs_pipeline/run_breezy.py
python jobs_pipeline/run_bamboohr.py
python jobs_pipeline/run_teamtailor.py
python jobs_pipeline/run_workday.py
python jobs_pipeline/run_rippling.py
python jobs_pipeline/run_classifier.py
```

Total raw scraped: ~640 jobs across all adapters. Classifier produced 30 pending in first run, 7 more after second pass. Admin reviewed → 13 approved live on hub.

### Hub integration

All adapters write to hub `jobs` table via `upsert_job` RPC (10 args). RPC handles dedup by URL, preserves `first_seen_at`, updates `last_seen_at`, never regresses status (so admin decisions persist across re-scrapes). Returns `(id, was_inserted, was_reactivated)` for adapter logging.

---

## Hub Supabase Integration (shared across both pipelines)

Hub project: xwqmnofkvdwpagfweqmj (West EU/Ireland).

### Tables written to from this repo

- `news_items` — via `upsert_news_item_if_higher_score` RPC (12 args)
- `jobs` — via `upsert_job` RPC (10 args)
- `company_careers_sources` — read-only (one-off import done 24 April)

### RPC signatures

`upsert_news_item_if_higher_score(p_url, p_title, p_source, p_summary, p_tags, p_verticals, p_published_at, p_score, p_score_reason, p_mentioned_companies, p_image_url, p_original_title)` — only overwrites score/reason when new score is higher; COALESCE on image_url and original_title.

`upsert_job(p_url, p_title, p_source, p_sources_source_id, p_company_id, p_company_name, p_location_raw, p_summary, p_salary_range, p_scraped_at)` — non-archived URL match: updates mutable fields + last_seen_at, preserves status/classification/admin audit. Archived URL match: flags was_reactivated=true, inserts fresh pending row. SECURITY DEFINER, service_role only.

---

## GitHub Actions

- `.github/workflows/daily_monitor.yml` — `daily_monitor.py` at 9am UTC daily; commits `daily_monitor_seen.json`
- `.github/workflows/monthly.yml` — full news pipeline on the 1st of each month at 9am UTC
- ⬜ Jobs pipeline weekly cron — not yet wired up

---

## Do not change

- The daily news email with LinkedIn draft (fires for score 3+)
- The monthly news email with markdown attachment
- The `daily_monitor_seen.json` dedup logic
- The 1-5 news scoring criteria
- The `LINKEDIN_SYSTEM` prompt's company-hallucination guardrails (added 21 April after STATSports/concussion-tech false claim)

---

## Recent Changes Log

### 25 April 2026 — Second-pass ATS investigation, Phenom + Rippling adapters, hub UI live
- Built Phenom and Rippling adapters
- Reclassified 4 companies from custom_html to proper ATS rows in `company_careers_sources` (Blizzard→phenom, Flutter→workday, PFF/Thrive Global→rippling)
- 6 indigenous Irish companies reclassified custom_html → linkedin_only
- Blizzard later reclassified again to linkedin_only after discovering Phenom widget-API limitation
- Re-ran Workday adapter to pick up Flutter — 22 jobs scraped including 4 Dublin-tagged
- Classifier ran on 24 new jobs: 7 added to pending review queue
- Manual SQL cleanup of Flutter '6 Locations' / Leeds / Gibraltar pending rows that the regex didn't catch
- Hub UI deployed live with admin review queue, public /jobs page, company detail integration

### 24 April 2026 — Jobs pipeline foundation
- company_careers_sources schema + 73 rows imported from career_pages.csv
- jobs table extended with 17 new columns
- jobs_review_feedback table
- upsert_job RPC built and smoke-tested
- 8 ATS adapters built (Greenhouse, Ashby, Lever, Personio, Breezy, BambooHR, Teamtailor, Workday)
- ~618 jobs scraped on first run
- Classifier built with rule-based pre-filter + Haiku
- Hub admin /jobs page built (in hub repo)

### 18 April 2026 — News pipeline integration
- supabase_client.py created with upsert_news_item, fetch_og_metadata, extract_publisher
- daily_monitor.py and digest.py upsert score 3+ articles to hub
- Scoring prompts extended with score_reason, summary, tags, verticals, mentioned_companies
- RPC `upsert_news_item_if_higher_score` (12 args) created
- SendGrid `sportsd3c0d3d.ie` domain authenticated
- OG image + title extraction added
- Google News URL resolution bug fixed (returning publisher homepage as article URL)

### 21 April 2026 — LinkedIn draft prompt hardening
- Company hallucination bug caught (STATSports falsely associated with concussion tech)
- Verify-before-naming guardrails added directly to LINKEDIN_SYSTEM prompt
- Per-company capability facts embedded (STATSports GPS only, Orreco biomarkers only, etc.)
- Alert threshold lowered to score 3+
