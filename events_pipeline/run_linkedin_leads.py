"""run_linkedin_leads.py — entry point for the LinkedIn event-lead adapter.

Runs as its own step in events_weekly.yml, alongside (not inside) the 5-source
events orchestrator. Kept separate on purpose: run_weekly_events.py's registry
feeds every discovered URL to the Claude extractor and into public.events, and
this adapter must do neither. A failure here also must not cost the main events
run its Friday, which a shared process would risk.

Flags:
  --dry-run          run the queries, print what would land, write nothing to Supabase
  --keywords-only    skip the seed author pages
  --authors-only     skip the keyword searches
  --limit N          cap at N queries (cheap smoke test — N=1 is one actor call)
  --seed-authors P   override the seed author CSV path
  --skip-classify    discover and write leads without running the Haiku classifier

Exit codes:
  0  ran (possibly with some individual queries failed — see the summary)
  1  aborted before doing any work (missing APIFY_TOKEN, no Supabase)

NOTE: there is no aborting cost ceiling here, by Iddo's explicit decision — see
the "NO COST CEILING" section of adapters/linkedin_keywords.py. Both halves of
the spend warn instead: APIFY_WARN_ABOVE_USD below, and
lead_classifier.WARN_ABOVE_USD for the Haiku step. Crossing either logs and
continues. Real billed spend goes to scripts/data/apify_spend.jsonl and
scripts/data/anthropic_spend.jsonl so the decision can be revisited from data.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

_PIPELINE = "events_linkedin_leads"

# What counts as "high confidence" in the run summary. A reporting threshold
# only — nothing is filtered or dropped on it; the column holds the raw 0-100.
HIGH_CONFIDENCE_THRESHOLD = 70

# Warn-only spend threshold for the Apify half of the run. There is still no
# aborting ceiling — Iddo's standing call — but the first real run cost $0.9929
# with an untuned query set, so roughly 3x that is a generous line past which
# something has changed and nobody would otherwise notice. Crossing it logs a
# warning and the run continues.
APIFY_WARN_ABOVE_USD = 3.00


def _fmt_runtime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _print_dry_run(leads: list[dict]) -> None:
    print()
    print("=" * 78)
    print(f"DRY RUN — {len(leads)} unique leads that WOULD be written to event_leads")
    print("=" * 78)
    for i, lead in enumerate(sorted(leads, key=lambda x: x.get("posted_at") or "", reverse=True), 1):
        content = (lead.get("content") or "").replace("\n", " ")
        print(f"\n[{i}] {lead.get('author_name') or '(unknown author)'}"
              f"  ({lead.get('author_type') or '?'})"
              f"  {(lead.get('posted_at') or '')[:10]}")
        print(f"    {lead.get('post_url')}")
        print(f"    matched: {', '.join(lead.get('matched_all') or [])}")
        print(f"    {content[:220]}{'...' if len(content) > 220 else ''}")
    print()
    print("=" * 78)


def _classify_pending(run_ts: str, limit: int | None = None) -> dict:
    """Classify every unclassified pending lead with Haiku. Never raises.

    Returns a stats dict for the run summary. Failures are per-lead: a lead that
    errors is simply left unclassified and picked up by the next run, because
    `classified_at IS NULL` is the work queue.
    """
    import anthropic

    from claude_budget import RunCost
    from events_pipeline import lead_classifier
    from events_pipeline.supabase_events_client import (
        fetch_unclassified_leads,
        update_lead_classification,
    )
    import run_telemetry

    stats = {
        "candidates": 0, "classified": 0, "failed": 0,
        "relevant": 0, "high_confidence": 0,
        "by_kind": {}, "input_tokens": 0, "output_tokens": 0,
        "requests": 0, "cost_usd": 0.0,
    }

    leads = fetch_unclassified_leads(limit=limit)
    stats["candidates"] = len(leads)
    if not leads:
        log.info("Classification: nothing unclassified — skipping")
        return stats

    log.info("=== Classifying %d leads with %s ===", len(leads), lead_classifier.MODEL)

    ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    # RunCost is used here purely as a token accumulator and for
    # call_claude_with_retry's retry behaviour. Its ceiling is NOT enforced —
    # this pipeline warns rather than aborts (see lead_classifier's docstring) —
    # and its .cost property must not be read, because that prices tokens at the
    # news pipelines' Sonnet rates and this step runs on Haiku.
    run_cost = RunCost(ceiling_usd=float("inf"), label="linkedin_lead_classify")

    for lead in leads:
        try:
            raw, _ = lead_classifier.classify_with_haiku(lead, ai, run_cost)
            result = lead_classifier.normalise(raw)
        except Exception as exc:
            log.warning("Classification failed for lead %s: %s", lead.get("id"), exc)
            stats["failed"] += 1
            continue

        if not update_lead_classification(lead["id"], result):
            stats["failed"] += 1
            continue

        stats["classified"] += 1
        kind = result["post_kind"]
        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
        if result["is_event_relevant"]:
            stats["relevant"] += 1
            if result["relevance_confidence"] >= HIGH_CONFIDENCE_THRESHOLD:
                stats["high_confidence"] += 1

    stats["input_tokens"] = run_cost.input_tokens
    stats["output_tokens"] = run_cost.output_tokens
    stats["requests"] = run_cost.requests
    stats["cost_usd"] = round(
        run_telemetry.haiku_cost_usd(run_cost.input_tokens, run_cost.output_tokens), 6,
    )

    run_telemetry.record_anthropic_run(
        _PIPELINE,
        lead_classifier.MODEL,
        step="classify_leads",
        items=stats["classified"],
        requests=run_cost.requests,
        input_tokens=run_cost.input_tokens,
        output_tokens=run_cost.output_tokens,
        run_ts=run_ts,
        extra={
            "failed": stats["failed"],
            "relevant": stats["relevant"],
            "high_confidence": stats["high_confidence"],
            "by_kind": stats["by_kind"],
        },
    )

    if stats["cost_usd"] > lead_classifier.WARN_ABOVE_USD:
        log.warning(
            "Classification spend $%.4f exceeded the $%.2f warn threshold "
            "(%d leads). Not an abort — check scripts/data/anthropic_spend.jsonl "
            "for the trend before changing anything.",
            stats["cost_usd"], lead_classifier.WARN_ABOVE_USD, stats["classified"],
        )

    return stats


def main(
    dry_run: bool = False,
    keywords_only: bool = False,
    authors_only: bool = False,
    limit: int | None = None,
    seed_authors_path: str | None = None,
    skip_classify: bool = False,
) -> int:
    wall_t0 = time.time()
    run_started_at = datetime.now(timezone.utc)

    log.info("=== LinkedIn event-lead discovery starting ===")
    log.info(
        "flags: dry_run=%s keywords_only=%s authors_only=%s limit=%s",
        dry_run, keywords_only, authors_only, limit,
    )

    if keywords_only and authors_only:
        log.error("--keywords-only and --authors-only are mutually exclusive")
        return 1

    if not os.getenv("APIFY_TOKEN"):
        log.error("APIFY_TOKEN is not set — cannot run LinkedIn lead discovery.")
        return 1

    if not dry_run and not skip_classify and not os.getenv("ANTHROPIC_API_KEY"):
        log.error(
            "ANTHROPIC_API_KEY is not set — cannot classify leads. "
            "Re-run with --skip-classify to discover without classifying."
        )
        return 1

    from events_pipeline.adapters.linkedin_keywords import (
        ACTOR,
        ApifyTokenMissingError,
        LinkedInKeywordsAdapter,
    )
    from events_pipeline.supabase_events_client import get_supabase_client, upsert_event_lead
    import run_telemetry

    client = None
    if not dry_run:
        client = get_supabase_client()
        if client is None:
            log.error(
                "Aborting — could not connect to Supabase. Check "
                "NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY. "
                "(Use --dry-run to run the queries without writing.)"
            )
            return 1

    adapter = LinkedInKeywordsAdapter(
        seed_authors_path=Path(seed_authors_path) if seed_authors_path else None,
    )

    try:
        result = adapter.run(
            keywords_only=keywords_only,
            authors_only=authors_only,
            limit=limit,
        )
    except ApifyTokenMissingError as exc:
        log.error("%s — aborting", exc)
        return 1

    leads = result["leads"]
    audit = result["audit"]

    # ── Telemetry: one record per actor call, real billed $ from Apify ────────
    # Written before the upserts so a Supabase failure cannot lose the record of
    # money already spent.
    run_ts = run_started_at.isoformat()
    for q in audit["per_query"]:
        run_telemetry.record_apify_run(
            _PIPELINE,
            ACTOR,
            run_id=q["run_id"],
            query_kind=q["matched_by"],
            query_value=q["matched_value"],
            posts_fetched=q["posts_fetched"],
            usage_total_usd=q["usage_total_usd"],
            status=q["status"],
            run_ts=run_ts,
            extra={
                "dry_run": dry_run,
                "error": q["error"],
                # Apify's per-event breakdown (posts, actor starts, 0-result
                # queries). Kept alongside the dollar total so a later reader can
                # see WHY a query cost what it did without re-deriving it.
                "charged_events": q["charged_events"],
                # Apify's own `usageTotalUsd` at the moment we read it. Lags the
                # event counts, so it is recorded for comparison, not used as
                # the figure — see charged_cost_usd() in the adapter.
                "usage_reported_usd": q["usage_reported_usd"],
            },
        )

    # ── Write ─────────────────────────────────────────────────────────────────
    inserted = updated = failed = 0
    if dry_run:
        _print_dry_run(leads)
    else:
        for lead in leads:
            lead_id, was_inserted = upsert_event_lead(lead)
            if lead_id is None:
                failed += 1
            elif was_inserted:
                inserted += 1
            else:
                updated += 1

    # ── Classify ──────────────────────────────────────────────────────────────
    # Runs after the write, against the table rather than against `leads`, so it
    # also picks up anything an earlier run left unclassified (a crash, an API
    # outage, or --skip-classify). Skipped entirely on a dry run: there is
    # nothing in the table to classify and no lead ids to write back to.
    classify_stats: dict = {}
    if not dry_run and not skip_classify:
        classify_stats = _classify_pending(run_ts)
    elif skip_classify:
        log.info("Skipping classification (--skip-classify)")

    # ── Summary ───────────────────────────────────────────────────────────────
    runtime = time.time() - wall_t0
    log.info("=== LinkedIn event-lead discovery complete (%s) ===", _fmt_runtime(runtime))
    log.info("  queries run          : %d (%d keyword, %d seed author)",
             audit["queries_run"], audit["keyword_queries"], audit["seed_author_queries"])
    log.info("  queries failed       : %d", audit["queries_failed"])
    log.info("  posts fetched        : %d (%d keyword, %d seed author)",
             audit["posts_fetched"], audit["posts_from_keywords"], audit["posts_from_seed_authors"])
    log.info("  unique leads         : %d", audit["unique_leads"])
    if dry_run:
        log.info("  written              : 0 (dry run)")
    else:
        log.info("  written              : %d new, %d re-seen, %d failed",
                 inserted, updated, failed)
    if classify_stats.get("candidates"):
        log.info("  classified           : %d of %d candidates (%d failed)",
                 classify_stats["classified"], classify_stats["candidates"],
                 classify_stats["failed"])
        log.info("  event-relevant       : %d (%d at confidence >= %d)",
                 classify_stats["relevant"], classify_stats["high_confidence"],
                 HIGH_CONFIDENCE_THRESHOLD)
        for kind, n in sorted(classify_stats["by_kind"].items(), key=lambda x: -x[1]):
            log.info("      %-20s %d", kind, n)
    log.info("  apify billed         : $%.4f", audit["usage_total_usd"])
    if audit["usage_total_usd"] > APIFY_WARN_ABOVE_USD:
        log.warning(
            "  Apify spend $%.4f exceeded the $%.2f warn threshold (%d posts over "
            "%d queries). Not an abort — check scripts/data/apify_spend.jsonl for "
            "which queries moved before changing anything.",
            audit["usage_total_usd"], APIFY_WARN_ABOVE_USD,
            audit["posts_fetched"], audit["queries_run"],
        )
    log.info("  anthropic billed     : $%.4f", classify_stats.get("cost_usd", 0.0))
    log.info("  total billed         : $%.4f",
             audit["usage_total_usd"] + classify_stats.get("cost_usd", 0.0))
    if audit["usage_unreported_queries"]:
        log.warning(
            "  %d queries returned no cost figure — the total above is a floor, not a total",
            audit["usage_unreported_queries"],
        )
    if adapter.transient_retries:
        log.info("  transient retries    : %d", adapter.transient_retries)
    for label, err in adapter.failed_queries:
        log.warning("  failed query: %s — %s", label, err)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Discover event leads from LinkedIn posts (keywords + seed organiser pages).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the queries and print results without writing to Supabase")
    parser.add_argument("--keywords-only", action="store_true",
                        help="Only run the keyword searches")
    parser.add_argument("--authors-only", action="store_true",
                        help="Only run the seed organiser page searches")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Cap at N queries (N=1 is a single actor call)")
    parser.add_argument("--seed-authors", default=None, metavar="PATH",
                        help="Override the seed author CSV path")
    parser.add_argument("--skip-classify", action="store_true",
                        help="Discover and write leads without running the Haiku classifier")
    args = parser.parse_args()
    sys.exit(main(
        dry_run=args.dry_run,
        keywords_only=args.keywords_only,
        authors_only=args.authors_only,
        limit=args.limit,
        seed_authors_path=args.seed_authors,
        skip_classify=args.skip_classify,
    ))
