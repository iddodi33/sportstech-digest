"""lead_classifier.py — Haiku relevance classification for LinkedIn event leads.

The step v1 of the adapter deliberately omitted. It exists now because the first
real run (2026-09-14) put 384 leads in event_leads in a single week, which is
more than anyone will read, and because that run finally provided a week of
production posts to tune a prompt against instead of guessing cold.

Model and calling pattern follow jobs_pipeline/classifier.py — same Haiku model
id, same messages.create shape, same "strip the code fence then json.loads"
handling — so there is one classifier idiom in this repo rather than two. The
retry wrapper is claude_budget.call_claude_with_retry, shared with the news
pipelines.

It labels; it never deletes
---------------------------
A low score writes is_event_relevant=false and leaves the row exactly where it
was. Nothing is dropped, and `status` is never touched — that column is the
human's. A classifier that silently deleted its own low scorers would hide its
false negatives, and false negatives are the specific failure this whole source
exists to fix: the Meath conference was already missed by five adapters and a
manual web search. The triage query sorts by confidence instead:

    select * from event_leads
    where status = 'pending' and is_event_relevant
    order by relevance_confidence desc;

NO ABORTING COST CEILING, consistent with the rest of this pipeline
-------------------------------------------------------------------
Iddo's standing call for the LinkedIn source. This step warns above
WARN_ABOVE_USD and keeps going; it never aborts. Spend is bounded structurally
instead: one call per *unclassified* lead, and a lead is classified once ever
(classified_at is the guard), so a re-run costs nothing and the per-run ceiling
is the number of genuinely new posts LinkedIn produced that week. At Haiku rates
and ~380 leads that is roughly $0.30.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# Same model as jobs_pipeline/classifier.py — deliberately. If one moves, move
# both, and update run_telemetry's RATE_HAIKU_* in the same change (CLAUDE.md).
MODEL = "claude-haiku-4-5-20251001"

# Enough for the JSON object below and nothing more; these are short answers.
_MAX_TOKENS = 512

# Post text sent to the model. LinkedIn posts run long and the tail is usually
# hashtags, which carry almost no signal for this decision and would be a third
# of the input bill at 384 posts a week.
_CONTENT_CHARS = 1800

# Warn-only threshold — see the module docstring. Set at roughly 3x the expected
# per-run spend, the same generous-multiple convention used for the Apify log.
WARN_ABOVE_USD = 1.00

_VALID_POST_KINDS = frozenset({
    "event_announcement",
    "event_recap",
    "attendee_post",
    "generic_content",
    "job_post",
    "other",
})

_SYSTEM_PROMPT = (
    "You triage LinkedIn posts for Sports D3c0d3d, an Irish sportstech intelligence "
    "service that maintains a public listing of upcoming sport, sportstech and "
    "sport-innovation events in Ireland, the UK and Europe.\n\n"
    "You are given one LinkedIn post. Decide whether it announces a SPECIFIC, "
    "IDENTIFIABLE, UPCOMING event that a reader could attend or register for.\n\n"
    "Return ONLY a JSON object, no prose and no code fence:\n"
    "{\n"
    '  "post_kind": "event_announcement" | "event_recap" | "attendee_post" | '
    '"generic_content" | "job_post" | "other",\n'
    '  "is_event_relevant": true | false,\n'
    '  "confidence": <integer 0-100>,\n'
    '  "reason": "<one short sentence, max 160 characters>"\n'
    "}\n\n"
    "post_kind definitions — pick the single best fit:\n"
    "  event_announcement: announces an upcoming event, conference, summit, webinar, "
    "workshop, meetup, expo, hackathon, awards night or programme launch. Speaker "
    "reveals, agenda reveals, 'registration now open' and 'save the date' for a named "
    "future event all count.\n"
    "  event_recap: describes an event that has already happened — 'great day at', "
    "'thanks to everyone who came', photo round-ups, highlights.\n"
    "  attendee_post: the author is attending, speaking at or travelling to someone "
    "else's event, but the post is about their own participation rather than an "
    "announcement a reader could act on. Treat 'come find me at stand 4' as this.\n"
    "  generic_content: thought leadership, product news, funding news, company "
    "milestones, reports, opinion, congratulations — no event.\n"
    "  job_post: a vacancy or hiring call.\n"
    "  other: anything genuinely none of the above.\n\n"
    "is_event_relevant is true ONLY for post_kind = event_announcement.\n\n"
    "confidence is your confidence in the post_kind label, 0-100. Use the full range. "
    "Be decisive on clear cases: a named conference with a date and a registration "
    "link is 90+; an unmistakable product-news post is 90+ for generic_content. "
    "Reserve 40-60 for posts that genuinely sit between two labels, such as a recap "
    "that also trails next year's edition.\n\n"
    "Judgement notes:\n"
    "- Ireland/UK/Europe is preferred but NOT required. A major event elsewhere that "
    "an Irish sportstech audience would travel to still counts.\n"
    "- The event does not have to be sportstech. A county Local Sports Partnership's "
    "women-in-sport conference is exactly the kind of thing being looked for.\n"
    "- Recurring series ('our monthly meetup, next one Tuesday') count as "
    "event_announcement.\n"
    "- Do not reward event-sounding vocabulary on its own. 'Register your interest in "
    "our platform' or 'join the conversation' is generic_content, not an event.\n"
    "- If the post is too short or too vague to tell, say so in reason and give a low "
    "confidence rather than guessing a specific label."
)


def _build_user_prompt(lead: dict) -> str:
    content = (lead.get("content") or "").strip()
    if len(content) > _CONTENT_CHARS:
        content = content[:_CONTENT_CHARS] + " […truncated]"

    author = lead.get("author_name") or "(unknown)"
    author_type = lead.get("author_type") or "unknown"
    posted = (lead.get("posted_at") or "")[:10] or "(unknown date)"
    matched = ", ".join(lead.get("matched_all") or []) or "(unknown)"

    return (
        f"Author: {author} (LinkedIn {author_type} page)\n"
        f"Posted: {posted}\n"
        f"Surfaced by: {matched}\n"
        f"---\n"
        f"{content or '(no post text)'}"
    )


def classify_with_haiku(lead: dict, anthropic_client, run_cost) -> tuple[dict, object]:
    """Call Haiku to classify one lead. Returns (parsed JSON, raw response).

    Raises anthropic.APIError or json.JSONDecodeError on failure — callers
    should catch and handle (log + skip; the lead stays unclassified and is
    retried on the next run, since classified_at is the work-queue guard).
    """
    from claude_budget import call_claude_with_retry

    response = call_claude_with_retry(
        anthropic_client,
        run_cost,
        model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(lead)}],
    )

    text = response.content[0].text.strip()

    # Strip markdown code fences if Haiku wraps the JSON — same handling as
    # jobs_pipeline/classifier.py, for the same reason.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)

    return json.loads(text), response


def normalise(raw: dict) -> dict:
    """Clamp a raw model response to what the event_leads CHECK constraints accept.

    A model returning an off-menu post_kind or a confidence of 1.0-instead-of-100
    must not fail the whole run's write, so every field is coerced here and an
    unrecognised value becomes 'other' rather than an exception.
    """
    kind = str(raw.get("post_kind") or "").strip().lower().replace(" ", "_")
    if kind not in _VALID_POST_KINDS:
        log.warning("lead_classifier: unrecognised post_kind %r — storing 'other'", kind)
        kind = "other"

    try:
        confidence = int(round(float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))

    # is_event_relevant is derived from post_kind, not taken on trust: the prompt
    # defines it as "true only for event_announcement", and a model that returns
    # the two fields inconsistently should not be able to put a recap into the
    # relevant queue. The label is the decision; the boolean is a view of it.
    is_relevant = kind == "event_announcement"
    if bool(raw.get("is_event_relevant")) != is_relevant:
        log.debug(
            "lead_classifier: is_event_relevant=%r disagreed with post_kind=%r — "
            "deriving from post_kind",
            raw.get("is_event_relevant"), kind,
        )

    reason = (raw.get("reason") or "").strip() or None
    if reason and len(reason) > 300:
        reason = reason[:300]

    return {
        "post_kind": kind,
        "is_event_relevant": is_relevant,
        "relevance_confidence": confidence,
        "rating_notes": reason,
        "classification": raw,
        "classifier_model": MODEL,
    }
