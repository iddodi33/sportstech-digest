# Sports D3c0d3d — Research Agent
## CLAUDE.md — Project Context for AI Assistants

---

## What This Project Does

Automated research pipeline for **Sports D3c0d3d**, a monthly Irish sportstech newsletter (~100 subscribers on Beehiiv). The agent does two things:

1. **Daily monitor** — scans sportstech news every weekday morning, emails score 4-5 articles to the editor with a ready-to-post LinkedIn draft
2. **Monthly digest** — runs on the 1st of each month, scrapes jobs + news, outputs a structured research markdown file for the editor to write from

The editor (Iddo) manually reviews output, picks ~3 top stories, ~6 jobs, and writes the newsletter in Beehiiv.

---

## Repo Structure

```
sportstech-digest/
├── enhanced_sportstech_job_scraper_v3.py  # LinkedIn + WHOOP + Adzuna job scraping
├── news_pipeline.py                        # Google News RSS + direct site RSS fetching
├── digest.py                               # Claude API scoring + markdown research file
├── daily_monitor.py                        # Daily alert + LinkedIn post generation + SendGrid email
├── run_monthly.sh                          # Orchestrates monthly pipeline
├── requirements.txt
├── .env                                    # Never commit this
├── daily_monitor_seen.json                 # Dedup tracker for daily monitor
├── sportstech_jobs_ireland.csv             # Output of job scraper
├── news_raw_YYYY-MM.json                   # Output of news pipeline
├── research/YYYY-MM-research.md            # Final monthly research output
├── logs/                                   # Run logs
└── .github/workflows/
    ├── daily_monitor.yml                   # Cron: 0 9 * * 1-5 (9am UTC weekdays)
    └── monthly.yml                         # Cron: 0 9 1 * * (9am on 1st of month)
```

---

## Tech Stack

- **Python 3.10+** on Windows (local) / Ubuntu (GitHub Actions)
- **feedparser** — RSS fetching
- **requests + BeautifulSoup** — scraping
- **anthropic** — Claude API (model: claude-sonnet-4-20250514)
- **sendgrid** — email delivery
- **pandas** — job CSV output
- **GitHub Actions** — scheduling

---

## Environment Variables (.env)

```
ANTHROPIC_API_KEY=
SENDGRID_API_KEY=
ADZUNA_APP_ID=07a66aae
ADZUNA_APP_KEY=
ALERT_FROM=monitor@sportsd3c0d3d.com
ALERT_TO=iddodiamant@gmail.com
```

GitHub Actions secrets: `ANTHROPIC_API_KEY`, `SENDGRID_API_KEY`

---

## News Sources

### Google News RSS Queries (in news_pipeline.py + daily_monitor.py)
Ireland-specific: sportstech ireland, sports technology ireland, sports startup ireland, irish sports tech funding, sports analytics ireland, digital sport ireland, fan engagement ireland, fitness startup ireland, stadium technology ireland, esports ireland, wellness startup ireland

Named Irish companies: kitman labs, output sports, orreco ireland, statsports, tixserve, enterprise ireland sports, sport ireland technology, wiistream ireland, clubforce ireland, trojantrack, sports impact technologies ireland, feenix group ireland, anyscor ireland, locker app ireland sports, precision sports technology ireland, headhawk ireland, teamfeepay ireland, clubspot ireland, revelate fitness ireland

Ecosystem people (LinkedIn posts indexed by Google): "Keith Brock" sportstech site:linkedin.com, "Rob Hartnett" site:linkedin.com, "Aimee Williams" site:linkedin.com, "Trev Keane" Feenix, "Colin Deering" Anyscor

Europe: sportstech europe startup, sports technology funding europe

### Direct Site RSS Feeds
- siliconrepublic.com (cap: 15)
- sportforbusiness.com (cap: 15)
- thinkbusiness.ie (scrape fallback — RSS broken)
- businessplus.ie (cap: 5)
- techcentral.ie (cap: 5)
- irishtechnews.ie (cap: 3)
- bebeez.eu (cap: 15, Ireland/UK filter)
- businesspost.ie (403 blocked)
- enterprise-ireland.com (scrape fallback)
- sportireland.ie (scrape fallback)

---

## Claude Scoring Criteria (digest.py + daily_monitor.py)

**Score 5 ⭐ — Irish sportstech direct:**
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
- Real examples: "20 sports tech ideas to invest in 2026", "Brussels startup raises €800k for movement analysis"

**Score 2 — Surface but low editorial value:**
- Irish sports governance without tech angle
- Sports industry awards without tech company involvement
- Real examples: "Federation of Irish Sport launches awards", "Sport Ireland gender balance study"

**Score 1 — Exclude:**
- No sports angle whatsoever
- Pure politics, property, crime, lifestyle
- Exact duplicates

---

## Jobs Pipeline (enhanced_sportstech_job_scraper_v3.py)

### Sources
- **LinkedIn** — keyword searches: "sports technology", "sports analytics", "sports data", "sports software", "sports digital", "performance technology" in Ireland (5 pages each)
- **WHOOP Careers** — Lever ATS, filtered to Ireland/EMEA/Remote only
- **Adzuna API** — currently returning 404, needs fix
- **Indeed RSS** — free tier, limited results

### Company Tiers
**CORE_SPORTSTECH_COMPANIES** (score +8, always included):
Kitman Labs, Output Sports, Orreco, STATSports, Stats Perform, TixServe, Playermaker, Wiistream, Clubforce, Xtremepush, TrojanTrack, Sports Impact Technologies, Revelate Fitness, Feenix Group, Anyscor, Fanatics, Flutter Entertainment, FanDuel, Betfair, BoyleSports, Teamworks, Catapult, Hudl, Sportradar, Genius Sports, Glofox, LegitFit, Web Summit, WHOOP, Sport Ireland, IRFU, FAI, GAA, Leinster/Munster/Ulster/Connacht Rugby

**EXCLUDE_COMPANIES** (hard excluded):
Yahoo, UPMC, CarTrawler, An Post, Susquehanna, TRACTIAN, Spanish Point, Accenture, Deloitte, PwC, KPMG, Ernst & Young, Amazon Web, Microsoft, Apple Inc, Meta Platforms, IBM Ireland

### Location Filter (digest.py jobs section)
Keep: Ireland, Dublin, Cork, Galway, Limerick, Belfast, Waterford, Remote, Hybrid
Exclude: Boston, New York, NYC, United States, US, Texas, California, Chicago, "Remote (US)"

---

## Daily Monitor (daily_monitor.py)

**Schedule:** 9am UTC Monday-Friday via GitHub Actions

**Flow:**
1. Fetch all Google News RSS feeds (25hr filter)
2. Score with Claude in batches of 15
3. Deduplicate by story (Claude groups similar titles)
4. Resolve Google News redirect URLs to real article URLs
5. Generate LinkedIn post draft per qualifying article
6. Send email via SendGrid
7. Update daily_monitor_seen.json to prevent re-sending

**Email format:** Subject: `⚡ [Score X/5] Article Title`
**Recipient:** iddodiamant@gmail.com
**Sender:** monitor@sportsd3c0d3d.com (domain authenticated via Blacknight DNS)

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
(same table)

## Score 3 — European / Global Sportstech
(same table)

## 🔇 Low Relevance (scores 1-2)
(same table — kept for editor reference)

## 💼 Jobs Longlist
| Tier | Title | Company | Location | Source | Link |
(Core SporsTech first, then Sports Adjacent, capped at 30)

## 📊 Run Stats
```

---

## Known Issues / Outstanding Items

- **Adzuna API 404** — URL format needs fixing, low priority as LinkedIn gives good coverage
- **businesspost.ie** — returns 403, scraping blocked
- **thinkbusiness.ie RSS** — malformed XML, scrape fallback works
- **"Work at Web Summit"** placeholder job — add to title exclusion list
- **WHOOP Remote roles** — "North American" in title should be excluded even if location says "Remote"
- **Investor LinkedIn profiles** — to be added as Google News queries (Iddo to supply names)
- **More network companies** — Iddo to supply additional Irish sportstech companies to monitor

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

```bash
# Full monthly pipeline
bash run_monthly.sh

# Individual scripts
python enhanced_sportstech_job_scraper_v3.py
python news_pipeline.py
python digest.py

# Daily monitor
python daily_monitor.py

# Test daily monitor (ignore seen file)
# First clear seen file:
echo '{"seen_urls": []}' > daily_monitor_seen.json  # Mac/Linux
'{"seen_urls": []}' | Out-File -FilePath daily_monitor_seen.json -Encoding utf8  # Windows PowerShell
python daily_monitor.py
```

---

## When Making Changes

- Always wrap external calls in try/except — never crash the pipeline because one source fails
- Batch Claude API calls at 15 articles max per call to avoid JSON truncation
- Use model `claude-sonnet-4-20250514` throughout
- Load env vars with python-dotenv at top of each file
- Log failures to file, don't just print
- Test locally before pushing — GitHub Actions runs are hard to debug
