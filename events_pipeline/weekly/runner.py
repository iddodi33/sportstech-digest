"""runner.py — per-URL extraction and upsert for the events orchestrator."""

import logging
import time
import traceback
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_SLEEP_BETWEEN_EXTRACTIONS = 0.5  # seconds — be polite to Anthropic API


@dataclass
class ExtractionResult:
    url: str
    source_name: str
    # 'success', 'failed', 'skipped_irrelevant'
    status: str = "failed"
    category: str | None = None
    name: str | None = None
    date: str | None = None
    was_inserted: bool | None = None
    event_id: str | None = None
    error_message: str | None = None
    runtime_seconds: float = 0.0


def run_extractions(
    url_to_source: dict[str, str],
) -> list[ExtractionResult]:
    """Extract and upsert events for each URL.

    Args:
        url_to_source: mapping of canonical URL → source_name (first adapter to find it).

    Returns a list of ExtractionResult, one per URL.
    """
    from events_pipeline.extractor import extract_event, ExtractorError
    from events_pipeline.supabase_events_client import upsert_event

    results: list[ExtractionResult] = []
    total = len(url_to_source)

    for i, (url, source_name) in enumerate(url_to_source.items(), 1):
        log.info("[runner] [%d/%d] extracting: %s", i, total, url[:80])
        t0 = time.time()
        result = ExtractionResult(url=url, source_name=source_name)

        try:
            extraction = extract_event(url)
            category = extraction.get("relevance_category", "not_relevant")
            result.category = category
            result.name  = extraction.get("name")
            result.date  = extraction.get("date")

            if category == "not_relevant":
                result.status = "skipped_irrelevant"
                log.info(
                    "[runner] skipping (not_relevant): %s — %s",
                    url[:60], extraction.get("relevance_reason", ""),
                )
            else:
                event_id, was_inserted = upsert_event(extraction, source=source_name)
                result.event_id     = event_id
                result.was_inserted = was_inserted
                result.status       = "success"
                action = "INSERTED" if was_inserted else "updated"
                log.info(
                    "[runner] %s [%s]: %s — %s",
                    action, category, result.name or "?", url[:60],
                )

        except ExtractorError as exc:
            result.status        = "failed"
            result.error_message = str(exc)
            log.error("[runner] extraction failed for %s: %s", url[:60], exc)
        except Exception as exc:
            result.status        = "failed"
            result.error_message = f"{type(exc).__name__}: {exc}"
            log.error(
                "[runner] unexpected error for %s: %s\n%s",
                url[:60], exc, traceback.format_exc(),
            )

        result.runtime_seconds = time.time() - t0
        results.append(result)

        if i < total:
            time.sleep(_SLEEP_BETWEEN_EXTRACTIONS)

    return results
