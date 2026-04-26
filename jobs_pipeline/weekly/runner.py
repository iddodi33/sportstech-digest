"""runner.py — execute each pipeline step and return structured results.

Adapter steps run via direct import (structured stats returned immediately).
Classifier and archive sweep run as subprocesses (stdout parsed for stats).
"""

import logging
import re
import subprocess
import sys
import time
import traceback

from ..supabase_jobs_client import get_active_sources, get_linkedin_sources
from ..adapters.greenhouse import GreenhouseAdapter
from ..adapters.ashby import AshbyAdapter
from ..adapters.lever import LeverAdapter
from ..adapters.personio import PersonioAdapter
from ..adapters.breezy import BreezyAdapter
from ..adapters.bamboohr import BambooHRAdapter
from ..adapters.teamtailor import TeamtailorAdapter
from ..adapters.workday import WorkdayAdapter
from ..adapters.rippling import RipplingAdapter
from ..adapters.phenom import PhenomAdapter
from ..adapters.linkedin import LinkedInAdapter

log = logging.getLogger(__name__)

CLASSIFIER_TIMEOUT = 1800   # 30 min
SWEEP_TIMEOUT = 300         # 5 min

# Canonical order — LinkedIn last (slowest, most likely to abort early)
ATS_ADAPTERS: list[tuple[str, type]] = [
    ("greenhouse",  GreenhouseAdapter),
    ("ashby",       AshbyAdapter),
    ("lever",       LeverAdapter),
    ("personio",    PersonioAdapter),
    ("breezy",      BreezyAdapter),
    ("bamboohr",    BambooHRAdapter),
    ("teamtailor",  TeamtailorAdapter),
    ("workday",     WorkdayAdapter),
    ("rippling",    RipplingAdapter),
    ("phenom",      PhenomAdapter),
]


# ── Shared helpers ─────────────────────────────────────────────────────────────

def fmt_runtime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _aggregate(platform: str, per_source: list[dict], runtime: float, error: Exception | None = None) -> dict:
    return {
        "step_name": platform,
        "status": "failed" if error else "success",
        "runtime_seconds": runtime,
        "jobs_scraped": sum(s.get("jobs_found", 0) for s in per_source),
        "jobs_new": sum(s.get("inserted", 0) for s in per_source),
        "jobs_updated": sum(s.get("updated", 0) + s.get("reactivated", 0) for s in per_source),
        "companies_processed": len(per_source),
        "companies_with_errors": sum(1 for s in per_source if s.get("errors", 0) > 0),
        "error_message": f"{type(error).__name__}: {error}" if error else None,
    }


# ── ATS adapters ───────────────────────────────────────────────────────────────

def run_ats_adapter(platform: str, adapter_class: type) -> dict:
    """Run a standard ATS adapter for all its active sources."""
    log.info("=== Starting %s adapter ===", platform)
    t0 = time.time()
    per_source: list[dict] = []

    try:
        sources = get_active_sources(platform)
        log.info("%s: %d active sources", platform, len(sources))

        if sources:
            adapter = adapter_class()
            for source in sources:
                stats = adapter.run(source)
                per_source.append(stats)

    except Exception as exc:
        runtime = time.time() - t0
        log.error("%s adapter failed: %s\n%s", platform, exc, traceback.format_exc())
        return _aggregate(platform, per_source, runtime, exc)

    runtime = time.time() - t0
    result = _aggregate(platform, per_source, runtime)
    log.info(
        "=== %s complete: %d scraped, %d new, %d updated, runtime %s ===",
        platform, result["jobs_scraped"], result["jobs_new"],
        result["jobs_updated"], fmt_runtime(runtime),
    )
    return result


def run_linkedin_adapter() -> dict:
    """Run the LinkedIn adapter with abort and session-close handling."""
    log.info("=== Starting linkedin adapter ===")
    t0 = time.time()
    per_source: list[dict] = []

    try:
        sources = get_linkedin_sources()
        log.info("linkedin: %d active sources", len(sources))

        if sources:
            adapter = LinkedInAdapter()
            try:
                for source in sources:
                    stats = adapter.run(source)
                    per_source.append(stats)
                    if adapter.abort:
                        log.warning("linkedin: aborting due to rate-limit or block signal")
                        break
            finally:
                adapter.close()

    except Exception as exc:
        runtime = time.time() - t0
        log.error("linkedin adapter failed: %s\n%s", exc, traceback.format_exc())
        return _aggregate("linkedin", per_source, runtime, exc)

    runtime = time.time() - t0
    result = _aggregate("linkedin", per_source, runtime)
    log.info(
        "=== linkedin complete: %d scraped, %d new, %d updated, runtime %s ===",
        result["jobs_scraped"], result["jobs_new"],
        result["jobs_updated"], fmt_runtime(runtime),
    )
    return result


# ── Classifier ─────────────────────────────────────────────────────────────────

def _is_credit_exhausted(output: str) -> bool:
    lower = output.lower()
    return any(
        token in lower
        for token in ("credit balance", "insufficient_quota", "rate_limit_error", "credit exhausted")
    ) or "error code: 429" in lower or ": 429 -" in output


def _parse_classifier_output(output: str, status: str, runtime: float) -> dict:
    def _int(pattern: str) -> int:
        m = re.search(pattern, output, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    total = _int(r"Found (\d+) jobs to classify")
    if not total:
        total = _int(r"Classification complete\. (\d+) jobs processed")

    approved     = _int(r"Passed.*?:\s*(\d+)")
    too_junior   = _int(r"Rejected - too_junior.*?:\s*(\d+)")
    fdi_geo      = _int(r"Rejected - fdi_geography.*?:\s*(\d+)")
    not_sports   = _int(r"Rejected - not_sportstech.*?:\s*(\d+)")
    haiku_errors = _int(r"Haiku errors.*?:\s*(\d+)")
    rejected_total = too_junior + fdi_geo + not_sports

    return {
        "status": status,
        "runtime_seconds": runtime,
        "jobs_processed": total,
        "approved": approved,
        "rejected_total": rejected_total,
        "rejected_by_reason": {
            "too_junior": too_junior,
            "fdi_geography": fdi_geo,
            "not_sportstech": not_sports,
            "haiku_errors": haiku_errors,
        },
        "jobs_with_null_function": None,  # filled in from snapshot after run
        "error_message": None,
    }


def run_classifier_step(classifier_path: str, repo_root: str) -> dict:
    """Run the classifier as a subprocess and parse its stdout for stats."""
    log.info("=== Starting classifier ===")
    t0 = time.time()

    try:
        proc = subprocess.run(
            [sys.executable, classifier_path],
            capture_output=True,
            text=True,
            timeout=CLASSIFIER_TIMEOUT,
            cwd=repo_root,
        )
        runtime = time.time() - t0
        combined = proc.stdout + "\n" + proc.stderr
        credit_issue = _is_credit_exhausted(combined)

        if proc.returncode != 0:
            status = "credit_exhausted" if credit_issue else "failed"
        elif credit_issue:
            status = "credit_exhausted"
        else:
            status = "success"

        parsed = _parse_classifier_output(combined, status, runtime)

        if status == "credit_exhausted":
            parsed["error_message"] = (
                "Anthropic credit balance too low — partial run. "
                "Check https://console.anthropic.com/settings/billing"
            )
            log.warning("=== Classifier credit exhausted after %s ===", fmt_runtime(runtime))
        elif status == "failed":
            parsed["error_message"] = combined[-500:].strip()
            log.error("=== Classifier failed (exit %d) after %s ===", proc.returncode, fmt_runtime(runtime))
        else:
            log.info(
                "=== Classifier complete: %d processed, %d approved, %d rejected, runtime %s ===",
                parsed["jobs_processed"], parsed["approved"],
                parsed["rejected_total"], fmt_runtime(runtime),
            )

        return parsed

    except subprocess.TimeoutExpired:
        runtime = time.time() - t0
        log.error("Classifier timed out after %ds", CLASSIFIER_TIMEOUT)
        return {
            "status": "failed", "runtime_seconds": runtime,
            "jobs_processed": 0, "approved": 0, "rejected_total": 0,
            "rejected_by_reason": {}, "jobs_with_null_function": None,
            "error_message": f"Timed out after {CLASSIFIER_TIMEOUT}s",
        }
    except Exception as exc:
        runtime = time.time() - t0
        log.error("Classifier step error: %s\n%s", exc, traceback.format_exc())
        return {
            "status": "failed", "runtime_seconds": runtime,
            "jobs_processed": 0, "approved": 0, "rejected_total": 0,
            "rejected_by_reason": {}, "jobs_with_null_function": None,
            "error_message": f"{type(exc).__name__}: {exc}",
        }


# ── Archive sweep ──────────────────────────────────────────────────────────────

def _parse_sweep_output(output: str, status: str, runtime: float) -> dict:
    def _int(pattern: str) -> int:
        m = re.search(pattern, output, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    archived    = _int(r"Archived:\s*(\d+)")
    no_history  = _int(r"Skipped \(no source history\):\s*(\d+)")
    health_gate = _int(r"Skipped \(source health gate\):\s*(\d+)")
    not_stale   = _int(r"Skipped \(not stale\):\s*(\d+)")

    # Parse per-source breakdown: lines after "Breakdown by source" heading
    breakdown: dict[str, int] = {}
    in_breakdown = False
    for line in output.splitlines():
        # Strip leading timestamp + level prefix if present (archive sweep log format)
        stripped = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+\S+\s+", "", line).strip()
        if "breakdown by source" in stripped.lower():
            in_breakdown = True
            continue
        if in_breakdown:
            m = re.match(r"(.+?)\s{2,}(\d+)$", stripped)
            if m:
                breakdown[m.group(1).strip()] = int(m.group(2))

    return {
        "status": status,
        "runtime_seconds": runtime,
        "jobs_archived": archived,
        "breakdown_by_source": breakdown,
        "skipped_no_history": no_history,
        "skipped_health_gate": health_gate,
        "skipped_not_stale": not_stale,
        "error_message": None,
    }


def run_archive_sweep_step(sweep_path: str, repo_root: str) -> dict:
    """Run the archive sweep as a subprocess and parse its log output for stats."""
    log.info("=== Starting archive sweep ===")
    t0 = time.time()

    try:
        proc = subprocess.run(
            [sys.executable, sweep_path],
            capture_output=True,
            text=True,
            timeout=SWEEP_TIMEOUT,
            cwd=repo_root,
        )
        runtime = time.time() - t0
        combined = proc.stdout + "\n" + proc.stderr

        if proc.returncode != 0:
            log.error("Archive sweep failed (exit %d) after %s", proc.returncode, fmt_runtime(runtime))
            result = _parse_sweep_output(combined, "failed", runtime)
            result["error_message"] = combined[-500:].strip()
            return result

        result = _parse_sweep_output(combined, "success", runtime)
        log.info(
            "=== Archive sweep complete: %d archived, runtime %s ===",
            result["jobs_archived"], fmt_runtime(runtime),
        )
        return result

    except subprocess.TimeoutExpired:
        runtime = time.time() - t0
        log.error("Archive sweep timed out after %ds", SWEEP_TIMEOUT)
        return {
            "status": "failed", "runtime_seconds": runtime, "jobs_archived": 0,
            "breakdown_by_source": {}, "skipped_no_history": 0,
            "skipped_health_gate": 0, "skipped_not_stale": 0,
            "error_message": f"Timed out after {SWEEP_TIMEOUT}s",
        }
    except Exception as exc:
        runtime = time.time() - t0
        log.error("Archive sweep step error: %s\n%s", exc, traceback.format_exc())
        return {
            "status": "failed", "runtime_seconds": runtime, "jobs_archived": 0,
            "breakdown_by_source": {}, "skipped_no_history": 0,
            "skipped_health_gate": 0, "skipped_not_stale": 0,
            "error_message": f"{type(exc).__name__}: {exc}",
        }
