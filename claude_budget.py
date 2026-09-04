"""claude_budget.py — shared cost metering and circuit breaker for the news pipelines.

Both scorers (daily_monitor.py and digest.py) run unattended on a cron, which is
exactly where a runaway spend goes unnoticed. This module owns the machinery they
share:

  RunCost                 accumulates actual billed usage across one run
  call_claude_with_retry  retries transient API errors, metering every response
  within_budget           prices a prospective call before making it

The ceiling is a **parameter, not a constant here**: it is a per-run ceiling, and the
two pipelines have legitimately different per-run volumes (a 72h window versus a
35-40 day one). Each pipeline owns its own value and passes it in.

Metering lives inside call_claude_with_retry rather than at the call sites, so a
logical call that issues several billed requests before succeeding is counted once
per request that actually returned. The retry path is precisely the runaway this
guards against, and metering per response cannot miss it.
"""

from __future__ import annotations

import logging
import time

import anthropic

import run_telemetry

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class RunCost:
    """Accumulates actual billed usage across every Anthropic call in one run."""

    def __init__(self, ceiling_usd: float, label: str = "run"):
        self.ceiling_usd = ceiling_usd
        self.label = label
        self.reset()

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.requests = 0
        self.tripped = False
        self.abort_details: dict = {}

    def add(self, response) -> tuple[int, int]:
        i, o = run_telemetry.usage_from_response(response)
        self.input_tokens += i
        self.output_tokens += o
        self.requests += 1
        return i, o

    @property
    def cost(self) -> float:
        return run_telemetry.cost_usd(self.input_tokens, self.output_tokens)

    def over_ceiling(self) -> bool:
        return self.cost > self.ceiling_usd

    def trip(self, **details) -> None:
        self.tripped = True
        self.abort_details = details


def call_claude_with_retry(client, run_cost: RunCost, **kwargs):
    """client.messages.create with retries, metering every billed response.

    Adopting this in a pipeline that had no retry logic is a fix — a transient API
    error previously dropped a batch silently — but it also means one logical call
    can now issue up to MAX_ATTEMPTS billed requests. Ceilings must be set with that
    multiplier in mind.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.messages.create(**kwargs)
            run_cost.add(response)   # meter every billed response, retries included
            return response
        except (anthropic.APIConnectionError, anthropic.APIStatusError,
                anthropic.InternalServerError) as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("Claude API error (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, MAX_ATTEMPTS, exc, wait)
            time.sleep(wait)


def within_budget(client, run_cost: RunCost, model: str, prompt: str,
                  max_tokens: int, hard_stop_usd: float, what: str = "call") -> bool:
    """True if a prospective call fits under hard_stop_usd.

    Prices the real prompt with Anthropic's free token counter and takes worst-case
    output as max_tokens (the API cannot bill more than that per call), so the
    projection is an upper bound, not an estimate.

    Used to bound the completion path after the ceiling has tripped: the ceiling
    stops a run *expanding* its spend, but should not abandon work already paid for.
    The exception stays measured rather than asserted.
    """
    try:
        counted = client.messages.count_tokens(
            model=model, messages=[{"role": "user", "content": prompt}],
        )
        projected = run_telemetry.cost_usd(counted.input_tokens, max_tokens)
    except Exception as exc:
        # Cannot measure. Ample headroom if the breaker has not tripped; if it has,
        # refuse to spend blind.
        if run_cost.tripped:
            log.error("Cost ceiling hit and %s could not be counted (%s) — skipping "
                      "rather than spending unmeasured.", what, exc)
            return False
        log.warning("Token count failed for %s (%s) — proceeding, breaker not tripped.",
                    what, exc)
        return True

    if run_cost.cost + projected > hard_stop_usd:
        log.error("Skipping %s — accumulated $%.4f + projected $%.4f would exceed the "
                  "$%.2f hard stop.", what, run_cost.cost, projected, hard_stop_usd)
        return False

    if run_cost.tripped:
        log.warning("Cost ceiling hit ($%.4f), but running %s as completion: projected "
                    "$%.4f (worst case) keeps the run under the $%.2f hard stop.",
                    run_cost.cost, what, projected, hard_stop_usd)
    return True
