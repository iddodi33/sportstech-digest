"""run_telemetry.py — append-only instrumentation for the daily news run.

Two rolling logs, both JSONL (one JSON object per line) so a week of runs
accumulates without rewriting earlier records:

  scripts/data/daily_monitor_usage.jsonl     real billed token usage per API call
  scripts/data/regional_cap_drops.jsonl      items CAP_REGIONAL truncated

Token counts come from `response.usage` on the Anthropic response object — the
actual billed figures the API returns, not a token-counter estimate and not
arithmetic over prompt text.

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
