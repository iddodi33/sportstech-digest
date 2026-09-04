"""verify_discovery_coverage.py — discovery-only coverage check. No Anthropic calls.

Runs daily_monitor's fetch step with the date filter widened (the audit gap list
spans 2026-06 to 2026-09, far outside the production 72h window) and reports which
of the 15 unique URLs in scripts/data/alerts_missing_from_hub.csv are now reachable.

Also reports total articles fetched per run, split Google News vs regional site
RSS, so CAP_REGIONAL can be tuned against a real figure rather than a guess.

Makes zero billed API calls — it stops at discovery and never scores.

Usage:
    python scripts/verify_discovery_coverage.py                # after-change run
    python scripts/verify_discovery_coverage.py --google-only  # baseline
    python scripts/verify_discovery_coverage.py --hours 2400
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import daily_monitor  # noqa: E402
from news_pipeline import REGIONAL_RSS_FEEDS, _cap_for  # noqa: E402

MISSING_CSV = Path(__file__).resolve().parent / "data" / "alerts_missing_from_hub.csv"

# Defaults to production's 72h. Widening this is expensive and mostly pointless:
# daily_monitor decodes a Google News redirect with one HTTP request per
# IN-WINDOW entry, so a 100-day window turns ~70 feeds into thousands of requests
# and takes many minutes. It also buys nothing for the gap list — RSS feeds carry
# 10-30 recent items, not an archive, so June-August stories are gone from every
# feed regardless of the date filter. Domain-level coverage below is the
# meaningful measure of this change.
DEFAULT_HOURS = 72


def normalize_url(url: str) -> str:
    """Same normalisation the audit used: host without www, no query/fragment,
    no trailing slash."""
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return (url or "").strip().lower()
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"{host}{(p.path or '').rstrip('/')}" if host else (url or "").strip().lower()


def load_gap_list() -> list[dict]:
    if not MISSING_CSV.exists():
        raise SystemExit(f"{MISSING_CSV} not found — run the audit first.")
    with open(MISSING_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    unique: dict[str, dict] = {}
    for r in rows:
        unique.setdefault(normalize_url(r["url"]), r)
    return list(unique.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                    help="lookback window for this check (production is 72)")
    ap.add_argument("--google-only", action="store_true",
                    help="baseline: skip the regional site-RSS feeds")
    args = ap.parse_args()

    if args.google_only:
        # Baseline — reproduce pre-change discovery by emptying the regional list.
        daily_monitor.REGIONAL_RSS_FEEDS = []

    label = "BASELINE (Google News only)" if args.google_only else "AFTER (Google News + regional RSS)"
    print(f"=== {label} — lookback {args.hours}h ===\n")

    articles, total_fetched = daily_monitor.fetch_recent_articles(args.hours)

    regional_domains = {urlparse(u).hostname.removeprefix("www.") for u in REGIONAL_RSS_FEEDS}
    from_regional = [
        a for a in articles
        if (urlparse(a["link"]).hostname or "").removeprefix("www.") in regional_domains
    ]

    print(f"\nTotal entries seen (pre-window filter) : {total_fetched}")
    print(f"Articles within {args.hours}h window        : {len(articles)}")
    print(f"  from regional site RSS               : {len(from_regional)}")
    print(f"  from Google News                     : {len(articles) - len(from_regional)}")

    if not args.google_only:
        print("\nPer-regional-feed yield (for tuning CAP_REGIONAL):")
        for url in REGIONAL_RSS_FEEDS:
            host = (urlparse(url).hostname or "").removeprefix("www.")
            n = sum(
                1 for a in articles
                if (urlparse(a["link"]).hostname or "").removeprefix("www.") == host
            )
            cap = _cap_for(url, "site_rss")
            flag = "  <- AT CAP" if n >= cap else ""
            print(f"  {n:3d}/{cap:<3d} {host}{flag}")

    # --- gap-list coverage ---
    found_urls = {normalize_url(a["link"]) for a in articles}
    gap = load_gap_list()
    hits, misses = [], []
    for row in gap:
        (hits if normalize_url(row["url"]) in found_urls else misses).append(row)

    print(f"\n=== GAP LIST COVERAGE — {len(hits)}/{len(gap)} reachable ===")
    print("\nREACHABLE NOW:")
    for r in hits:
        host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
        print(f"  [{r['score']}] {host:22s} {r['headline'][:62]}")
    print("\nSTILL NOT REACHABLE:")
    for r in misses:
        host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
        print(f"  [{r['score']}] {host:22s} {r['headline'][:62]}")

    # --- domain-level coverage -------------------------------------------
    # RSS feeds are a window on recent content, not an archive: these feeds carry
    # 10-30 items, so a June or July story has long scrolled off regardless of how
    # wide the date filter is set. URL-level recovery therefore understates the
    # change. The forward-looking question is whether the story's DOMAIN is now
    # reached at all — that is what determines whether the next such story lands.
    covered_domains = {
        (urlparse(a["link"]).hostname or "").removeprefix("www.") for a in articles
    }
    dom_hits = [
        r for r in gap
        if (urlparse(r["url"]).hostname or "").removeprefix("www.") in covered_domains
    ]
    print(f"\n=== DOMAIN-LEVEL COVERAGE — {len(dom_hits)}/{len(gap)} ===")
    print("(story's publisher is now reached by at least one live feed)")
    for r in gap:
        host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
        mark = "YES" if host in covered_domains else " no"
        print(f"  {mark}  [{r['score']}] {host:22s} {r['headline'][:56]}")

    print(f"\nSummary: {len(hits)}/{len(gap)} gap URLs still live in-feed; "
          f"{len(dom_hits)}/{len(gap)} gap domains now covered.")


if __name__ == "__main__":
    main()
