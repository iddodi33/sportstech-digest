# Sports D3c0d3d — Research Agent
## CLAUDE.md — Project Context for AI Assistants

*Last updated: 18 April 2026*

---

## What This Project Does

Automated research pipeline for **Sports D3c0d3d**, a monthly Irish sportstech newsletter (~100 subscribers on Beehiiv). The agent does two things:

1. **Daily monitor** — scans sportstech news every day, emails score 4-5 articles to the editor with a ready-to-post LinkedIn draft
2. **Monthly digest** — runs on the 1st of each month, scrapes jobs + news, outputs a structured research markdown file and emails it to the editor as an attachment

The editor (Iddo) manually reviews output, picks ~3 top stories, ~6 jobs, and writes the newsletter in Beehiiv.

---

## Repo Structure

```
sportstech-digest/
├── enhanced_sportstech_job_scraper_v3.py  # LinkedIn + WHOOP + Adzuna job scraping
├── news_pipeline.py                        # Google News RSS + direct site RSS + cloudscraper
├── digest.py                               # Claude API scoring + markdown research file + email
├── daily_monitor.py                        # Daily alert + LinkedIn post generation + SendGrid email
├── run_monthly.sh                          # Orchestrates monthly pipeline
├── requirements.txt
├── .env                                    # Never commit this
├── .gitignore                              # Excepts: sportstech_jobs_ireland.csv, news_raw_*.json, daily_monitor_seen.json, research/*.md
├── daily_monitor_seen.json                 # Dedup tracker for daily monitor (committed back to repo each run)
├── sportstech_jobs_ireland.csv             # Output of job scraper (archived)
├── news_raw_YYYY-MM.json                   # Output of news pipeline (archived)
├── research/YYYY-MM-research.md            # Final monthly research output (archived)
├── logs/                                   # Run logs
└── .github/workflows/
    ├── daily_monitor.yml                   # Cron: 0 9 * * * (9am UTC daily), commits seen tracker back
    └── monthly.yml                         # Cron: 0 9 1 * * (9am on 1st of month), commits outputs back
```

---

## Tech Stack

- **Python 3.10+** on Windows (local) / Ubuntu 24.04 (GitHub Actions)
- **feedparser** — RSS fetching
- **requests + BeautifulSoup + lxml** — scraping
- **cloudscraper** — Cloudflare-protected sites (businesspost.ie)
- **anthropic** — Claude API (model: claude-sonnet-4-5-20250929)
- **sendgrid** — email delivery
- **pandas** — job CSV output
- **GitHub Actions** — scheduling + commit-back for state persistence

---

## Environment Variables (.env / GitHub Secrets)

```
ANTHROPIC_API_KEY=
SENDGRID_API_KEY=
ADZUNA_APP_ID=07a66aae
ADZUNA_APP_KEY=
ALERT_FROM=monitor@sportsd3c0d3d.ie
ALERT_TO=iddodiamant@gmail.com
```

GitHub Actions secrets required: `ANTHROPIC_API_KEY`, `SENDGRID_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`.

**Critical**: `ALERT_FROM` must be `monitor@sportsd3c0d3d.ie` (the domain authenticated in SendGrid). Using a Gmail address here causes SendGrid to silently fail due to DMARC policy.

---

## News Sources

### Direct Site RSS Feeds (in news_pipeline.py)

| Source | Method | Notes |
|--------|--------|-------|
| siliconrepublic.com | RSS | Cap 15 |
| sportforbusiness.com | RSS | Cap 15 |
| thinkbusiness.ie | Scrape | RSS malformed, scrape fallback works |
| businessplus.ie | RSS | Cap 5 |
| techcentral.ie | RSS | Cap 5 |
| irishtechnews.ie | RSS (Feedburner) | Uses http://feeds.feedburner.com/IrishTechNews (direct feed is malformed) |
| bebeez.eu | RSS | Cap 15, Ireland/UK filter |
| businesspost.ie | cloudscraper | Bypasses Cloudflare; also covered via Google News site: queries |
| enterprise-ireland.com | HTML scrape | Custom scraper for /en/news page |
| sportireland.ie | Scrape | RSS malformed, scrape fallback works |

### Google News RSS Queries

**Ireland-specific:** sportstech ireland, sports technology ireland, sports startup ireland, irish sports tech funding, sports analytics ireland, digital sport ireland, fan engagement ireland, fitness startup ireland, stadium technology ireland, esports ireland, wellness startup ireland

**Named Irish companies:** kitman labs, output sports, orreco ireland, statsports, tixserve, enterprise ireland sports, sport ireland technology, wiistream ireland, clubforce ireland, trojantrack, sports impact technologies ireland, feenix group ireland, anyscor ireland, locker app ireland sports, precision sports technology ireland, headhawk ireland, teamfeepay ireland, clubspot ireland, revelate fitness ireland

**Named-entity queries (added Apr 2026):** Kitman Labs funding, STATSports Northern Ireland, Orreco sports science, Clubforce Ireland sport, Leinster Rugby data analytics, IRFU technology, FAI technology, GAA analytics technology, Munster Rugby data, Connacht Rugby analytics, Ulster Rugby technology

**Ecosystem people (broadened Apr 2026):** "Keith Brock" sportstech, "Keith Brock" Enterprise Ireland sport, "Aimée Williams" sportstech, "Aimee Williams" sportstech, "Rob Hartnett" sport, "Trev Keane" Feenix, "Colin Deering" sport, Anyscor Ireland, Feenix Group Ireland

**Publications:** "Irish Times" sports technology, "Irish Independent" sportstech, "Irish Examiner" sport tech, "Business Post" sportstech

**Business Post Google News fallback:** site:businesspost.ie sportstech, site:businesspost.ie sport technology, site:businesspost.ie "sports tech" Ireland

**Europe:** sportstech europe startup, sports technology funding europe

### Pool Size (as of Apr 2026)
- Monthly pipeline fetches ~2,800 raw entries → 166 articles in final pool after date/cap/dedup filtering
- Daily monitor fetches ~430 entries → typically 0-3 within 72-hour window

---

## Claude Scoring Criteria (digest.py + daily_monitor.py)

**Model:** `claude-sonnet-4-5-20250929` (updated Apr 2026, off deprecated claude-sonnet-4-20250514)

**Score 5 — Irish sportstech direct:**
- Irish sportstech startup: funding, award, product launch, international expansion
- Irish company entering new market or announcing partnership
- Real examples: "ClubSpot scales global from Cavan", "Torpey Glove shortlisted for global award", "Output Sports x HYROX365 partnership", "TeamFeePay bags £9m"

**Score 4 — Irish sports + tech adjacent:**
- Irish sports org adopting technology (GAA, IRFU, FAI, Leinster Rugby)
- Irish sportstech person featured in press
- Keith Brock / Rob Hartnett / Aimée Williams ecosystem commentary
- Real examples: "Leinster Rugby using data for fan experience", "New Sport Ireland Board members appointed"

**Score 3 — European/global sportstech relevant to Irish audience:**
- European sportstech funding rounds
- Global industry reports on sportstech trends
- International partnerships with Irish angle

**Score 2 — Surface but low editorial value:**
- Irish sports governance without tech angle
- Sports industry awards without tech company involvement

**Score 1 — Exclude:**
- No sports angle whatsoever
- Pure politics, property, crime, lifestyle
- Exact duplicates

---

## Jobs Pipeline (enhanced_sportstech_job_scraper_v3.py)

### Sources
- **LinkedIn** — keyword searches in Ireland (5 pages each)
- **WHOOP Careers** — Lever ATS, filtered to Ireland/EMEA/Remote only
- **Adzuna API** — currently returning 404, low priority as LinkedIn gives good coverage
- **Indeed RSS** — free tier, limited results

### Company Tiers
**CORE_SPORTSTECH_COMPANIES (score +8):** Kitman Labs, Output Sports, Orreco, STATSports, Stats Perform, TixServe, Playermaker, Wiistream, Clubforce, Xtremepush, TrojanTrack, Sports Impact Technologies, Revelate Fitness, Feenix Group, Anyscor, Fanatics, Flutter Entertainment, FanDuel, Betfair, BoyleSports, Teamworks, Catapult, Hudl, Sportradar, Genius Sports, Glofox, LegitFit, Web Summit, WHOOP, Sport Ireland, IRFU, FAI, GAA, Leinster/Munster/Ulster/Connacht Rugby

**EXCLUDE_COMPANIES:** Yahoo, UPMC, CarTrawler, An Post, Susquehanna, TRACTIAN, Spanish Point, Accenture, Deloitte, PwC, KPMG, Ernst & Young, Amazon Web, Microsoft, Apple Inc, Meta Platforms, IBM Ireland

### Location Filter (digest.py jobs section)
**Keep:** Ireland, Dublin, Cork, Galway, Limerick, Belfast, Waterford, Remote, Hybrid
**Exclude:** Boston, New York, NYC, United States, US, Texas, California, Chicago, "Remote (US)"

---

## Daily Monitor (daily_monitor.py)

**Schedule:** 9am UTC every day via GitHub Actions

**Lookback window:** 72 hours (updated Apr 2026 from 25h — Irish ecosystem too thin for a 25h window)

**Minimum score to email:** 4

**Flow:**
1. Fetch all Google News RSS feeds with cache-busting headers
2. Parse dates robustly, apply 72h freshness filter
3. Score with Claude in batches of 15 using retry-with-backoff helper
4. Deduplicate by story (Claude groups similar titles)
5. Resolve Google News redirect URLs to real article URLs
6. Generate LinkedIn post draft per qualifying article
7. Send email via SendGrid (logs response status code)
8. Update daily_monitor_seen.json to prevent re-sending
9. Commit seen tracker back to repo via GitHub Actions

**Retry logic:** 4 attempts with 5s/15s/30s backoff on APIConnectionError, APIStatusError, InternalServerError

**Email format:** Subject: `⚡ [Score X/5] Article Title`
**Recipient:** iddodiamant@gmail.com
**Sender:** monitor@sportsd3c0d3d.ie (domain authenticated via Blacknight DNS)

---

## Monthly Digest (digest.py + monthly.yml)

**Schedule:** 9am UTC on the 1st of each month

**Flow:**
1. `enhanced_sportstech_job_scraper_v3.py` → generates sportstech_jobs_ireland.csv
2. `news_pipeline.py` → generates news_raw_YYYY-MM.json
3. `digest.py` → scores articles, filters jobs, writes research/YYYY-MM-research.md
4. `digest.py` emails the markdown file as a SendGrid attachment
5. GitHub Actions commits research markdown, jobs CSV, and news JSON back to the repo

**Email format:**
- Subject: `Sports D3c0d3d Monthly Research — YYYY-MM`
- Body: Short intro text
- Attachment: Full markdown file, base64-encoded

---

## LinkedIn Post Style (for daily_monitor.py prompt)

Iddo's voice — key rules:
- Never starts with "I" or "Exciting news" or "Delighted to share"
- Short punchy opener — statement or observation
- Short sentences, lots of white space
- → arrow lists for specifics
- Adds ecosystem-builder perspective, not just summarising
- Tags companies/people as [Company Name] placeholders
- 3-5 hashtags: always #SportsTech, plus #IrishSportsTech #SportsInnovation #Innovation #Entrepreneurship #SportsData #FanEngagement as relevant
- Forward-looking closer, not a call to action
- 150-250 words
- Ends with "Link in comments 👇" then URL

---

## Monthly Digest Output Format (research/YYYY-MM-research.md)

```
# Sports D3c0d3d — Research Digest YYYY-MM

## ⭐ Score 5 — Irish Sportstech Direct
| Score | Reason | Category | Title | Source | Date | Link | Summary |

## Score 4 — Irish Sports + Tech Adjacent

## Score 3 — European / Global Sportstech

## 🔇 Low Relevance (scores 1-2)

## 💼 Jobs Longlist
| Tier | Title | Company | Location | Source | Link |

## 📊 Run Stats
```

**Typical volumes (Apr 2026 reference):** Score 5: ~6, Score 4: ~2, Score 3: ~13, Low relevance: ~100. Jobs: ~45-77 after location + quality filter.

---

## GitHub Actions Configuration

### daily_monitor.yml
- Cron: `0 9 * * *` (9am UTC daily)
- `permissions: contents: write` for commit-back
- Runs `python daily_monitor.py`
- Final step commits `daily_monitor_seen.json` back to repo so dedup persists across runs
- Env vars: `ANTHROPIC_API_KEY`, `SENDGRID_API_KEY`, `ALERT_FROM`, `ALERT_TO`

### monthly.yml
- Cron: `0 9 1 * *` (9am UTC on the 1st)
- `permissions: contents: write` for commit-back
- Runs `enhanced_sportstech_job_scraper_v3.py` → `news_pipeline.py` → `digest.py` in sequence
- Final step commits `research/`, `sportstech_jobs_ireland.csv`, `news_raw_*.json` back to repo
- Env vars: same as daily + `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`

---

## .gitignore Strategy

```
.env
*.csv
*.json
!sportstech_jobs_ireland.csv
!news_raw_*.json
!daily_monitor_seen.json
*.log
logs/
__pycache__/
*.pyc
research/
!research/*.md
.venv/
```

The `!` exceptions allow specific generated files to be committed back by the workflows, creating a historical archive in the repo.

---

## Changes Applied 18 April 2026

### Bug fixes
- `ALERT_FROM` corrected from `iddodiamant@gmail.com` to `monitor@sportsd3c0d3d.ie` (SendGrid DMARC issue was silently blocking emails)
- `daily_monitor_seen.json` now persists across GitHub Actions runs (previously reset every run, breaking dedup)
- `ANTHROPIC_API_KEY` GitHub secret was blank — re-added, causing prior "Connection error" failures

### Resilience improvements
- Retry-with-backoff wrapper around all Claude API calls (4 attempts, 5s/15s/30s)
- SendGrid response status code logging (previously silent on failures)
- SDK's internal retries disabled (`max_retries=0`) so only our wrapper handles retries
- 60-second HTTP timeout on Anthropic client

### Coverage improvements
- Lookback window: 25h → 72h (thin Irish ecosystem)
- Model updated: `claude-sonnet-4-20250514` → `claude-sonnet-4-5-20250929` (deprecated)
- Irish Tech News RSS fixed: now uses Feedburner URL (direct feed malformed)
- Enterprise Ireland: custom HTML scraper for `/en/news` page
- Business Post restored: cloudscraper bypass + Google News `site:` queries as dual coverage
- Broadened ecosystem people queries (dropped forced co-occurrence and "linkedin" keyword)
- Added 11 named-entity queries (Munster/Ulster/IRFU/FAI/GAA/etc.)

### New features
- `digest.py` now emails the monthly research markdown as a SendGrid attachment
- Monthly workflow commits research/jobs/news artefacts back to repo for historical archive

### Removed
- Adzuna job scraper 404 errors — still broken, deprioritised (LinkedIn covers job queries)

---

## Known Issues / Outstanding Items

- **Adzuna API 404** — URL format needs fixing, low priority
- **"Work at Web Summit" placeholder job** — add to title exclusion list
- **WHOOP Remote roles** — "North American" in title should be excluded even if location says "Remote"
- **Node.js 20 deprecation warning** — `actions/checkout@v3` and `actions/setup-python@v4` need upgrading to v4/v5 before Sept 2026
- **Ecosystem people spiky** — Aimée Williams, Colin Deering queries frequently return 0 results; expected behaviour, not broken

---

## Key Ecosystem People to Monitor

| Person | Role | Why |
|--------|------|-----|
| Keith Brock | Enterprise Ireland, HPSU sportstech lead | Investment signals, new companies getting EI backing |
| Rob Hartnett | CEO, Sport for Business | Broadest Irish sports business lens |
| Aimée Williams | IDA Ireland, sportstech | FDI angle, multinationals landing in Ireland |
| Trev Keane | Co-founder, Feenix Group | Esports, gaming, youth fan engagement |
| Colin Deering | Founder/CEO, Anyscor | Grassroots sportstech, amateur sport |

---

## Newsletter Context

**Name:** Sports D3c0d3d
**Platform:** Beehiiv (sportsd3c0d3d.beehiiv.com)
**Frequency:** Monthly
**Subscribers:** ~100
**Audience:** Irish sportstech professionals, founders, ecosystem participants

**Newsletter sections:**
1. 🚀 Top Stories — 3 written-up news items with editor commentary
2. 🦄 Report in Focus — one deeper report/whitepaper (found manually)
3. 🗓️ Events — upcoming sportstech events, Ireland-first
4. 🔥 Career Opportunities — 5-6 curated jobs, title/company/location/link only

**Editorial style:** Ecosystem-builder perspective, not just reporting. Iddo adds his own take on what news means for the Irish sportstech community.

---

## Running Locally

```powershell
# Full monthly pipeline (Windows PowerShell)
python enhanced_sportstech_job_scraper_v3.py
python news_pipeline.py
python digest.py

# Daily monitor
python daily_monitor.py

# Test daily monitor (ignore seen file)
'{"seen_urls": []}' | Out-File -FilePath daily_monitor_seen.json -Encoding utf8
python daily_monitor.py
```

```bash
# Full monthly pipeline (bash)
bash run_monthly.sh

# Reset seen file (bash)
echo '{"seen_urls": []}' > daily_monitor_seen.json
python daily_monitor.py
```

---

## When Making Changes

- Always wrap external calls in try/except — never crash the pipeline because one source fails
- Batch Claude API calls at 15 articles max per call to avoid JSON truncation
- Use the `_call_claude_with_retry` helper for all `client.messages.create()` calls
- Use model `claude-sonnet-4-5-20250929` throughout
- Load env vars with python-dotenv at top of each file
- Log failures to file, don't just print
- Test locally before pushing — GitHub Actions runs are harder to debug
- Before concluding "it's a network issue" on GitHub Actions, verify all repo secrets have values (the "Connection error" we saw was actually a blank `ANTHROPIC_API_KEY`)
