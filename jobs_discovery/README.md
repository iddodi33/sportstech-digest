# Career Page Discovery

Probes 73 Irish sportstech companies for ATS integrations and careers pages.
The output CSV is the source-of-truth input for the weekly job scraper.

## How to run

```bash
pip install aiohttp beautifulsoup4
python jobs_discovery/discover_career_pages.py
```

## What it does

1. **Phase 1 — ATS API probe**: tries slug variants against Greenhouse, Lever, Workable,
   Ashby, SmartRecruiters, Teamtailor, Recruitee, Personio, BambooHR, and Breezy APIs.
2. **Phase 2 — Website HTML fingerprinting**: fetches `/careers`, `/jobs`, etc. and looks
   for ATS embed markers in the HTML. If a marker is found, re-probes the API with the
   extracted slug.
3. **Phase 3 — Classification**: assigns `scrapability` (easy / medium / hard / none)
   and `confidence` (high / medium / low) to each result.

Five companies are pre-seeded with manually confirmed answers and are not probed:
Kitman Labs, Output Sports, Orreco, Hexis, Clubforce.

## Output

`career_pages.csv` — one row per company with:

| Column | Description |
|--------|-------------|
| `company_id` | UUID from source list |
| `name` | Company name |
| `website` | Company website |
| `careers_url` | Best careers URL found |
| `ats_platform` | ATS name or `custom_html` / `none_found` / `linkedin_only` |
| `ats_api_endpoint` | Full JSON API URL if applicable |
| `scrapability` | `easy` / `medium` / `hard` / `none` |
| `confidence` | `high` / `medium` / `low` |
| `needs_manual_review` | `true` / `false` |
| `notes` | Flags: Cloudflare block, redirect, empty ATS, etc. |
| `discovered_at` | ISO timestamp |

## Manual review

Rows with `needs_manual_review=true` had no ATS hit and no discoverable careers page.
Check these by hand: visit the website, check LinkedIn jobs, or contact the company directly.

## Re-running

Re-running overwrites `career_pages.csv` in place. Pre-seeded rows are always written
directly without probing. HTTP probe logs are written to `probe_log.txt`.
