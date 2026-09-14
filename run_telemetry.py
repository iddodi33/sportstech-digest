"""run_telemetry.py — append-only instrumentation for the scheduled pipelines.

Rolling logs, all JSONL (one JSON object per line) so a week of runs accumulates
without rewriting earlier records:

  scripts/data/daily_monitor_usage.jsonl     real billed token usage per API call
  scripts/data/regional_cap_drops.jsonl      items CAP_REGIONAL truncated
  scripts/data/regional_feed_stats.jsonl     per-feed outcome, including zeroes
  scripts/data/apify_spend.jsonl             real billed Apify $ per actor call

Token counts come from `response.usage` on the Anthropic response object — the
actual billed figures the API returns, not a token-counter estimate and not
arithmetic over prompt text. Apify costs likewise come from `usageTotalUsd` on
the run object, i.e. the platform's own billed total, not our arithmetic over
its published rates (see the Apify section at the bottom of this file).

Persistence: these files are committed back to the repo by the workflow's existing
"Commit seen URLs" step. A GitHub Actions runner's filesystem is discarded when the
job ends, so a local append alone would silently produce nothing from scheduled
runs. See the docstring on _DATA_DIR below for why that route was chosen over a
Supabase table or a workflow artifact.

Every writer here is failure-tolerant: instrumentation must never break a
production run, so all IO is wrapped and errors are logged, not raised.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# scripts/data/ already holds the audit outputs (alerts_scored.csv,
# alerts_token_estimate.json, ...), so these logs sit alongside their own kind.
# Anchored to __file__ rather than cwd so it resolves the same whether invoked as
# `python daily_monitor.py` from the repo root or imported from scripts/.
#
# Chosen persistence route: commit back from the workflow. daily_monitor.yml
# already runs with `permissions: contents: write` and already commits and pushes
# daily_monitor_seen.json every run, so this reuses machinery that exists and is
# proven rather than adding any. A Supabase table was the obvious alternative and
# supabase_client is already authenticated here, but a new table plus RLS plus a
# migration is not proportionate for two low-volume append-only logs whose whole
# purpose is to be read by a human a week from now and then acted on. Workflow
# artifacts were rejected outright: they expire, and they cannot accumulate across
# runs, which is the one thing these logs need to do.
_DATA_DIR = Path(__file__).resolve().parent / "scripts" / "data"

USAGE_LOG = _DATA_DIR / "daily_monitor_usage.jsonl"
CAP_DROPS_LOG = _DATA_DIR / "regional_cap_drops.jsonl"
FEED_STATS_LOG = _DATA_DIR / "regional_feed_stats.jsonl"

# claude-sonnet-4-5-20250929 standard (non-batch) rates per claude.com/pricing.
# Stored on every record so a later reader can tell which pricing was applied
# rather than having to guess what the rates were on the day.
RATE_INPUT_PER_MTOK = 3.00
RATE_OUTPUT_PER_MTOK = 15.00
PRICING_VERIFIED_ON = "2026-09-03"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, record: dict) -> None:
    """Append one JSON object as a line. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("telemetry: failed to append to %s — %s", path.name, exc)


def usage_from_response(response) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) off an Anthropic response. (0, 0) if absent."""
    try:
        usage = response.usage
        return int(usage.input_tokens), int(usage.output_tokens)
    except Exception:
        return 0, 0


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * RATE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * RATE_OUTPUT_PER_MTOK
    )


def record_call(
    call: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    articles: int = 0,
    batches: int = 0,
    per_batch: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    """Append one per-run usage record. Returns the record (for logging)."""
    record = {
        "timestamp":            _now(),
        "call":                 call,
        "model":                model,
        "articles":             articles,
        "batches":              batches,
        "input_tokens":         input_tokens,
        "output_tokens":        output_tokens,
        "cost_usd":             round(cost_usd(input_tokens, output_tokens), 6),
        "rate_input_per_mtok":  RATE_INPUT_PER_MTOK,
        "rate_output_per_mtok": RATE_OUTPUT_PER_MTOK,
        "pricing_verified_on":  PRICING_VERIFIED_ON,
    }
    if per_batch:
        record["per_batch"] = per_batch
    if extra:
        record.update(extra)
    _append(USAGE_LOG, record)
    return record


def record_feed_stats(feed_url: str, source_label: str, status: str, *,
                      entries_fetched: int = 0, entries_in_window: int = 0,
                      kept_after_cap: int = 0, cap: int = 0,
                      error: str = "", run_ts: str | None = None) -> None:
    """Append one record per regional feed per run — including feeds that yielded zero.

    Two things depend on this. First, a feed that times out otherwise produces a
    log.warning and then nothing, which in the telemetry is indistinguishable from a
    feed that simply published nothing; `status` separates those. Second, the cap-drop
    log records only the numerator — this is the denominator, without which the
    CAP_REGIONAL decision would be half-informed.

    status: ok | zero_entries | error
    """
    _append(FEED_STATS_LOG, {
        "run_timestamp":     run_ts or _now(),
        "feed_url":          feed_url,
        "source":            source_label,
        "status":            status,
        "entries_fetched":   entries_fetched,
        "entries_in_window": entries_in_window,
        "kept_after_cap":    kept_after_cap,
        "dropped_by_cap":    max(entries_in_window - kept_after_cap, 0),
        "cap":               cap,
        "error":             error,
    })


def record_cap_drops(feed_url: str, source_label: str, dropped: list[dict],
                     cap: int, run_ts: str | None = None) -> None:
    """Append one line per item CAP_REGIONAL truncated.

    The cap truncates by recency, not relevance, so the open question is whether
    these would have scored. Logging them makes that answerable from real drops.
    """
    if not dropped:
        return
    ts = run_ts or _now()
    for item in dropped:
        _append(CAP_DROPS_LOG, {
            "run_timestamp": ts,
            "feed_url":      feed_url,
            "source":        source_label,
            "cap":           cap,
            "title":         item.get("title", ""),
            "pubDate":       item.get("pubDate", ""),
            "link":          item.get("link", ""),
        })


# ── Apify spend ───────────────────────────────────────────────────────────────
#
# Deliberately a parallel structure to the Anthropic accounting above, not an
# extension of it. Everything above this line meters *token* spend against a
# per-MTok model rate table; this meters *vendor event* spend on the Apify
# platform, which has no tokens and no model. Sharing the rate constants or the
# usage log between the two would make each one lie about the other — and
# CLAUDE.md's "model bumps must update run_telemetry.py's rates" rule would
# start firing on changes that have nothing to do with Anthropic pricing.
#
# Same append-only JSONL discipline, same failure-tolerance, same commit-back
# persistence (events_weekly.yml grew a commit step for this file).

APIFY_SPEND_LOG = _DATA_DIR / "apify_spend.jsonl"

# harvestapi/linkedin-post-search — PAY_PER_EVENT, BRONZE tier.
# Read off the actor's own pricing block via the Apify API on the date below,
# not from the store listing and not from memory. These are used ONLY to
# cross-check the authoritative figure: `usage_total_usd` on each record is
# Apify's own billed total for the run, pulled from the run object. When the
# two disagree, believe Apify and re-verify these constants.
APIFY_RATE_PER_POST_USD = 0.002
APIFY_RATE_PER_EMPTY_QUERY_USD = 0.001
APIFY_RATE_ACTOR_START_USD = 0.00005  # per GB of run memory, minimum one
APIFY_PRICING_VERIFIED_ON = "2026-09-14"


def apify_estimated_cost_usd(posts: int, empty_queries: int = 0, starts: int = 1) -> float:
    """Back-of-envelope cost for a run, for cross-checking Apify's own figure."""
    return (
        posts * APIFY_RATE_PER_POST_USD
        + empty_queries * APIFY_RATE_PER_EMPTY_QUERY_USD
        + starts * APIFY_RATE_ACTOR_START_USD
    )


def record_apify_run(
    pipeline: str,
    actor: str,
    *,
    run_id: str = "",
    query_kind: str = "",
    query_value: str = "",
    posts_fetched: int = 0,
    usage_total_usd: float | None = None,
    status: str = "",
    run_ts: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Append one record per Apify actor call. Returns the record (for logging).

    usage_total_usd is Apify's own billed total for the run (run object field
    `usageTotalUsd`). It is Optional because a failed or aborted call still has
    to be logged — a run that cost money and returned nothing is exactly the
    kind of spend this log exists to make visible.
    """
    record = {
        "timestamp":            run_ts or _now(),
        "pipeline":             pipeline,
        "actor":                actor,
        "run_id":               run_id,
        "query_kind":           query_kind,   # 'keyword' | 'seed_author'
        "query_value":          query_value,
        "posts_fetched":        posts_fetched,
        "status":               status,
        "usage_total_usd":      round(usage_total_usd, 6) if usage_total_usd is not None else None,
        "estimated_usd":        round(
            apify_estimated_cost_usd(posts_fetched, empty_queries=0 if posts_fetched else 1),
            6,
        ),
        "rate_per_post_usd":    APIFY_RATE_PER_POST_USD,
        "pricing_verified_on":  APIFY_PRICING_VERIFIED_ON,
    }
    if extra:
        record.update(extra)
    _append(APIFY_SPEND_LOG, record)
    return record
