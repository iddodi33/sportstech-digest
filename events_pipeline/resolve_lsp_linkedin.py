"""resolve_lsp_linkedin.py — one-off setup script: resolve the 29 Local Sports
Partnerships to their LinkedIn company pages.

Run by hand, never by a workflow. Writes a PROPOSAL CSV
(events_pipeline/data/lsp_linkedin_resolved.csv) for human review. It does not
write the file the adapter reads (data/linkedin_seed_authors.csv) — a resolved
row only becomes a live seed after someone opens the LinkedIn page and confirms
it is the right organisation.

Why the two-file split, and why the corroboration gate:
CLAUDE.md's standing rule from the `onezero` and `ea` incidents applies exactly
here. Guessing `linkedin.com/company/<slug-from-name>` proves a page exists, not
that it is *this* organisation's page — and an LSP page mis-attributed into the
seed list would quietly pull an unrelated org's posts into event_leads every
Friday. So this script never guesses slugs. It asks Serper (Google's index) which
LinkedIn company page ranks for the LSP's exact name, then corroborates the
returned page title against the name before marking a row `verified`.

Corroboration is deliberately strict and fails closed:
  - the county/place token must appear in the LinkedIn page title, AND
  - a sport/partnership token must appear in the title.
Anything else lands as needs_manual_review with the candidate URL recorded, so a
human has something to click rather than nothing. Expect false negatives; that is
the intended direction of the error.

The 29 LSP names and websites below were read off Sport Ireland's own LSP Contact
Finder (sportireland.ie/participation/lsp-contact-finder, 3 pages, "Displaying
1-12 of 29") on 2026-09-14 — not assembled from memory.

Usage:
    python events_pipeline/resolve_lsp_linkedin.py
    python events_pipeline/resolve_lsp_linkedin.py --only meath
    python events_pipeline/resolve_lsp_linkedin.py --out some/other.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "lsp_linkedin_resolved.csv"

# Polite gap between Serper calls. 29 calls is nothing against the free tier's
# 2,500/month, but this script is also the thing someone re-runs when a page
# moves, so it should stay well-behaved by default.
_THROTTLE_SECONDS = 1.0

# (place token used for corroboration, official name, Sport Ireland website)
# Read from the LSP Contact Finder on 2026-09-14.
LSPS: list[tuple[str, str, str]] = [
    ("carlow",      "Carlow Sports Partnership",                   "www.carlowsports.ie"),
    ("cavan",       "Cavan Sports Partnership",                    "www.cavansportspartnership.ie"),
    ("clare",       "Clare Sports Partnership",                    "www.claresports.ie"),
    ("cork",        "Cork Sports Partnership",                     "www.corksports.ie"),
    ("donegal",     "Donegal Sports Partnership",                  "www.activedonegal.com"),
    ("dublin",      "Dublin City Sport & Wellbeing Partnership",   "www.dcswphub.ie"),
    ("laoghaire",   "Dun Laoghaire Rathdown Sports Partnership",   "www.dlrsportspartnership.ie"),
    ("fingal",      "Fingal Sports Partnership",                   "www.fingal.ie"),
    ("galway",      "Galway Sports Active",                        "www.galwayactive.ie"),
    ("kerry",       "Kerry Recreation and Sports Partnership",     "(Facebook only)"),
    ("kildare",     "Kildare Sports Partnership",                  "kildarecoco.ie/kildaresp/"),
    ("kilkenny",    "Kilkenny Recreation & Sports Partnership",    "www.krsp.ie"),
    ("laois",       "Laois Sports Partnership",                    "www.laoissports.ie"),
    ("leitrim",     "Leitrim Sports Partnership",                  "www.leitrimsports.ie"),
    ("limerick",    "Limerick Sports Partnership",                 "www.limericksports.ie"),
    ("longford",    "Longford Sports",                             "www.longfordsports.ie"),
    ("louth",       "Louth Sports Partnership",                    "www.louthlsp.com"),
    ("mayo",        "Mayo Sports Partnership",                     "www.mayosports.ie"),
    ("meath",       "Meath Local Sports Partnership",              "www.meathsports.ie"),
    ("monaghan",    "Monaghan Sports Partnership",                 "www.monaghansports.ie"),
    ("offaly",      "Offaly Sports Partnership",                   "www.offalysports.ie"),
    ("roscommon",   "Roscommon Sports Partnership",                "www.rosactive.org"),
    ("sligo",       "Sligo Sport & Recreation Partnership",        "www.sligosportandrecreation.ie"),
    ("dublin",      "South Dublin County Sports Partnership",      "www.sdcsp.ie"),
    ("tipperary",   "Tipperary Sports",                            "www.tipperarysports.ie"),
    ("waterford",   "Waterford Sports Partnership",                "www.waterfordsportspartnership.ie"),
    ("westmeath",   "Westmeath Sports Partnership",                "www.westmeathsports.ie"),
    ("wexford",     "Sports Active Wexford",                       "www.sportsactivewexford.ie"),
    ("wicklow",     "Wicklow Local Sports Partnership",            "www.wicklowlsp.ie"),
]

# At least one of these must appear in the LinkedIn page title for a row to pass.
# "partnership" is deliberately NOT in this list even though it is in almost
# every LSP's name: Ireland is full of county-level LEADER, local development and
# family-resource "partnerships", so place + "partnership" alone corroborates
# nothing. It let Kilkenny LEADER Partnership through as Kilkenny Recreation &
# Sports Partnership on the first run of this script (2026-09-14) — the same
# wrong-organisation-same-slug-shape failure the module docstring is about.
_SPORT_TOKENS = ("sport", "recreation", "active", "wellbeing")

_CSV_FIELDS = [
    "lsp_name",
    "website",
    "linkedin_url",
    "result_title",
    "verdict",
    "notes",
]


class SerperError(Exception):
    """Serper call failed. Aborts the script — a partial CSV is worse than none."""


def _normalise(text: str) -> str:
    """Lowercase, fold the Irish-language accents that appear in LSP names,
    strip punctuation, collapse whitespace."""
    text = (text or "").lower()
    for accented, plain in (
        ("ú", "u"), ("í", "i"), ("á", "a"),
        ("é", "e"), ("ó", "o"),
    ):
        text = text.replace(accented, plain)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _company_url(link: str) -> str | None:
    """Reduce a LinkedIn URL to its canonical /company/<slug> form, or None."""
    try:
        parsed = urlparse(link)
    except ValueError:
        return None
    if "linkedin.com" not in (parsed.netloc or ""):
        return None
    m = re.match(r"^/company/([^/?#]+)", parsed.path or "")
    if not m:
        return None
    return f"https://www.linkedin.com/company/{m.group(1)}"


def _serper_search(query: str, api_key: str) -> list[dict]:
    """POST one query to Serper; return organic results. Raises SerperError."""
    try:
        resp = requests.post(
            _SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise SerperError(f"network error: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SerperError(f"HTTP {resp.status_code} — check SERPER_API_KEY")
    if resp.status_code == 429:
        raise SerperError("HTTP 429 — Serper free tier quota exhausted")
    if resp.status_code != 200:
        raise SerperError(f"HTTP {resp.status_code}")

    try:
        return resp.json().get("organic", []) or []
    except ValueError as exc:
        raise SerperError(f"invalid JSON: {exc}") from exc


def resolve_one(place: str, name: str, api_key: str) -> dict:
    """Resolve one LSP to a LinkedIn company page, with a corroboration verdict."""
    row = {
        "lsp_name": name,
        "website": "",
        "linkedin_url": "",
        "result_title": "",
        "verdict": "not_found",
        "notes": "",
    }

    results = _serper_search(f'site:linkedin.com/company "{name}"', api_key)

    place_norm = _normalise(place)
    first_candidate: tuple[str, str] | None = None

    for result in results:
        url = _company_url(result.get("link", ""))
        if not url:
            continue
        title = (result.get("title") or "").strip()
        title_norm = _normalise(title)

        if first_candidate is None:
            first_candidate = (url, title)

        if place_norm in title_norm and any(t in title_norm for t in _SPORT_TOKENS):
            row.update({
                "linkedin_url": url,
                "result_title": title,
                "verdict": "verified",
                "notes": f"title contains '{place}' + a sport token",
            })
            return row

    if first_candidate is not None:
        url, title = first_candidate
        row.update({
            "linkedin_url": url,
            "result_title": title,
            "verdict": "needs_manual_review",
            "notes": (
                f"top LinkedIn company result did not corroborate "
                f"(expected '{place}' + a sport token in the title)"
            ),
        })
    else:
        row["notes"] = "no linkedin.com/company result returned by Serper"

    return row


def main(only: str | None, out_path: Path) -> int:
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        log.error("SERPER_API_KEY is not set — cannot resolve LSP LinkedIn pages.")
        return 1

    targets = LSPS
    if only:
        needle = only.lower()
        targets = [t for t in LSPS if needle in t[1].lower() or needle == t[0]]
        if not targets:
            log.error("--only %r matched none of the %d LSPs", only, len(LSPS))
            return 1

    rows: list[dict] = []
    for i, (place, name, website) in enumerate(targets, start=1):
        log.info("[%d/%d] resolving %s", i, len(targets), name)
        try:
            row = resolve_one(place, name, api_key)
        except SerperError as exc:
            log.error("Serper failed on %r: %s — aborting", name, exc)
            return 1
        row["website"] = website
        rows.append(row)
        log.info("  -> %s %s", row["verdict"], row["linkedin_url"] or "(no url)")
        if i < len(targets):
            time.sleep(_THROTTLE_SECONDS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    log.info("Wrote %d rows to %s", len(rows), out_path)
    for verdict in ("verified", "needs_manual_review", "not_found"):
        log.info("  %-20s %d", verdict, counts.get(verdict, 0))
    log.info(
        "Next: review the CSV by hand, then copy confirmed rows into "
        "events_pipeline/data/linkedin_seed_authors.csv with enabled=true."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Resolve Local Sports Partnerships to LinkedIn company pages (one-off setup).",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="NAME",
        help="Resolve a single LSP by substring of its name (e.g. 'meath').",
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_OUT),
        metavar="PATH",
        help=f"Output CSV path (default: {_DEFAULT_OUT}).",
    )
    args = parser.parse_args()
    sys.exit(main(only=args.only, out_path=Path(args.out)))
