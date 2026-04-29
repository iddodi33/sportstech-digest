"""
digest.py
Reads news_raw_YYYY-MM.json and the most recent sportstech_jobs_ireland.csv,
calls Claude to score and categorise articles, then writes research/YYYY-MM-research.md.
"""

import json
import logging
import os
import glob
import re
from datetime import datetime
from pathlib import Path

import base64

import anthropic
import pandas as pd
from dotenv import load_dotenv
from supabase_client import build_news_item, upsert_news_item
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Attachment, Disposition, FileContent, FileName, FileType, Mail,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"
TOP_N_ARTICLES = 20
TOP_N_JOBS = 30

# Mirror of the scraper's core list — used to assign the "Core SporsTech" tier label.
_CORE_SPORTSTECH = {c.lower() for c in [
    "Kitman Labs", "Output Sports", "Orreco", "STATSports", "Stats Perform",
    "TixServe", "Playermaker", "Wiistream", "Clubforce", "Xtremepush",
    "TrojanTrack", "Sports Impact Technologies", "Revelate Fitness",
    "Fanatics", "Flutter Entertainment", "FanDuel", "Betfair", "BoyleSports",
    "Teamworks", "Catapult", "Hudl", "Sportradar", "Genius Sports",
    "Second Spectrum", "PlayMetrics", "GameOn", "Performa Sports",
    "SportLoMo", "ClubZap", "Danu Sports", "SportsKey", "EquiRatings",
    "Glofox", "LegitFit", "Nutritics", "Web Summit", "WHOOP",
    "Sport Ireland", "IRFU", "FAI", "GAA", "Swim Ireland",
    "Athletics Ireland", "Basketball Ireland", "Leinster Rugby",
    "Munster Rugby", "Ulster Rugby", "Connacht Rugby",
]}


def load_articles(month: str) -> tuple[list[dict], list[dict]]:
    path = f"news_raw_{month}.json"
    if not os.path.exists(path):
        raise FileNotFoundError(f"News file not found: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("articles", []), data.get("failed_sources", [])


def load_jobs() -> pd.DataFrame | None:
    csv_files = sorted(glob.glob("sportstech_jobs_ireland*.csv"), reverse=True)
    if not csv_files:
        log.warning("No jobs CSV found — jobs section will be empty.")
        return None
    log.info("Loading jobs from %s", csv_files[0])
    try:
        df = pd.read_csv(csv_files[0])
        return df
    except Exception as exc:
        log.warning("Failed to load jobs CSV: %s", exc)
        return None


# Clearly irrelevant topic terms — if a title contains one of these AND none of the
# salvage signals, it is dropped. Everything else passes through to Claude.
_NOISE_TOPICS = [
    "cooking", "recipe", "fashion", "weather", "traffic",
    "property prices", "mortgage", "planning permission",
    "criminal", "court case", "politics", "election", "budget",
]

# If any of these appear alongside a noise topic, keep the article anyway.
_SALVAGE_SIGNALS = [
    "sport", "tech", "startup", "ireland", "irish",
    "digital", "data", "fitness", "health", "performance",
    "innovation", "funding", "investment", "launch", "raises",
    "award", "summit", "conference",
]


def keyword_prefilter(articles: list[dict]) -> list[dict]:
    """
    Loose noise filter: only drops articles whose titles contain an obvious
    off-topic term (cooking, fashion, court case, etc.) AND no salvage signal.
    Everything else is passed to Claude for proper scoring.
    """
    kept, dropped = [], 0
    for art in articles:
        title_lower = art.get("title", "").lower()
        has_noise   = any(n in title_lower for n in _NOISE_TOPICS)
        has_salvage = any(s in title_lower for s in _SALVAGE_SIGNALS)
        if has_noise and not has_salvage:
            dropped += 1
        else:
            kept.append(art)
    log.info("Keyword pre-filter: %d kept, %d dropped", len(kept), dropped)
    print(f"  Keyword pre-filter : {len(kept)} kept, {dropped} dropped (clear off-topic titles)")
    return kept


def score_articles_with_claude(articles: list[dict]) -> list[dict]:
    """Score articles in batches of 15 to avoid JSON truncation."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    all_scored = []

    batch_size = 15
    batches = [articles[i:i + batch_size] for i in range(0, len(articles), batch_size)]

    log.info("Scoring %d articles in %d batches of %d…", len(articles), len(batches), batch_size)

    for batch_num, batch in enumerate(batches):
        log.info("Scoring batch %d/%d (%d articles)…", batch_num + 1, len(batches), len(batch))

        articles_text = ""
        for i, a in enumerate(batch):
            articles_text += f"{i}. TITLE: {a.get('title', '')[:120]}\n"
            articles_text += f"   SOURCE: {a.get('source', '')}\n"
            articles_text += f"   DATE: {a.get('pubDate', '')}\n"
            articles_text += f"   SNIPPET: {a.get('snippet', '')[:150]}\n\n"

        prompt = f"""Score these {len(batch)} articles for an Irish sportstech newsletter.

[SCORING CRITERIA]
5 = Irish sportstech company news, funding, award, product launch, international expansion
  Examples: "Cavan Start-up ClubSpot Scales Grassroots Glory into a Global Tech Empire"
            "Torpey Glove Shortlisted for Prestigious Global Sports Tech Award"
            "Feenix Group expands to US base" / "Anyscor secures Enterprise Ireland HPSU funding"
4 = Irish sports org adopting tech, Irish sportstech person featured, Irish adjacent
  Examples: "How Leinster Rugby is using data to boost fan experiences"
            "TrojanTrack grabs One to Watch prize at UCD AI accelerator"
            "Keith Brock Enterprise Ireland sportstech investment"
  Also score 4: Irish legal/regulatory developments with a direct impact on Irish sportstech
  companies (e.g. new DPC guidance on athlete biometric data, AI Act enforcement actions,
  athlete data rights rulings, NGB tech-related regulation, Project Red Card developments).
3 = European sportstech news relevant to Irish audience, Irish sports ecosystem news
  Examples: "Federation of Irish Sport launches 2026 Sport Industry Awards"
            "PEAK Conference - The Global Home of SportsTech & Innovation"
            "How broadcasting and rights deals are reshaping rugby coverage"
  Also score 3 minimum: Irish legal, regulatory or governance commentary on sport
  (e.g. DPC guidance, EU AI Act implications for sport, athlete data rights, Project Red Card,
  Law Society Gazette on sports compliance) directly relevant to Irish sportstech.
2 = Irish sports news without tech angle, tangential sports connection, operations roles
  Examples: "Shamrock Rovers recruiting a Community Manager"
            "This Working Life: assistive technology"
1 = No sports angle, pure politics/property/crime/lifestyle, exact duplicate

Return ONLY a JSON array, no other text, no markdown, no explanation.
Each item must have ALL of these keys:
  "idx": <number>
  "score": <1-5>
  "category": one of: Funding | Product Launch | Company News | Industry Report | Partnership | Event | Other
  "score_reason": <5-8 words explaining the score>
  "summary": <Exactly 2 sentences, 40-60 words total. Sentence 1: what happened, who did it, where (include Irish angle if present). Sentence 2: why it matters, what it enables, or what context helps the reader understand significance. Never just restate the headline. Factual, Irish-ecosystem-builder voice, no hype, never starts with "Exciting news" or "Delighted". Prepend ⭐ if score is 5.
  BAD (too short, just restates headline): "Output Sports launches HYROX365 Athlete Readiness Test."
  GOOD (gives context and why-it-matters): "Dublin-based Output Sports has partnered with HYROX365 to launch a standardised Athlete Readiness Test using its sensor platform to measure strength, endurance, and recovery benchmarks. The partnership extends Output's reach into mass-participation fitness testing across the global HYROX network.">
  "tags": <list of 3-5 keyword strings: company names, themes, event types>
  "verticals": <list of 1-2 from: Performance Analytics | Wearables & Hardware | Fan Engagement | Media & Broadcasting | Health, Fitness and Wellbeing | Scouting & Recruitment | Esports & Gaming | Betting & Fantasy | Stadium & Event Tech | Club Management Software | Sports Education & Coaching | Other / Emerging>
  "mentioned_companies": <list of company names actually mentioned in the article>

ARTICLES:
{articles_text}
JSON array:"""

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            try:
                batch_scored = json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r'\[.*\]', raw, re.DOTALL)
                if match:
                    try:
                        batch_scored = json.loads(match.group())
                    except json.JSONDecodeError:
                        log.error("Batch %d JSON parse failed after regex — saving debug file.", batch_num + 1)
                        with open(f"claude_debug_batch_{batch_num + 1}.txt", "w") as f:
                            f.write(raw)
                        continue
                else:
                    log.error("Batch %d: no JSON array found in response.", batch_num + 1)
                    continue

            for scored_item in batch_scored:
                idx = scored_item.get("idx", -1)
                if 0 <= idx < len(batch):
                    article = batch[idx].copy()
                    article["score"]               = scored_item.get("score",    1)
                    article["category"]            = scored_item.get("category", "Other")
                    article["reason"]              = scored_item.get("score_reason", scored_item.get("reason", ""))
                    article["summary"]             = scored_item.get("summary",  "")
                    article["tags"]                = scored_item.get("tags",     [])
                    article["verticals"]           = scored_item.get("verticals", [])
                    article["mentioned_companies"] = scored_item.get("mentioned_companies", [])
                    all_scored.append(article)

        except Exception as exc:
            log.error("Batch %d API call failed: %s", batch_num + 1, exc)
            continue

    log.info("Total scored: %d articles across %d batches", len(all_scored), len(batches))
    return all_scored


def format_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


def build_markdown(
    scored: list[dict],
    jobs_df: pd.DataFrame | None,
    failed_sources: list[dict],
    total_raw: int,
    month: str,
) -> str:
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_jobs = len(jobs_df) if jobs_df is not None else 0

    def _article_row(art: dict) -> str:
        title    = art.get("title",    "").replace("|", "\\|")
        source   = art.get("source",   "").replace("|", "\\|")
        summary  = art.get("summary",  "").replace("|", "\\|")
        reason   = art.get("reason",   "").replace("|", "\\|")
        date     = format_date(art.get("pubDate", ""))
        link     = art.get("link",     "")
        score    = art.get("score",    "")
        category = art.get("category", "")
        return f"| {score} | {reason} | {category} | {title} | {source} | {date} | [link]({link}) | {summary} |"

    TABLE_HEADER = [
        "| Score | Reason | Category | Title | Source | Date | Link | Summary |",
        "|------:|--------|----------|-------|--------|------|------|---------|",
    ]

    # Coerce scores to int (Claude may return strings like "3")
    def _sc(a):
        try:
            return int(a.get("score", 0))
        except (TypeError, ValueError):
            return 0

    # Partition by score tier
    score5   = [a for a in scored if _sc(a) == 5]
    score4   = [a for a in scored if _sc(a) == 4]
    score3   = [a for a in scored if _sc(a) == 3]
    low      = [a for a in scored if _sc(a) in (1, 2)]

    lines = [
        f"# Sports D3c0d3d — Research Digest {month}",
        "",
        "---",
        "",
    ]

    # --- Score 5 section ---
    lines += [
        "## ⭐ Score 5 — Irish Sportstech Direct",
        "",
        *TABLE_HEADER,
    ]
    if score5:
        lines += [_article_row(a) for a in score5]
    else:
        lines.append("_No score-5 articles this month._")

    # --- Score 4 section ---
    lines += [
        "",
        "## Score 4 — Irish Sports + Tech Adjacent",
        "",
        *TABLE_HEADER,
    ]
    if score4:
        lines += [_article_row(a) for a in score4]
    else:
        lines.append("_No score-4 articles this month._")

    # --- Score 3 section ---
    lines += [
        "",
        "## Score 3 — European / Global Sportstech (relevant to Irish audience)",
        "",
        *TABLE_HEADER,
    ]
    if score3:
        lines += [_article_row(a) for a in score3]
    else:
        lines.append("_No score-3 articles this month._")

    # --- Low relevance section ---
    lines += [
        "",
        "---",
        "",
        "## 🔇 Low Relevance (scores 1–2) — excluded from main digest",
        "",
        *TABLE_HEADER,
    ]
    if low:
        lines += [_article_row(a) for a in low]
    else:
        lines.append("_No low-relevance articles._")

    lines += ["", "---", "", "## 💼 Jobs Longlist", ""]

    if jobs_df is not None and not jobs_df.empty:
        # --- location filter: exclude non-Irish / non-remote jobs ---
        loc_col = next(
            (c for c in jobs_df.columns if c.lower() == "location"), None
        )
        if loc_col:
            _EXCLUDE_LOCS = [
                "boston", "new york", "nyc", "united states",
                " us,", "(us)", "texas", "california", "chicago", "remote (us)",
            ]
            _INCLUDE_LOCS = [
                "ireland", "dublin", "cork", "galway", "limerick",
                "belfast", "waterford", "remote", "hybrid",
            ]

            def _loc_ok(loc_val) -> bool:
                if not loc_val or str(loc_val).lower() in ("nan", "none", ""):
                    return True  # blank → keep
                loc_lower = str(loc_val).lower()
                if any(ex in loc_lower for ex in _EXCLUDE_LOCS):
                    return False
                return any(inc in loc_lower for inc in _INCLUDE_LOCS)

            before = len(jobs_df)
            jobs_df = jobs_df[jobs_df[loc_col].apply(_loc_ok)]
            excluded = before - len(jobs_df)
            print(f"  Jobs location filter: {excluded} excluded, {len(jobs_df)} remaining")
        # --- end location filter ---

        # Map columns flexibly
        col = lambda candidates: next(
            (c for c in candidates if c in jobs_df.columns), None
        )
        title_col    = col(["title",   "Title",   "job_title",  "Job Title"])
        company_col  = col(["company", "Company", "employer",   "Employer"])
        location_col = col(["location","Location"])
        source_col   = col(["source",  "Source"])
        link_col     = col(["link",    "Link",    "url",        "URL"])
        relevancy_col = col(["relevancy", "Relevancy"])

        def _is_core(row) -> bool:
            if not company_col:
                return False
            return any(c in str(row[company_col]).lower() for c in _CORE_SPORTSTECH)

        # Keep only high-relevancy OR core-company jobs
        def _passes_quality(row) -> bool:
            if _is_core(row):
                return True
            if relevancy_col and str(row.get(relevancy_col, "")).lower() == "high":
                return True
            return False

        before_quality = len(jobs_df)
        jobs_df = jobs_df[jobs_df.apply(_passes_quality, axis=1)]
        quality_excluded = before_quality - len(jobs_df)
        print(f"  Jobs quality filter: {quality_excluded} excluded (non-core, non-high), {len(jobs_df)} remaining")

        # Sort: core first, then relevancy descending
        _RELEVANCY_ORDER = {"high": 0, "medium": 1, "low": 2}
        jobs_df = jobs_df.copy()
        jobs_df["_is_core"] = jobs_df.apply(_is_core, axis=1)
        if relevancy_col:
            jobs_df["_rel_order"] = jobs_df[relevancy_col].map(_RELEVANCY_ORDER).fillna(3)
        else:
            jobs_df["_rel_order"] = 3
        jobs_df = jobs_df.sort_values(["_is_core", "_rel_order"], ascending=[False, True])

        top_jobs = jobs_df.head(TOP_N_JOBS)

        lines.append("| Tier | Title | Company | Location | Source | Link |")
        lines.append("|------|-------|---------|----------|--------|------|")

        for _, row in top_jobs.iterrows():
            tier  = "Core SporsTech" if _is_core(row) else "Sports Adjacent"
            t     = str(row.get(title_col,    "") if title_col    else "").replace("|", "\\|")
            c     = str(row.get(company_col,  "") if company_col  else "").replace("|", "\\|")
            loc   = str(row.get(location_col, "") if location_col else "").replace("|", "\\|")
            src   = str(row.get(source_col,   "") if source_col   else "").replace("|", "\\|")
            lnk   = str(row.get(link_col,     "") if link_col     else "")
            link_cell = f"[link]({lnk})" if lnk and lnk != "nan" else ""
            lines.append(f"| {tier} | {t} | {c} | {loc} | {src} | {link_cell} |")
    else:
        lines.append("_No jobs data available._")

    failed_list = (
        ", ".join(f["source"] for f in failed_sources) if failed_sources else "None"
    )

    # Source diversity breakdown
    source_counts: dict[str, int] = {}
    for art in scored:
        src = art.get("source", "Unknown")
        source_counts[src] = source_counts.get(src, 0) + 1
    source_rows = sorted(source_counts.items(), key=lambda x: -x[1])

    lines += [
        "",
        "---",
        "",
        "## 📊 Run Stats",
        "",
        "| Stat | Value |",
        "|------|-------|",
        f"| Date run | {run_date} |",
        f"| Total articles fetched | {total_raw} |",
        f"| Articles scored total | {len(scored)} |",
        f"| Score 5 (Irish direct) | {sum(1 for a in scored if _sc(a)==5)} |",
        f"| Score 4 (Irish adjacent) | {sum(1 for a in scored if _sc(a)==4)} |",
        f"| Score 3 (European relevant) | {sum(1 for a in scored if _sc(a)==3)} |",
        f"| Scores 1–2 (low relevance) | {sum(1 for a in scored if _sc(a) in (1,2))} |",
        f"| Total jobs fetched | {total_jobs} |",
        f"| Failed sources | {failed_list} |",
        "",
        "### Source mix (scored articles)",
        "",
        "| Source | Count |",
        "|--------|------:|",
    ]
    for src, cnt in source_rows:
        lines.append(f"| {src.replace('|', chr(92)+'|')} | {cnt} |")
    lines.append("")

    return "\n".join(lines)


def email_research_digest(output_path: str, month: str) -> None:
    sg_key     = os.getenv("SENDGRID_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to   = os.getenv("ALERT_TO")

    if not sg_key or not alert_from or not alert_to:
        log.warning("SendGrid env vars not set — skipping research email.")
        return

    with open(output_path, encoding="utf-8") as f:
        md_content = f.read()

    encoded = base64.b64encode(md_content.encode("utf-8")).decode()
    attachment = Attachment(
        FileContent(encoded),
        FileName(f"{month}-research.md"),
        FileType("text/markdown"),
        Disposition("attachment"),
    )

    html_body = (
        "<p>Here is this month's research digest. "
        "Review and pick the top stories for the newsletter.</p>"
        f"<p>Attached: <strong>{month}-research.md</strong></p>"
    )

    message = Mail(
        from_email=alert_from,
        to_emails=alert_to,
        subject=f"Sports D3c0d3d Monthly Research \u2014 {month}",
        html_content=html_body,
    )
    message.attachment = attachment

    try:
        sg = SendGridAPIClient(sg_key)
        response = sg.send(message)
        log.info("Research email sent: status=%s", response.status_code)
        if response.status_code >= 400:
            log.error("SendGrid returned error status %s: %s", response.status_code, response.body)
    except Exception as exc:
        log.error("Failed to send research email: %s", exc)


def run():
    month = datetime.now().strftime("%Y-%m")
    log.info("Loading articles for %s…", month)

    try:
        articles, failed_sources = load_articles(month)
    except FileNotFoundError as exc:
        log.error("%s — run news_pipeline.py first.", exc)
        raise

    log.info("Loaded %d articles. Loading jobs…", len(articles))
    total_raw = len(articles)   # capture before prefilter
    jobs_df = load_jobs()

    articles = keyword_prefilter(articles)
    log.info("Scoring %d articles with Claude…", len(articles))
    try:
        scored = score_articles_with_claude(articles)
    except Exception as exc:
        log.error("Claude scoring failed: %s", exc)
        raise

    Path("research").mkdir(exist_ok=True)
    output_path = f"research/{month}-research.md"

    md = build_markdown(scored, jobs_df, failed_sources, total_raw, month)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md)

    # Upsert score 3+ articles to Supabase hub (additive; does not affect email)
    hub_articles = [a for a in scored if int(a.get("score", 0)) >= 3]
    hub_upsert_count = 0
    for article in hub_articles:
        item = build_news_item(article)
        if upsert_news_item(item) is not None:
            hub_upsert_count += 1
        else:
            log.warning("Supabase upsert failed for: %s", article.get("title", "")[:80])
    log.info("Supabase: upserted %d/%d score-3+ items to hub", hub_upsert_count, len(hub_articles))

    print(f"\n=== Digest Summary ===")
    print(f"  Articles scored   : {len(scored)}")
    print(f"  Jobs included     : {len(jobs_df) if jobs_df is not None else 0}")
    print(f"  Output            : {output_path}")
    print(f"  Supabase upserted : {hub_upsert_count}/{len(hub_articles)} (score 3+)")

    email_research_digest(output_path, month)


if __name__ == "__main__":
    run()
