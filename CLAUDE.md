# sportstech-digest — Claude Code Context

## What this repo is

A Python research pipeline that scrapes Irish sportstech news and jobs, scores articles with Claude, and emails results to Iddo Diamant (iddodiamant@gmail.com).

## Key files

| File | Purpose |
|------|---------|
| `daily_monitor.py` | Fetches Google News RSS, scores with Claude, sends email alerts for score 4/5 articles with LinkedIn post drafts |
| `digest.py` | Monthly: scores `news_raw_YYYY-MM.json`, writes `research/YYYY-MM-research.md`, emails as attachment |
| `news_pipeline.py` | Scrapes RSS + direct sources, writes `news_raw_YYYY-MM.json` |
| `enhanced_sportstech_job_scraper_v3.py` | Scrapes Irish sportstech jobs from Adzuna + LinkedIn, writes CSV |
| `supabase_client.py` | Writes scored articles to the hub's Supabase `news_items` table |

## Scoring scale

| Score | Meaning |
|-------|---------|
| 5 | Irish sportstech company — funding, product launch, award, expansion |
| 4 | Irish sports org adopting tech, Irish sportstech person, Irish adjacent |
| 3 | European sportstech news relevant to Irish audience |
| 2 | Irish sports without tech angle, operations roles |
| 1 | Off-topic, no sports angle, duplicate |

Email alerts fire for **score 4 and 5 only**. Supabase upserts happen for **score 3+**.

## Environment variables required

```
ANTHROPIC_API_KEY
ADZUNA_APP_ID
ADZUNA_APP_KEY
SENDGRID_API_KEY
ALERT_FROM
ALERT_TO
NEXT_PUBLIC_SUPABASE_URL
NEXT_PUBLIC_SUPABASE_ANON_KEY   (informational — pipeline uses service role key)
SUPABASE_SERVICE_ROLE_KEY
```

## GitHub Actions

- `.github/workflows/daily_monitor.yml` — runs `daily_monitor.py` at 9am UTC daily; commits `daily_monitor_seen.json`
- `.github/workflows/monthly.yml` — runs the full pipeline on the 1st of each month

## Do not change

- The daily email with LinkedIn draft (fires for score 4/5)
- The monthly email with markdown attachment
- The `daily_monitor_seen.json` dedup logic
- The monthly research markdown output
- The existing 1–5 scoring criteria

---

## Changes Applied 18 April 2026

### Hardened scraper pipeline (prior)
- Retry logic on Claude API calls in `daily_monitor.py`
- Extended lookback window to 72 hours
- Fixed feed sources and broadened queries in `news_pipeline.py`
- `daily_monitor_seen.json` persisted across Actions runs

### Hub Integration (18 April 2026)

Added a write path from the digest pipeline to the **Sports D3c0d3d Intelligence Hub** Supabase database. Emails continue to fire unchanged.

**New files / changes:**

- `supabase_client.py` — Supabase client module exposing `build_news_item(article, scoring_result)` and `upsert_news_item(item)`. Uses the service role key (bypasses RLS). Tries the `upsert_news_item_if_higher_score` RPC first; falls back to SELECT + INSERT/UPDATE if the RPC is not available. Failures are logged and never crash the pipeline.
- `daily_monitor.py` — After story dedup, upserts all score 3+ new articles to Supabase before sending emails.
- `digest.py` — After writing the markdown, upserts all score 3+ articles to Supabase before sending the monthly email.
- Both scoring prompts now return additional fields per article: `score_reason`, `summary` (40-60 words, editorial tone), `tags` (3-5 keywords), `verticals` (1-2 from closed list), `mentioned_companies`.
- `requirements.txt` — added `supabase==2.*`
- `.env.example` — documents all required env vars including Supabase keys
- Both workflow files now pass `NEXT_PUBLIC_SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`

**New env vars required (add to GitHub Secrets):**
- `NEXT_PUBLIC_SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

**Verticals closed list:**
Performance Analytics | Wearables & Hardware | Fan Engagement | Media & Broadcasting | Health, Fitness and Wellbeing | Scouting & Recruitment | Esports & Gaming | Betting & Fantasy | Stadium & Event Tech | Club Management Software | Sports Education & Coaching | Other / Emerging
