"""run_weekly_events.py — weekly events pipeline orchestrator.

Execution order:
  1. All 5 source adapters (discover event detail URLs)
  2. Global dedup across adapters
  3. Per-URL: extract with Claude, upsert relevant events to hub Supabase
  4. DB snapshot
  5. SendGrid summary email

Flags:
  --skip-adapters    skip discovery, go straight to email (shows 0 extractions)
  --skip-email       print email to stdout instead of sending via SendGrid
  --limit N          cap at N URLs after dedup (default: unlimited)
  --source NAME      only run one specific adapter by source_name

Both --skip-adapters and --skip-email can be combined.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

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

_REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "SENDGRID_API_KEY",
    "NEXT_PUBLIC_SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
]


def _check_env() -> bool:
    missing = [v for v in _REQUIRED_ENV if not os.getenv(v)]
    for v in missing:
        log.error("Missing required env var: %s", v)
    return not missing


def _fmt_runtime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _build_adapter_registry() -> dict:
    from events_pipeline.adapters.sport_for_business import SportForBusinessAdapter
    from events_pipeline.adapters.eventbrite_ireland import EventbriteIrelandAdapter
    from events_pipeline.adapters.meetup import MeetupAdapter
    from events_pipeline.adapters.irish_diversity_in_tech import IrishDiversityInTechAdapter
    from events_pipeline.adapters.ai_tinkerers_dublin import AiTinkerersDublinAdapter

    adapters = [
        SportForBusinessAdapter(),
        EventbriteIrelandAdapter(),
        MeetupAdapter(),
        IrishDiversityInTechAdapter(),
        AiTinkerersDublinAdapter(),
    ]
    return {a.source_name: a for a in adapters}


def main(
    skip_adapters: bool = False,
    skip_email: bool = False,
    limit: int | None = None,
    source_filter: str | None = None,
) -> None:
    run_started_at = datetime.now(timezone.utc)
    wall_t0 = time.time()

    log.info("=== Sports D3c0d3d weekly events pipeline starting ===")
    log.info(
        "flags: skip_adapters=%s skip_email=%s limit=%s source=%s",
        skip_adapters, skip_email, limit, source_filter,
    )

    if not _check_env():
        log.error("Aborting — missing env vars (see above)")
        sys.exit(1)

    from events_pipeline.supabase_events_client import get_supabase_client
    client = get_supabase_client()
    if client is None:
        log.error(
            "Aborting — could not connect to Supabase. "
            "Check NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
        sys.exit(1)

    from events_pipeline.weekly.runner import run_extractions
    from events_pipeline.weekly.snapshot import fetch_snapshot
    from events_pipeline.weekly.email_builder import build_email
    from events_pipeline.weekly.sendgrid_client import send_email

    adapter_results = []

    # ── 1. Discovery ───────────────────────────────────────────────────────────
    if skip_adapters:
        log.info("Skipping adapter runs (--skip-adapters)")
    else:
        registry = _build_adapter_registry()

        if source_filter:
            if source_filter not in registry:
                log.error(
                    "Unknown source %r. Valid values: %s",
                    source_filter, ", ".join(sorted(registry)),
                )
                sys.exit(1)
            adapters_to_run = {source_filter: registry[source_filter]}
        else:
            adapters_to_run = registry

        for source_name, adapter in adapters_to_run.items():
            log.info("=== Starting %s adapter ===", source_name)
            result = adapter.run()
            adapter_results.append(result)
            log.info(
                "=== %s complete: %d URLs, runtime %s ===",
                source_name, len(result.urls_discovered), _fmt_runtime(result.runtime_seconds),
            )

    # ── 2. Global URL dedup (first-seen adapter wins as source) ───────────────
    url_to_source: dict[str, str] = {}
    for ar in adapter_results:
        for url in ar.urls_discovered:
            if url not in url_to_source:
                url_to_source[url] = ar.source_name

    total_unique = len(url_to_source)
    total_discovered = sum(len(ar.urls_discovered) for ar in adapter_results)
    log.info(
        "URLs: %d discovered across adapters, %d unique after dedup",
        total_discovered, total_unique,
    )

    if limit is not None and total_unique > limit:
        log.info("Applying --limit %d (dropping %d URLs)", limit, total_unique - limit)
        url_to_source = dict(list(url_to_source.items())[:limit])

    # ── 3. Extraction + upsert ─────────────────────────────────────────────────
    extraction_results = []
    if url_to_source:
        log.info("=== Starting extraction for %d URLs ===", len(url_to_source))
        extraction_results = run_extractions(url_to_source)
        inserted = sum(1 for r in extraction_results if r.was_inserted)
        relevant = sum(1 for r in extraction_results if r.status == "success")
        log.info(
            "=== Extraction complete: %d relevant, %d new, runtime so far %s ===",
            relevant, inserted, _fmt_runtime(time.time() - wall_t0),
        )
    else:
        log.info("No URLs to extract.")

    # ── 4. Snapshot ────────────────────────────────────────────────────────────
    log.info("Fetching DB snapshot...")
    snapshot = fetch_snapshot(client)

    # ── 5. Email ───────────────────────────────────────────────────────────────
    total_runtime = time.time() - wall_t0
    html_body = build_email(
        adapter_results=adapter_results,
        extraction_results=extraction_results,
        snapshot=snapshot,
        run_started_at=run_started_at,
        total_runtime_seconds=total_runtime,
    )

    date_str = run_started_at.strftime("%Y-%m-%d")
    subject  = f"Sports D3c0d3d weekly events run — {date_str}"

    if skip_email:
        log.info("Skipping email send (--skip-email) — printing to stdout")
        _print_email(subject, html_body)
    else:
        log.info("Sending summary email...")
        ok = send_email(subject, html_body)
        if not ok:
            log.error("SendGrid send failed — printing email to stdout for log preservation")
            _print_email(subject, html_body)
            sys.exit(1)

    log.info(
        "=== Weekly events pipeline complete. Total runtime: %s ===",
        _fmt_runtime(total_runtime),
    )


def _print_email(subject: str, body: str) -> None:
    print()
    print("=" * 72)
    print(f"SUBJECT: {subject}")
    print("=" * 72)
    print(body)
    print("=" * 72)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the full weekly Sports D3c0d3d events pipeline",
    )
    parser.add_argument(
        "--skip-adapters",
        action="store_true",
        help="Skip all adapter runs (jump straight to email with 0 extractions)",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Print email to stdout instead of sending via SendGrid",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap at N URLs after dedup (useful for first runs)",
    )
    parser.add_argument(
        "--source",
        default=None,
        metavar="NAME",
        help=(
            "Only run one adapter. Valid values: "
            "sport_for_business, eventbrite_ireland, meetup, "
            "irish_diversity_in_tech, ai_tinkerers_dublin"
        ),
    )
    args = parser.parse_args()
    main(
        skip_adapters=args.skip_adapters,
        skip_email=args.skip_email,
        limit=args.limit,
        source_filter=args.source,
    )
