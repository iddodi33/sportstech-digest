"""newsletter_export.py — pull news/jobs/events from hub Supabase and write a monthly source markdown."""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCORE_LABELS = {
    5: "Irish SportsTech company news",
    4: "Irish sports adopting tech / Irish-adjacent",
    3: "European SportsTech relevant to Irish audience",
}

SENIORITY_ORDER = ["Executive", "Lead", "Senior", "Mid-level", "Junior", "Other"]


def _get_client():
    url = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
        sys.exit(1)
    from supabase import create_client
    return create_client(url, key)


def _fetch_news(client, now: datetime) -> list[dict]:
    cutoff = (now - timedelta(days=30)).isoformat()
    result = (
        client.table("news_items")
        .select(
            "title,url,source,summary,score,score_reason,"
            "tags,verticals,mentioned_companies,published_at,image_url"
        )
        .gte("score", 3)
        .gte("scraped_at", cutoff)
        .order("score", desc=True)
        .order("published_at", desc=True)
        .execute()
    )
    return result.data or []


def _fetch_jobs(client, now: datetime) -> list[dict]:
    cutoff = (now - timedelta(days=8)).isoformat()
    result = (
        client.table("jobs")
        .select(
            "title,company_name,location_normalised,url,seniority,"
            "job_function,vertical,summary,last_seen_in_scrape_run,"
            "salary_range,employment_type,remote_status"
        )
        .eq("status", "approved")
        .gte("last_seen_in_scrape_run", cutoff)
        .execute()
    )
    return result.data or []


def _fetch_events(client, now: datetime) -> list[dict]:
    today = now.date().isoformat()
    ninety_days_out = (now.date() + timedelta(days=90)).isoformat()
    result = (
        client.table("events")
        .select(
            "name,date,end_date,start_time,location,area,"
            "format,organiser,description,url,recurrence"
        )
        .eq("status", "verified")
        .gte("date", today)
        .lte("date", ninety_days_out)
        .order("date")
        .execute()
    )
    return result.data or []


def _join(items, sep=", ") -> str:
    if not items:
        return ""
    return sep.join(str(i) for i in items if i)


def _format_event_date(date_str: str, end_date_str: str | None) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str or ""
    if end_date_str and end_date_str != date_str:
        try:
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
            return f"{dt.day}-{end_dt.day} {dt.strftime('%b')}"
        except (ValueError, TypeError):
            pass
    return dt.strftime("%a %d %b")


def _build_news_section(news: list[dict]) -> str:
    lines = ["## News (last 30 days, score 3+)", ""]

    by_score: dict[int, list] = defaultdict(list)
    for article in news:
        by_score[int(article.get("score") or 0)].append(article)

    for score in [5, 4, 3]:
        label = SCORE_LABELS.get(score, f"Score {score}")
        articles = by_score.get(score, [])
        lines.append(f"### Score {score}: {label} ({len(articles)})")
        lines.append("")

        if not articles:
            lines.append(f"_No score {score} articles in the last 30 days._")
            lines.append("")
            continue

        for article in articles:
            title = article.get("title") or "Untitled"
            url = article.get("url") or ""
            source = article.get("source") or ""
            published_at = article.get("published_at") or ""
            verticals = article.get("verticals") or []
            summary = article.get("summary") or ""
            companies = article.get("mentioned_companies") or []
            tags = article.get("tags") or []

            pub_date = published_at[:10] if published_at else ""
            verticals_str = _join(verticals)
            meta_parts = [p for p in [source, pub_date, verticals_str] if p]

            lines.append(f"**{title}**")
            lines.append(" · ".join(meta_parts))
            lines.append("")
            if summary:
                lines.append(summary)
                lines.append("")
            if companies:
                lines.append(f"_Companies mentioned: {_join(companies)}_")
                lines.append("")
            if tags:
                lines.append(f"_Tags: {_join(tags)}_")
                lines.append("")
            if url:
                lines.append(f"[Read more]({url})")
            lines.append("")

    return "\n".join(lines)


def _build_jobs_section(jobs: list[dict]) -> str:
    lines = ["## Open jobs (approved, seen in last 8 days)", ""]

    if not jobs:
        lines.append("_No approved jobs seen in the last 8 days._")
        lines.append("")
        return "\n".join(lines)

    by_seniority: dict[str, list] = defaultdict(list)
    for job in jobs:
        seniority = job.get("seniority") or "Other"
        by_seniority[seniority].append(job)

    present = [s for s in SENIORITY_ORDER if s in by_seniority]
    remaining = sorted(set(by_seniority.keys()) - set(SENIORITY_ORDER))
    all_levels = present + remaining

    for seniority in all_levels:
        level_jobs = by_seniority[seniority]
        lines.append(f"### {seniority} ({len(level_jobs)})")
        lines.append("")

        by_company: dict[str, list] = defaultdict(list)
        for job in level_jobs:
            company = job.get("company_name") or "Unknown"
            by_company[company].append(job)

        for company in sorted(by_company.keys()):
            company_jobs = by_company[company]
            lines.append(f"**{company}** ({len(company_jobs)})")
            for job in company_jobs:
                title = job.get("title") or "Untitled"
                url = job.get("url") or ""
                location = job.get("location_normalised") or ""
                remote = job.get("remote_status") or ""
                fn = job.get("job_function") or ""
                detail_parts = [p for p in [location, remote, fn] if p]
                detail_str = " · ".join(detail_parts)
                job_line = f"- [{title}]({url})" if url else f"- {title}"
                if detail_str:
                    job_line += f" — {detail_str}"
                lines.append(job_line)
            lines.append("")

    return "\n".join(lines)


def _build_events_section(events: list[dict]) -> str:
    lines = ["## Upcoming events (next 90 days, verified)", ""]

    if not events:
        lines.append("_No verified events in the next 90 days._")
        lines.append("")
        return "\n".join(lines)

    by_month: dict[tuple, list] = defaultdict(list)
    for event in events:
        date_str = event.get("date") or ""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            month_key = dt.strftime("%B %Y")
            month_sort = (dt.year, dt.month)
        except (ValueError, TypeError):
            month_key = "Unknown date"
            month_sort = (9999, 99)
        by_month[(month_sort, month_key)].append(event)

    for (month_sort, month_key), month_events in sorted(by_month.items()):
        month_events_sorted = sorted(month_events, key=lambda e: e.get("date") or "")
        lines.append(f"### {month_key} ({len(month_events_sorted)})")
        lines.append("")

        for event in month_events_sorted:
            name = event.get("name") or "Unnamed event"
            recurrence = event.get("recurrence") or ""
            date_str = event.get("date") or ""
            end_date_str = event.get("end_date") or ""
            location = event.get("location") or ""
            organiser = event.get("organiser") or ""
            fmt = event.get("format") or ""
            description = event.get("description") or ""
            url = event.get("url") or ""

            heading = f"**{name}**"
            if recurrence:
                heading += f" _{recurrence}_"
            lines.append(heading)

            date_display = _format_event_date(date_str, end_date_str)
            meta_parts = [p for p in [date_display, location, organiser, fmt] if p]
            lines.append(" · ".join(meta_parts))

            if description:
                truncated = description[:300]
                if len(description) > 300:
                    truncated += "…"
                lines.append(truncated)

            if url:
                lines.append(f"[Link]({url})")
            lines.append("")

    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    month_long = now.strftime("%B %Y")
    generated_str = now.strftime("%Y-%m-%d %H:%M UTC")

    client = _get_client()

    log.info("Fetching news...")
    news = _fetch_news(client, now)
    log.info("Fetched %d news articles", len(news))

    log.info("Fetching jobs...")
    jobs = _fetch_jobs(client, now)
    log.info("Fetched %d jobs", len(jobs))

    log.info("Fetching events...")
    events = _fetch_events(client, now)
    log.info("Fetched %d events", len(events))

    news_section = _build_news_section(news)
    jobs_section = _build_jobs_section(jobs)
    events_section = _build_events_section(events)

    header = (
        f"# Newsletter source — {month_long}\n"
        f"_{generated_str}_\n"
        f"News: {len(news)} · Jobs: {len(jobs)} · Events: {len(events)}"
    )

    md = "\n".join([
        header,
        "",
        "---",
        "",
        news_section,
        "",
        "---",
        "",
        jobs_section,
        "",
        "---",
        "",
        events_section,
    ])

    out_dir = Path("newsletter")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{month_str}-newsletter-source.md"
    out_path.write_text(md, encoding="utf-8")
    log.info("Written to %s", out_path)


if __name__ == "__main__":
    main()
