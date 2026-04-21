# sportstech-digest — Claude Code Context

*Last updated: 18 April 2026*

---

## What this repo is

A Python research pipeline that scrapes Irish sportstech news and jobs, scores articles with Claude, and does three things:

1. **Emails Iddo** daily alerts for score 3+ articles with LinkedIn post drafts
2. **Produces a monthly research markdown** on the 1st, emailed as an attachment
3. **Writes scored articles to the Sports D3c0d3d Intelligence Hub Supabase** where they're reviewed in `/admin/news` and published to `/news`

---

## Key files

| File | Purpose |
|------|---------|
| `daily_monitor.py` | Fetches Google News RSS, scores with Claude, upserts score 3+ to hub Supabase, sends email alerts for score 3+ with LinkedIn post drafts |
| `digest.py` | Monthly: scores `news_raw_YYYY-MM.json`, writes `research/YYYY-MM-research.md`, upserts score 3+ to hub Supabase, emails markdown as attachment |
| `news_pipeline.py` | Scrapes RSS + direct sources, decodes Google News redirects via googlenewsdecoder, writes `news_raw_YYYY-MM.json` |
| `enhanced_sportstech_job_scraper_v3.py` | Scrapes Irish sportstech jobs from LinkedIn, WHOOP, Adzuna, writes CSV |
| `supabase_client.py` | Writes scored articles to the hub's Supabase `news_items` table. Handles publisher name extraction, og:image and og:title fetching, and conditional upsert via RPC |

---

## Scoring scale

| Score | Meaning |
|-------|---------|
| 5 | Irish sportstech company — funding, product launch, award, expansion |
| 4 | Irish sports org adopting tech, Irish sportstech person, Irish adjacent |
| 3 | European sportstech news relevant to Irish audience |
| 2 | Irish sports without tech angle, operations roles |
| 1 | Off-topic, no sports angle, duplicate |

Email alerts fire for **score 3+** (lowered from 4/5 on 21 Apr 2026 after decision to surface European sportstech research angles for content). Supabase upserts happen for **score 3+**.

---

## Claude scoring prompt (important)

The scoring prompt in both `daily_monitor.py` and `digest.py` returns JSON with these fields per article:

- `score` (1-5)
- `score_reason` (short rationale, 1 sentence)
- `summary` (exactly 2 sentences, 40-60 words total; sentence 1 = what happened who where; sentence 2 = why it matters)
- `tags` (3-5 keyword strings)
- `verticals` (1-2 from closed hub list)
- `mentioned_companies` (list of company names from article)

Closed vertical list (must match hub exactly):
Performance Analytics | Wearables & Hardware | Fan Engagement | Media & Broadcasting | Health, Fitness and Wellbeing | Scouting & Recruitment | Esports & Gaming | Betting & Fantasy | Stadium & Event Tech | Club Management Software | Sports Education & Coaching | Other / Emerging

Summary instruction includes BAD/GOOD examples inside the prompt string (not just comments) so Claude sees them at inference time.

---

## Google News URL resolution

Google News RSS returns proxied URLs like `https://news.google.com/rss/articles/CBMi...`. These must be decoded to the real article URL or OG extraction breaks.

- `daily_monitor.py` `_extract_real_url()`: uses `googlenewsdecoder` for in-window articles. Out-of-window articles fall through to Google search fallback (harmless since never scored or upserted).
- `news_pipeline.py` `_decode_google_news_url()`: decodes Google News URLs in feeds after `entry.link` extraction. Falls back to original URL on decode failure, logs warning.

**Earlier bug fixed 18 Apr 2026:** `_extract_real_url()` used to return `entry.source.href` which is a publisher homepage label, not the article URL. That made every row's URL a root domain (like `https://mshale.com`), breaking OG extraction. The fix removed that step entirely.

---

## OG metadata extraction

`fetch_og_metadata(url)` in `supabase_client.py`:

- Fetches article page with realistic User-Agent, 10s timeout
- Extracts `og:image` (with `twitter:image` fallback) and `og:title`
- Resolves relative image URLs via `urljoin`
- Returns `{"image_url": None, "og_title": None}` on any error, never raises

`build_news_item` uses og:title when it's different from the RSS title AND ≥ 15 chars. Otherwise keeps RSS title. Preserves raw RSS title as `original_title` field always.

OG fetch only happens for score 3+ articles being upserted, not all scored articles (keeps performance acceptable).

---

## Publisher name extraction

`extract_publisher(url)` in `supabase_client.py`:

- Parses article URL domain
- Maps known Irish + international sports news domains to clean publisher names via dictionary
- Handles multi-part TLDs (`.co.uk`, `.com.au`, `.co.ie`, etc.) so e.g. `well-nation.co.uk` becomes "Well Nation"
- Falls back to title-cased domain stem for unmapped domains
- Returns clean names like "Silicon Republic", "Sport for Business", "Sustain Health Magazine" instead of the Google News query string that RSS feeds provide

---

## Hub Supabase integration

`upsert_news_item(item)` calls the RPC function `upsert_news_item_if_higher_score` in the hub's Supabase project (xwqmnofkvdwpagfweqmj). The RPC:

- Tries to INSERT the news item with status='pending'
- On URL conflict (unique constraint on url), only overwrites score/reason/summary if new score > existing
- Always overwrites `mentioned_companies` (latest enrichment wins)
- Uses COALESCE for image_url and original_title so later failed fetches don't blank out earlier good data
- 12 parameters total: url, title, source, summary, tags, verticals, published_at, score, score_reason, mentioned_companies, image_url, original_title

Fallback: if RPC is unavailable, `upsert_news_item` falls back to manual SELECT + INSERT/UPDATE. Logs failures but never crashes.

---

## Environment variables

```
ANTHROPIC_API_KEY
ADZUNA_APP_ID
ADZUNA_APP_KEY
SENDGRID_API_KEY
ALERT_FROM=monitor@sportsd3c0d3d.ie
ALERT_TO=iddodiamant@gmail.com
NEXT_PUBLIC_SUPABASE_URL=https://xwqmnofkvdwpagfweqmj.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY   (informational, pipeline uses service role)
SUPABASE_SERVICE_ROLE_KEY
```

GitHub Actions secrets required for both workflows: all of the above.

SendGrid sender domain `sportsd3c0d3d.ie` is authenticated via CNAME records at Blacknight (em7190.sportsd3c0d3d.ie, s1._domainkey, s2._domainkey). Old `em9635.sportsd3c0d3d.com` verification exists but is stale.

---

## GitHub Actions

- `.github/workflows/daily_monitor.yml` — runs `daily_monitor.py` at 9am UTC daily; commits `daily_monitor_seen.json`
- `.github/workflows/monthly.yml` — runs full pipeline on the 1st of each month at 9am UTC

---

## Do not change

- The daily email with LinkedIn draft (fires for score 3+)
- The monthly email with markdown attachment
- The `daily_monitor_seen.json` dedup logic
- The monthly research markdown output
- The existing 1–5 scoring criteria

---

## Changes Applied 18 April 2026

### Hardened scraper pipeline (early in day)
- Retry logic on Claude API calls in `daily_monitor.py`
- Extended lookback window to 72 hours
- Fixed feed sources and broadened queries in `news_pipeline.py`
- `daily_monitor_seen.json` persisted across Actions runs

### Hub Integration (mid-day)
- `supabase_client.py` created with `upsert_news_item()` + `build_news_item()` + `fetch_og_metadata()` + `extract_publisher()`
- `daily_monitor.py` upserts score 3+ articles after story dedup, before email
- `digest.py` upserts score 3+ articles after markdown write, before email
- Scoring prompts extended with score_reason, summary (2 sentences, 40-60 words), tags, verticals, mentioned_companies
- RPC function `upsert_news_item_if_higher_score` in Supabase (12 parameters)
- New env vars: `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`

### Summary prompt refinement (late afternoon)
- Summaries were too short and mechanical after initial integration
- Prompt rewritten with explicit 2-sentence structure + BAD/GOOD examples inline
- Output now reads as actual context-giving summaries, not headline paraphrases

### Publisher name extraction (late afternoon)
- Source field was storing Google News query names like `""output sports"" - Google News`
- `extract_publisher()` added to `supabase_client.py` with domain mapping dictionary
- Multi-part TLD handling added (`.co.uk`, `.com.au`, etc.)
- Backfill SQL run on existing rows to clean their source fields

### SendGrid domain authentication (early evening)
- Emails were returning 403 Forbidden since integration start
- Root cause: SendGrid had authenticated `sportsd3c0d3d.com` (wrong TLD) but we send from `sportsd3c0d3d.ie`
- Added 3 CNAME records to Blacknight DNS: em7190, s1._domainkey, s2._domainkey
- Domain now verified in SendGrid, test send returned 202
- Free trial ends 29 May 2026 (needs upgrade plan by then)

### OG image + title extraction (evening)
- Added `fetch_og_metadata()` to extract og:image and og:title from article pages
- Added `image_url` and `original_title` columns to `news_items` in Supabase
- RPC function updated from 10 args to 12 args
- `build_news_item` uses og:title when cleaner than RSS title (≥15 chars, different from RSS)

### Google News URL resolution bug fix (evening)
- `_extract_real_url()` was returning `entry.source.href` (publisher homepage) as the article URL
- All rows stored root domains instead of article paths
- Removed the bad step, added `googlenewsdecoder` package
- In-window articles now decoded to real article URLs
- `news_pipeline.py` also got `_decode_google_news_url()` for the monthly path

### Known issues / backlog
- **Adzuna API 404** — URL format needs fixing, low priority (LinkedIn covers job queries)
- **Mshale titles** — mshale.com is an aggregator that doesn't return usable og:title; articles from there get the ugly RSS title. Option: blacklist mshale.com domain, or reject in admin.
- **Business Post paywall** — og:image extraction fails on paywalled articles. Falls back to brand texture card. Accepted tradeoff.
- **SendGrid free plan** — ends 29 May 2026, needs paid upgrade for ongoing sending beyond trial

---

## Changes Applied 21 April 2026

### LinkedIn draft prompt hardening
- Company hallucination bug caught: a score 3 BBC article about football heading research generated a LinkedIn draft claiming STATSports "leads in wearable concussion tech". STATSports only makes GPS performance trackers.
- Root cause: LinkedIn draft prompt had no guardrail against pattern-matching Irish sportstech company names to story themes.
- Fix: rules added directly to `LINKEDIN_SYSTEM` prompt in `daily_monitor.py` so Claude sees them at inference time for every draft.
- Rules embedded: verify-before-naming gate, per-company capability facts (STATSports GPS only, Orreco biomarkers only, Output Sports IMU movement only, Kitman Labs software only, Hexis nutrition software only, Danu Sports smart textiles only), white-space/opportunity framing for categories where no Irish company has a genuine product, Éanna Falvey person-vs-company distinction.
- Explicit outcome allowed: a post with no Irish company reference is the correct default when nothing genuinely fits.

### Alert threshold lowered to score 3+
- Daily email alerts now fire for score 3+ (previously 4/5 only).
- Rationale: score 3 articles (European sportstech research relevant to Irish audience) are useful content angles when framed honestly. The hallucination risk that previously made score 3 alerts feel wrong is now addressed by the LINKEDIN_SYSTEM prompt hardening above.
- Supabase upsert threshold unchanged (still score 3+).
