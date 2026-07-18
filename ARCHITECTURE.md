# ARCHITECTURE.md — sportstech-digest

*Last updated: 2026-07-14*

Technical reference. For recent changes and open bugs see `STATUS.md`.

---

## Database Schema

### `jobs` table (key columns)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `url` | text unique | Canonical job URL, upsert key |
| `title` | text | |
| `company_id` | uuid FK → companies | |
| `company_name` | text | Denormalised for display |
| `sources_source_id` | uuid FK → company_careers_sources | |
| `source` | text | ATS platform name |
| `location_raw` | text | As returned by adapter |
| `location_normalised` | text | Set by Haiku classifier |
| `summary` | text | Plain-text description, max 2000 chars |
| `summary_excerpt` | text | Haiku-extracted 2-3 sentence role description, max 400 chars |
| `salary_range` | text | |
| `status` | text | `pending` / `approved` / `rejected` / `archived` |
| `rejected_reason` | text | `too_junior` / `fdi_geography` / `not_sportstech` / custom cleanup labels |
| `classification` | jsonb | Full Haiku output + rules flags + geo_check + classified_at + model |
| `seniority` | text | `mid` / `senior` / `lead` / `executive` |
| `employment_type` | text | `full_time` / `part_time` / `contract` / `internship` / `temporary` |
| `remote_status` | text | `onsite` / `hybrid` / `remote` |
| `vertical` | text | Closed list matching hub |
| `job_function` | text | 8 valid values (see classifier section) |
| `last_seen_in_scrape_run` | timestamptz | Stamped by adapter after each successful upsert |
| `archived_at` | timestamptz | Set by archive sweep |
| `created_at` | timestamptz | |

### `company_careers_sources` table (key columns)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `company_id` | uuid FK → companies | |
| `company_name` | text | Denormalised |
| `ats_platform` | text | CHECK constraint — see allowed values below |
| `ats_slug` | text | Platform-specific identifier (slug or tenant) |
| `ats_api_endpoint` | text | Full URL; used by most adapters directly |
| `careers_url` | text | Public careers page (HTML fallback) |
| `workday_tenant` | text | Workday-specific tenant string (e.g. `flutterbe`, `draftkings`) |
| `linkedin_search_name` | text | Override for Serper query when company LinkedIn name differs |
| `is_active` | boolean | Inactive sources are skipped |
| `last_scrape_run_at` | timestamptz | Stamped after every run attempt |
| `last_successful_scrape_at` | timestamptz | Stamped only when jobs_upserted > 0 |

**`ats_platform` CHECK constraint — 14 allowed values (verified live 2026-07-14):**
`greenhouse`, `lever`, `workable`, `ashby`, `teamtailor`, `smartrecruiters`, `bamboohr`, `personio`, `recruitee`, `breezy`, `workday`, `custom_html`, `linkedin_only`, `none_found`

Note: **`rippling` and `phenom` are NOT in this constraint** despite adapter code existing for both — neither has active sources, and adding one requires an ALTER TABLE first. `workable`, `smartrecruiters`, and `recruitee` are allowed values with no adapter code or active sources yet (unused headroom, not a gap to fix).

### `companies` table (key columns for jobs pipeline)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | text | |
| `is_fdi` | boolean | True for foreign multinationals with Irish presence |
| `is_irish_founded` | boolean | True for companies founded in Ireland |
| `fdi_classifier_allowlisted` | boolean | **Added 2026-05-28.** True for 18 FDI sportstech majors — bypasses strict Ireland-only geography in favour of Ireland+UK |
| `vertical` | text | Sportstech vertical, passed to Haiku as context |
| `description` | text | Company description, first 300 chars passed to Haiku |

---

## ATS Adapters

All adapters follow the `BaseAdapter` pattern in `adapters/base.py`: `fetch()` returns a normalised list of job dicts; `run()` orchestrates upserts and source-tracking stamps.

**Greenhouse** (`boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`) — simple JSON GET, description in `content` field. Most reliable adapter.

**Ashby** — GraphQL-style POST to `jobs.ashbyhq.com`, returns `descriptionPlain` or falls back to stripping `descriptionHtml`.

**Lever** — REST JSON list at `api.lever.co/v0/postings/{slug}?mode=json`, description in `text.description` or `lists`.

**Personio** — `search.json` list endpoint always returns empty `description` (server-side rendered). Fix: fetches per-job HTML detail page and extracts from `<script type="application/ld+json">`.

**Breezy** — `/json` endpoint returns published positions as a flat list. Description field present but not always populated. Returns `[]` for empty boards; adapter correctly calls `mark_source_attempted` (not `mark_source_successful`).

**BambooHR** — `{slug}.bamboohr.com/careers/list` returns JSON (not HTML). Job detail URL: `{slug}.bamboohr.com/careers/{id}`. Descriptions not fetched at list stage — `summary` is None. **EA Sports historic note:** BambooHR source pointed at wrong slug ("ea") which belongs to a social-services org — deactivated 2026-05-28, 29 misattributed jobs deleted, replaced with LinkedIn fallback.

**Teamtailor** — `/jobs.json` serves one of two shapes depending on the tenant, auto-detected per fetch by `_fetch_all()`: JSON:API (`data`/`included`/`relationships` — the original assumption) or JSON Feed 1.x (`items`, each carrying a Teamtailor-specific `_jobposting` schema.org JobPosting object with a structured `jobLocation` address — confirmed live for Boylesports, the sole active source, as of 2026-07-14). Parsing JSON Feed against the JSON:API code silently reads `data.get("data") == []` every time — 0 jobs, no exception raised, so the HTML fallback (which only triggers on HTTP/connection errors) never engaged either; this caused **7+ weeks of undetected zero-yield** (`last_successful_scrape_at` stuck at 2026-05-22 while `last_scrape_run_at` advanced weekly) before being caught and fixed 2026-07-14. HTML-scraping fallback (`_fetch_html`) is retained for genuine connectivity failures. Stats Perform migrated to `linkedin_only` as of 2026-05-28 (unrelated to this bug — its board was JS-rendered, this one returns valid JSON, just a different valid shape than expected).

**Workday** — Uses undocumented but stable `POST /wday/cxs/{tenant}/{site}/jobs` (same as the careers site JS calls). No description in list response; fetches per-job HTML page and extracts from JSON-LD. Adds ~0.3s per job. URL pattern: `{tenant}.wd{N}.myworkdayjobs.com/.../{slug}/job/{Office-CC}/...`. The `/job/{Office-CC}/` segment is used by `_check_fdi_geography_allowlisted` to disambiguate "Multiple Locations" — see Classifier section. Tenant examples: `flutterbe` (Flutter), `draftkings` (DraftKings).

**Rippling** — Adapter code exists. **Zero active sources.** `rippling` is not an allowed value in the `ats_platform` CHECK constraint — adding sources requires `ALTER TABLE` first.

**Phenom** — Adapter code exists. Zero active sources currently.

**LinkedIn/Serper** — See dedicated section below.

---

## Source Tracking Semantics

Two timestamps on `company_careers_sources`, set in `adapters/base.py` `run()` finally block:

- `last_scrape_run_at` — stamped on every run attempt via `mark_source_attempted`, regardless of outcome.
- `last_successful_scrape_at` — stamped only when `upserted_count > 0` via `mark_source_successful`. A run that fetches 0 jobs (empty board, broken endpoint, CDN block) does NOT update this column.

The archive sweep health gate uses `last_successful_scrape_at` with an 8-day window. A silently-failing adapter (0 jobs, no exception) will eventually lose its health status after 8 days, protecting its jobs from being archived.

Weekly email status: `_aggregate()` in `weekly/runner.py` sets `status="warning"` when `scraped == 0` and no exception was raised, `"failed"` on exception, `"success"` otherwise. Warning rows render in amber (#b85c00) in the email.

---

## Classifier Flow

### Stage 1 — Python rule pre-filter (`classifier.py:run_rules()`)

**Rule 1 — `too_junior`:** rejects on title word-boundary match against:
`junior`, `intern`, `internship`, `entry level`, `entry-level`, `trainee`, `apprentice`
`"graduate"` was intentionally removed 2026-05-28 — let Haiku decide seniority on graduate roles.

**Rule 2 — FDI geography:** only fires when `is_fdi=True AND is_irish_founded=False`.

```
if is_fdi and not is_irish_founded:
    if is_fdi_allowlisted:
        geo = _check_fdi_geography_allowlisted(location_raw, url)
    else:
        geo = _check_fdi_geography(location_raw)
    if geo == "reject": → status=rejected, reason=fdi_geography
    else: → continue to Haiku
```

`_check_fdi_geography` (non-allowlisted FDIs) — Ireland-eligible list checked first (Dublin, Cork, Galway, remote EMEA, etc.); ambiguous multi-location strings ("Multiple Locations", "N Locations") auto-reject; definitive non-Ireland strings (US cities/states, London, Madrid, Berlin, Singapore, etc.) reject; unknown → pending.

`_check_fdi_geography_allowlisted` (18 allowlisted FDIs) — Ireland+UK eligible list checked first (all Irish locations plus London, Leeds, Manchester, Birmingham, Edinburgh, Glasgow, Bristol, Sheffield, Liverpool, Newcastle, Cardiff, Derry, Antrim, `uk`, `united kingdom`, `england`, `scotland`, `wales`, `remote - uk`, EMEA); ambiguous multi-location strings attempt a URL fallback before returning pending (see below); definitive non-Ireland/UK strings reject; unknown → pending.

**URL fallback for ambiguous locations** (added 2026-05-28; hyphen-normalisation fixed 2026-06-30): when `location_raw` matches `_AMBIGUOUS_LOC` ("locations", "multiple") or `_N_LOCATIONS_RE` ("N Locations"), and a `url` is provided, the function parses the Workday `/job/{Office-CC}/` path segment. It lowercases and collapses any run of hyphens to a single space via `re.sub(r'-+', ' ', office)` (so `/job/Remote---Bulgaria/` → `remote bulgaria`; the old chained `.replace('-', ' ').replace('---', ' ')` left three spaces and silently failed to match the reject marker), then checks against:
- Ireland markers (`dublin`, `ireland`, ` ie`, etc.) → `"pass"`
- UK markers (`london`, `leeds`, ` uk`, `england`, etc.) → `"pass"`
- Reject markers (US state codes ` ma`, ` ny`, ` ca`, etc.; major US cities; `sofia`, `plovdiv`, ` bg`, `singapore`, `dubai`, `barcelona`, `berlin`, `colombia`, `gibraltar`, `usa`) → `"reject"`
- No match → falls through to `"pending"`

### Stage 2 — Haiku classification (`claude-haiku-4-5-20251001`)

Receives: company name, vertical, is_fdi flag, description snippet, job title, location, summary (max 1500 chars).

Returns JSON with 10 fields: `seniority`, `employment_type`, `remote_status`, `vertical`, `location_normalised`, `sportstech_relevance`, `sportstech_relevance_reason`, `job_function`, `classification_reasoning`, `summary_excerpt`.

`sportstech_relevance` values:
- `relevant` → job stays `pending` (admin review)
- `ambiguous` → job stays `pending`
- `not_sportstech` → job set to `rejected`, reason `not_sportstech`

**`job_function` normaliser** (updated 2026-05-28): before the enum check, applies `re.sub(r'\s*\([^)]*\)\s*$', '', v)` to strip verbose Haiku responses like `"Engineering (software/hardware/devops/QA/infrastructure roles)"` down to `"Engineering"`. 8 valid values: `Engineering`, `Data & Analytics`, `Product & Design`, `Sales & Business Development`, `Marketing & Content`, `Operations`, `Customer Success`, `Other`.

`summary_excerpt` — max 400 chars, plain text. Haiku instructed to describe what the role involves day-to-day, skipping company intros and boilerplate. Null if description is too thin.

`max_tokens` = 1224 (bumped from 1024 to accommodate summary_excerpt output).

### Re-classification note

`run_classifier.py` only processes jobs with `status='pending' AND classification IS NULL`. Once classified, a job is permanently excluded from the normal classifier loop. To re-evaluate historical rejected jobs, reset them to `status='pending', classification=null, rejected_reason=null` in SQL first.

`run_reclassify_all.py` fills `job_function` on all jobs where `job_function IS NULL` (any status) — it does NOT re-evaluate accept/reject decisions.

---

## LinkedIn Adapters

Two separate adapters now cover LinkedIn jobs, split by `ats_platform`:

- **`linkedin_only`** (12 sources) → `adapters/apify_linkedin.py`, run via
  `run_linkedin_apify.py`. Queries the Apify LinkedIn Jobs Scraper actor
  (`curious_coder/linkedin-jobs-scraper`), which reads LinkedIn's own live
  `/jobs/search` endpoint directly — closed postings structurally never
  appear in its output, unlike Serper's Google-index snippets.
- **`none_found`** (46 sources) → `adapters/linkedin.py`, run via
  `run_linkedin.py`. Unchanged Serper-discovery path, documented below.

Both write `jobs.source='linkedin'` (unchanged — nothing branches on that
column's value, and it's useful to treat both as one population for
reporting) and both apply the same rule-based relevance pre-filter
(`relevance_filter.py`) before a job reaches `upsert_job`.

### Relevance pre-filter (`relevance_filter.py`)

Rule-based, NOT the Haiku classifier — applied by both LinkedIn adapters in
`fetch()`, before a job is added to the upsert list, so obvious noise never
reaches the `pending` review queue. Deliberately conservative: only an
explicit denylist match (`street team`, `forum coordinator/moderator`,
`community/content moderator`, `brand ambassador`, generic
`customer service/support rep`, `retail assistant`, `cashier`, etc.) causes
a drop. A broader "function allowlist" keyword list (engineer, data,
analyst, product, sports science, ...) is defined for reference/future
tightening but does **not** drive rejection — gating on "outside the
allowlist" would false-positive on legitimate titles that simply don't
contain a listed keyword (e.g. "Backend Developer"). This is a deliberate
interpretation of the original brief's "drop roles outside the allowlist"
instruction; revisit if false negatives (noise that slips through) turn out
to be more costly than false positives would be.

Company-level scoping is a `frozenset` of exempt `company_careers_sources.id`
values (`_ROLE_SCOPE_OVERRIDE_SOURCE_IDS`, empty today) — never a
company-name `if` branch. To take a company fully out of scope, set
`company_careers_sources.is_active=false` instead.

### Apify adapter (`adapters/apify_linkedin.py`)

- Actor: `curious_coder/linkedin-jobs-scraper` (module constant `_ACTOR`,
  swappable). Called via plain `requests` against the Apify REST API
  (`POST /v2/acts/{actor}/run-sync-get-dataset-items?token=...`) — no vendor
  SDK, consistent with how Serper is called directly elsewhere in this repo.
- One actor call per company (not batched across all 12 `linkedin_only`
  sources): the actor's output items don't carry a field tying a row back
  to its originating input search URL, so per-company calls keep company
  attribution unambiguous. Low volume by design.
- Geography: mirrors `classifier.py`'s FDI-allowlist split
  (`is_indigenous = not is_fdi or is_irish_founded`). Indigenous /
  non-allowlisted-FDI sources get one search URL (`location=Ireland`);
  allowlisted-FDI sources (`companies.fdi_classifier_allowlisted=true`) get
  two (`Ireland` + `United Kingdom`).
- Freshness gate: `MAX_JOB_AGE_DAYS` (default 30, env-overridable) checked
  against the actor's `postedAt` field via `_parse_posted_at()` (handles ISO
  timestamps and relative text like "3 days ago"). **Unparseable date →
  allow** — the opposite default from the Serper adapter's strict gate,
  because the actor's data source (LinkedIn's own live search) already
  guarantees the job is currently open; this gate only refines "how
  recently posted", not "does it still exist".
- Missing `APIFY_TOKEN`: fails cleanly, not silently. `fetch()` raises
  `_ApifyTokenMissingError`; `run()` treats it as an abort signal (same
  severity as `_SerperAuthError` in the Serper adapter) — logs once, writes
  `last_scrape_error='apify_token_missing'` on the source, sets
  `adapter.abort=True`, and returns without writing anything. The weekly
  pipeline does **not** treat this as fatal — `APIFY_TOKEN` is intentionally
  excluded from `run_weekly.py`'s required-env check, so this step degrades
  to a logged warning in the weekly email rather than aborting the whole run.
- Company-name validation: **always runs**, regardless of whether
  `linkedin_search_name` is set — this is a deliberate divergence from the
  Serper adapter's `_validate_name`, which safely skips the equality check
  under `override=True` because Serper's `site:linkedin.com/jobs/view "X"`
  query is a precise quoted-phrase Google search. The Apify actor instead
  runs LinkedIn's own loose native keyword search (`keywords=X&location=Y`),
  which surfaces anything LinkedIn's relevance ranking associates with the
  term, not just postings at that company. Confirmed live (2026-07-14): with
  the override-skips-validation behaviour ported over unchanged, the three
  sources with an override set (Danu Sport, EA Sports, Stats Perform)
  returned 25/25, 24/25, and 22/25 completely unrelated companies. `override`
  (`linkedin_search_name` set) now only changes *which* name is compared —
  never whether it's compared. Reuses `_normalise_company_name`/
  `_names_match`/`_strip_html`/`_format_salary` imported directly from
  `adapters/linkedin.py` (these encode LinkedIn-specific semantics both
  adapters must agree on; this repo's general convention is otherwise
  self-contained per-adapter helpers). `_normalise_company_name` strips a
  trailing parenthetical (e.g. LinkedIn's `"Electronic Arts (EA)"` →
  `"electronic arts"`) in addition to legal suffixes — added after EA
  Sports' own genuine posting was rejected as a name mismatch without it.

### Serper adapter (`adapters/linkedin.py`)

Five-stage process per company:

**Stage 1 — Serper discovery**
Query format:
- Indigenous companies: `site:linkedin.com/jobs/view "{search_name}"`
- FDI companies: `site:linkedin.com/jobs/view "{search_name}" Ireland`

`linkedin_search_name` field on `company_careers_sources` overrides `company_name` in the query. Active overrides as of 2026-05-28: EA Sports → `"Electronic Arts"`, Stats Perform → `"Stats Perform"`, ggCircuit → `"ggCircuit"`, Orreco → `"ORRECO"`, Off The Ball, Clubforce, Danu Sports (exact names set per source row).

Recency window: the Serper payload includes `tbs=_SERPER_RECENCY_TBS` (`"qdr:m"` — past month) so discovery only returns recently-posted listings rather than ranking by relevance. Widen (`qdr:y`) or narrow (`qdr:w`) via the module constant.

Returns up to 10 LinkedIn job-view URLs from Serper organic results.

**Stage 2 — Domain filter**
Indigenous companies: accept `ie.linkedin.com` and `www.linkedin.com`.
FDI companies: accept `ie.linkedin.com` only.

**Stage 3 — LinkedIn page fetch**
GET with rotating User-Agent and 1.5–2.5s throttle. Session refreshed with 60–90s sleep every 25 fetches. Returns raw HTML, or `_RATE_LIMITED` sentinel on 999/429, or `None` on error.

**Stage 4 — Name validation**
Parses `hiringOrganization.name` from JSON-LD. Normalises both names (lowercase, strip legal suffixes, collapse whitespace). Accepts exact match and trailing-s variation (Sport/Sports). When `linkedin_search_name` override is set: skips equality check, still rejects if hiring_org is absent.

**Stage 5 — Posted-age check** (added 2026-05-28; made strict 2026-06-30)
Constants:
- `MAX_POSTED_AGE_DAYS = 90` — max age when a posted date IS parseable.
- `MIN_LINKEDIN_JOB_ID = 4_200_000_000` — job-ID floor used when no date is parseable. LinkedIn job IDs are monotonic over time; June 2026 postings are ~4.40e9, so this floor (~95% of current) rejects 2025-and-earlier IDs including legacy 8-digit ~2015 listings. **To refresh:** open a known-recent LinkedIn job, read the trailing numeric ID from its URL, set the floor to ~95% of it.

`_extract_posted_days_ago(html)`:
1. JSON-LD `datePosted` ISO timestamp (preferred — precise).
2. Regex fallback: `"(?:Posted|Reposted)\s+(\d+)\s+(hour|day|week|month|year)s?\s+ago"` — converts hours→0 days, weeks→×7, months→×30, years→×365.
3. Returns `None` if neither method finds a date.

`_extract_job_id(url)`: parses the trailing numeric ID from a `/jobs/view/` URL, stripping any query string / `refId` / fragment first so a refId's own digits aren't misread. Returns `int` or `None`.

> **Note:** LinkedIn serves scraper IPs a stripped page with no parseable `datePosted` on ~100% of fetches, so in practice `_extract_posted_days_ago` returns `None` and the **job-ID floor is the primary recency gate**, not a backstop.

Behaviour (strict — the default flipped from allow to reject):
- date found & `> MAX_POSTED_AGE_DAYS` → reject `"posted_too_old (N days)"`, count `stale_age`.
- no date, job ID `< MIN_LINKEDIN_JOB_ID` → reject `"stale_id (N)"`, count `stale_id`.
- no date, no usable job ID → reject `"posted_age_unknown"`, count `age_unknown`.
- no date, job ID `>= MIN_LINKEDIN_JOB_ID` → allow (recent enough by ID).
- date found & within range → allow.

This block is unconditional — it runs on every job, FDI or indigenous. It runs before the relevance pre-filter (Stage 5).

Per-source summary log format:
```
linkedin: '{company}' serper=N domain_filter=N fetched=N validated=N
errors: 999=N parse=N name_mismatch=N stale_age=N stale_id=N age_unknown=N relevance=N bypassed=N
```

**Source tracking** (`run()` override): mirrors `BaseAdapter.run()` via a `try/finally` — always `mark_source_attempted`, plus `mark_source_successful` when `upserted_count > 0`. Runs on every path including Serper-no-results, so the archive sweep health gate sees LinkedIn sources and result-less sources (ggCircuit/Orreco) are no longer reported as "never scraped". `adapters/apify_linkedin.py` follows the identical `try/finally` pattern using the same `mark_source_successful`/`mark_source_attempted` helpers.

---

## Archive Sweep (`run_archive_sweep.py`)

Runs after classifier each week. Operates only on jobs with `status IN ('approved', 'pending')` — rejected jobs are never archived.

For each such job:
1. Source must have a recorded `last_successful_scrape_at`.
2. Source health gate: `last_successful_scrape_at >= now - 8 days`. If unhealthy (failing adapter), job is skipped.
3. `cutoff = last_successful_scrape_at - 8 days`. If `last_seen_in_scrape_run < cutoff` → archive.
4. Jobs with `NULL last_seen_in_scrape_run` (legacy rows) are always exempt.

---

## FDI Allowlist

18 FDI sportstech companies have `fdi_classifier_allowlisted=true` on their `companies` row (set 2026-05-28). These companies' jobs bypass the strict Ireland-only geography and instead use `_check_fdi_geography_allowlisted` (Ireland+UK eligible):

Blizzard Entertainment, Catapult, DraftKings, EA Sports, Fanatics, Fitbit, Flutter Entertainment, Genius Sports, ggCircuit, Hudl, LiveScore, PFF (Pro Football Focus), Riot Games, Stats Perform, Strava, Teamworks, Thrive Global, WHOOP.

To add a new company to the allowlist: `UPDATE companies SET fdi_classifier_allowlisted = true WHERE name = '...';` — then verify an active `company_careers_sources` row exists for it.

---

## News Pipeline (summary)

**Sources:** 9 direct site RSS/scrape feeds + 62 Google News queries + Supabase company feeds (up to 150 indigenous Irish companies queried by name + Ireland). Full detail in `news_pipeline.py`.

**Scoring tiers:** HIGH (cap 15), MEDIUM (cap 5), LOW (cap 3), TECH_NEWS (cap 10, sport-keyword filtered), BROADSHEET (cap 5, sportstech-keyword filtered), GOOGLE_NEWS (cap 10), businesspost.ie (cap 10).

**Scores:**
- 5: Irish sportstech company — funding, launch, award, expansion
- 4: Irish sports org adopting tech, Irish sportstech person, Irish legal/regulatory with direct sportstech impact
- 3: European sportstech relevant to Irish audience; Irish legal/regulatory commentary on sport
- 2: Irish sports without tech angle, operations roles
- 1: Off-topic, duplicate

Alerts and hub upserts fire for score 3+. `relevance` field (email-only, not persisted) added 2026-05-13 for scores 3 and 4.

**Hub RPCs (never modify signatures):**
- `upsert_news_item_if_higher_score` (12 args)
- `upsert_job` (10 args)
- `upsert_event_if_new` (14 args)

---

## Events Pipeline (summary)

5 adapters: `sport_for_business` (10–20 URLs), `eventbrite_ireland` (30 capped), `meetup` (5–15), `irish_diversity_in_tech` (10–20, allowlist-filtered), `ai_tinkerers_dublin` (0, Cloudflare 403 blocked).

Extractor output fields: `name`, `date`, `end_date`, `start_time`, `location`, `area`, `format`, `organiser`, `description`, `image_url`, `recurrence`, `relevance_category` (sportstech | ai_tech_ireland | startup_opportunity | not_relevant), `relevance_reason`, `extraction_confidence`.

### Post-extraction auto-triage (added 2026-07-14)

`relevance_category='not_relevant'` is filtered before insert (unchanged, original
behaviour). Two further auto-reject steps run inline in `weekly/runner.py`, immediately
after a successful `upsert_event()`, both guarded so they only ever act on rows still
`status='pending'` (never overwrite a human's prior review decision):

- **`ai_tech_ireland` auto-reject** — this category's real approval rate was measured at
  ~1% (68 pending + 26 rejected + 1 verified, see STATUS.md 2026-07-14 audit), against
  ~12–18% for `sportstech`/`startup_opportunity`. The extractor prompt is unchanged (Claude
  still classifies and tags it for the audit trail in `classification`); `runner.py` calls
  `mark_event_auto_rejected(event_id, "ai_tech_ireland_auto_reject")` right after upsert.
- **Recurring-series collapse** — a weekly/monthly meetup re-enters `pending` every run
  under a genuinely distinct URL per occurrence (not a de-dup bug — each is a real, separate
  event page). `collapse_recurring_series(name, recurrence)` keeps only the soonest-dated
  pending row among exact-name matches with non-null `recurrence` + non-null `date`,
  rejecting the rest with `rejected_reason='recurring_series_superseded'`. Undated
  duplicates are left alone — no way to determine "next occurrence" ordering without a date.

### Archive sweep (`run_archive_sweep.py`, added 2026-07-14)

Events have no `archived` status (no CHECK constraint on `events.status` — only
pending/rejected/verified are used in practice), so this reuses `rejected` with
`rejected_reason='event_date_passed'` rather than inventing a new status value the hub
frontend may not render. Rejects `status='pending' AND date < today`; null-date pending
events are left untouched (separate extraction-quality issue). Unlike jobs' classifier/sweep
steps (subprocess + stdout-regex parsing), this exposes `run_sweep(dry_run: bool) -> dict`
for direct import — a fast pure-DB operation with no LLM call to isolate, so the extra
complexity of subprocess isolation wasn't warranted. Wired into `run_weekly_events.py`
between extraction and the DB snapshot.

Before this existed, nothing ever removed a stale pending event from the review queue —
the 2026-07-14 audit found the oldest pending row was from **2025-02-24** (17 months stale).
One-off cleanup that session: 50 stale-dated pending events rejected via this sweep, plus a
separate one-off bulk SQL pass for the `ai_tech_ireland` backlog not already caught by
staleness (31 more) — net 86 pending → 5 pending.
