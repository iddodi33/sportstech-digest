"""run_weekly.py — weekly jobs pipeline orchestrator.

Execution order:
  1.  11 ATS adapters: greenhouse → ashby → lever → personio → breezy →
      bamboohr → teamtailor → workday → rippling → phenom → linkedin
  2.  Classifier (run_classifier.py via subprocess)
  3.  Archive sweep (run_archive_sweep.py via subprocess, live mode)
  4.  SendGrid summary email to ALERT_TO

Flags:
  --skip-adapters   skip all 11 adapter runs, go straight to classifier
  --skip-email      print email to stdout instead of sending via SendGrid

Both flags may be combined.

Usage:
  python jobs_pipeline/run_weekly.py
  python jobs_pipeline/run_weekly.py --skip-adapters --skip-email
  python jobs_pipeline/run_weekly.py --skip-email
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Support: python jobs_pipeline/run_weekly.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT    = os.path.dirname(_PIPELINE_DIR)

_REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "SENDGRID_API_KEY",
    "ALERT_FROM",
    "ALERT_TO",
    "NEXT_PUBLIC_SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SERPER_API_KEY",
]


def _check_env() -> bool:
    missing = [v for v in _REQUIRED_ENV if not os.getenv(v)]
    if missing:
        for v in missing:
            log.error("Missing required env var: %s", v)
    return not missing


def _fmt_runtime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s:02d}s" if h else f"{m}m {s:02d}s"


def main(skip_adapters: bool = False, skip_email: bool = False) -> None:
    run_started_at = datetime.now(timezone.utc)
    wall_t0 = time.time()

    log.info("=== Sports D3c0d3d weekly jobs pipeline starting ===")
    log.info("flags: skip_adapters=%s  skip_email=%s", skip_adapters, skip_email)

    if not _check_env():
        log.error("Aborting — missing env vars (see above)")
        sys.exit(1)

    from jobs_pipeline.supabase_jobs_client import get_client
    client = get_client()
    if client is None:
        log.error(
            "Aborting — could not connect to Supabase. "
            "Check NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
        sys.exit(1)

    from jobs_pipeline.weekly.runner import (
        ATS_ADAPTERS,
        run_ats_adapter,
        run_linkedin_adapter,
        run_classifier_step,
        run_archive_sweep_step,
    )
    from jobs_pipeline.weekly.snapshot import fetch_snapshot
    from jobs_pipeline.weekly.email_builder import build_email
    from jobs_pipeline.weekly.sendgrid_client import send_email

    adapter_results: list[dict] = []

    # ── 1. ATS adapters ───────────────────────────────────────────────────────
    if skip_adapters:
        log.info("Skipping all adapter runs (--skip-adapters)")
    else:
        for platform, adapter_class in ATS_ADAPTERS:
            result = run_ats_adapter(platform, adapter_class)
            adapter_results.append(result)

        result = run_linkedin_adapter()
        adapter_results.append(result)

    # ── 2. Classifier ─────────────────────────────────────────────────────────
    classifier_path = os.path.join(_PIPELINE_DIR, "run_classifier.py")
    classifier_result = run_classifier_step(classifier_path, _REPO_ROOT)

    # ── 3. Archive sweep ──────────────────────────────────────────────────────
    sweep_path = os.path.join(_PIPELINE_DIR, "run_archive_sweep.py")
    sweep_result = run_archive_sweep_step(sweep_path, _REPO_ROOT)

    # ── 4. DB snapshot ────────────────────────────────────────────────────────
    log.info("Fetching DB snapshot...")
    snapshot = fetch_snapshot(client)
    # Back-fill jobs_with_null_function from snapshot (classifier can't report it)
    classifier_result["jobs_with_null_function"] = snapshot.get("pending_null_function")

    # ── 5. Build email ────────────────────────────────────────────────────────
    total_runtime = time.time() - wall_t0
    html_body = build_email(
        adapter_results=adapter_results,
        classifier_result=classifier_result,
        sweep_result=sweep_result,
        snapshot=snapshot,
        run_started_at=run_started_at,
        total_runtime_seconds=total_runtime,
    )
    date_str = run_started_at.strftime("%Y-%m-%d")
    subject = f"Sports D3c0d3d weekly jobs run — {date_str}"

    # ── 6. Send or print ──────────────────────────────────────────────────────
    if skip_email:
        log.info("Skipping email send (--skip-email) — printing to stdout")
        _print_email(subject, html_body)
    else:
        log.info("Sending summary email to %s...", os.getenv("ALERT_TO", "?"))
        ok = send_email(subject, html_body)
        if not ok:
            log.error(
                "SendGrid send failed — printing email body to stdout for log preservation"
            )
            _print_email(subject, html_body)
            sys.exit(1)

    log.info(
        "=== Weekly pipeline complete. Total runtime: %s ===",
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
        description="Run the full weekly Sports D3c0d3d jobs pipeline",
    )
    parser.add_argument(
        "--skip-adapters",
        action="store_true",
        help="Skip all 11 adapter runs (jump straight to classifier)",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Print email to stdout instead of sending via SendGrid",
    )
    args = parser.parse_args()
    main(skip_adapters=args.skip_adapters, skip_email=args.skip_email)
