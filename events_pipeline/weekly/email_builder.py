"""email_builder.py — build the weekly events run HTML summary email."""

from datetime import datetime


def _h(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_rt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _status_cell(status: str) -> str:
    if status == "failed":
        return '<td style="padding:4px 10px;color:#cc0000;font-weight:bold;">FAILED</td>'
    return '<td style="padding:4px 10px;color:#2a7a2a;">success</td>'


def build_email(
    adapter_results: list,          # list[AdapterResult]
    extraction_results: list,       # list[ExtractionResult]
    snapshot: dict,
    run_started_at: datetime,
    total_runtime_seconds: float,
) -> str:
    ts_str   = run_started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = run_started_at.strftime("%d %b %Y")

    failed_adapters = [r for r in adapter_results if r.error]
    failed_count    = len(failed_adapters)
    status_label    = "All steps successful" if not failed_count else f"{failed_count} adapter(s) failed"
    status_color    = "#2a7a2a" if not failed_count else "#cc0000"

    # ── 1. Headline ────────────────────────────────────────────────────────────
    section1 = f"""\
<h2 style="margin-bottom:4px;">Sports D3c0d3d &mdash; Weekly Events Run ({_h(date_str)})</h2>
<table style="border-collapse:collapse;margin-bottom:8px;">
  <tr><td style="padding:3px 16px 3px 0;"><strong>Total runtime</strong></td><td>{_h(_fmt_rt(total_runtime_seconds))}</td></tr>
  <tr><td style="padding:3px 16px 3px 0;"><strong>Status</strong></td><td style="color:{status_color};font-weight:bold;">{_h(status_label)}</td></tr>
</table>"""

    # ── 2. Pipeline state ──────────────────────────────────────────────────────
    section2 = f"""\
<h3>Pipeline State</h3>
<table style="border-collapse:collapse;">
  <tr><td style="padding:3px 20px 3px 0;">Verified upcoming events</td><td><strong>{snapshot.get("verified_upcoming", 0)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Pending review</td><td><strong>{snapshot.get("pending_review", 0)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Rejected (lifetime)</td><td><strong>{snapshot.get("rejected_lifetime", 0)}</strong></td></tr>
</table>"""

    # ── 3. Adapter table ───────────────────────────────────────────────────────
    adapter_rows = ""
    for r in adapter_results:
        status = "failed" if r.error else "success"
        adapter_rows += (
            f"  <tr>"
            f"<td style='padding:4px 10px;'>{_h(r.source_name)}</td>"
            f"{_status_cell(status)}"
            f"<td style='padding:4px 10px;'>{_h(_fmt_rt(r.runtime_seconds))}</td>"
            f"<td style='padding:4px 10px;text-align:right;'>{len(r.urls_discovered)}</td>"
            f"</tr>\n"
        )

    if adapter_results:
        section3 = f"""\
<h3>Adapter Run Details</h3>
<table style="border-collapse:collapse;font-size:14px;">
  <thead>
    <tr style="background:#f0f0f0;">
      <th style="padding:6px 10px;text-align:left;">Source</th>
      <th style="padding:6px 10px;text-align:left;">Status</th>
      <th style="padding:6px 10px;text-align:left;">Runtime</th>
      <th style="padding:6px 10px;text-align:right;">URLs discovered</th>
    </tr>
  </thead>
  <tbody>
{adapter_rows}  </tbody>
</table>"""
    else:
        section3 = "<h3>Adapter Run Details</h3><p style='color:#666;'>Adapters skipped (--skip-adapters).</p>"

    # ── 4. Adapter errors ──────────────────────────────────────────────────────
    if failed_adapters:
        items = "".join(
            f"<li><strong>{_h(r.source_name)}</strong>: {_h(r.error or 'unknown')}</li>"
            for r in failed_adapters
        )
        section4 = f"<h3>Adapter Errors</h3><ul>{items}</ul>"
    else:
        section4 = ""

    # ── 5. Extraction results ──────────────────────────────────────────────────
    total_extracted  = len(extraction_results)
    relevant         = [r for r in extraction_results if r.status in ("success",)]
    skipped          = [r for r in extraction_results if r.status == "skipped_irrelevant"]
    errors           = [r for r in extraction_results if r.status == "failed"]
    inserted         = [r for r in relevant if r.was_inserted]
    updated          = [r for r in relevant if r.was_inserted is False]

    cat_counts: dict[str, int] = {}
    for r in extraction_results:
        if r.category:
            cat_counts[r.category] = cat_counts.get(r.category, 0) + 1

    cat_rows = "".join(
        f"<tr><td style='padding:2px 20px 2px 0;'>&nbsp;&nbsp;{_h(cat)}</td><td>{count}</td></tr>"
        for cat, count in sorted(cat_counts.items())
    )

    section5 = f"""\
<h3>Extraction Results</h3>
<table style="border-collapse:collapse;">
  <tr><td style="padding:3px 20px 3px 0;">URLs extracted (after dedup)</td><td><strong>{total_extracted}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Relevant (sportstech / AI / startup)</td><td><strong>{len(relevant)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Not relevant (skipped)</td><td>{len(skipped)}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Extraction errors</td><td style="{'color:#cc0000;' if errors else ''}">{len(errors)}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">New events upserted</td><td><strong>{len(inserted)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Existing events updated</td><td>{len(updated)}</td></tr>
  {f'<tr><td colspan="2" style="padding:6px 0 2px 0;font-size:13px;color:#444;"><strong>By category:</strong></td></tr>' if cat_counts else ''}
  {cat_rows}
</table>"""

    # ── 6. New events for review ───────────────────────────────────────────────
    new_events = [r for r in relevant if r.was_inserted]
    if new_events:
        event_rows = "".join(
            f"<tr>"
            f"<td style='padding:3px 10px 3px 0;font-size:13px;'>{_h(r.date or '—')}</td>"
            f"<td style='padding:3px 10px 3px 0;font-size:13px;'>{_h((r.name or '—')[:50])}</td>"
            f"<td style='padding:3px 10px 3px 0;font-size:13px;color:#666;'>{_h(r.category or '—')}</td>"
            f"<td style='padding:3px 10px 3px 0;font-size:13px;color:#666;'>{_h(r.source_name)}</td>"
            f"</tr>"
            for r in new_events
        )
        section6 = f"""\
<h3>New Events for Review ({len(new_events)})</h3>
<table style="border-collapse:collapse;">
  <thead>
    <tr style="background:#f0f0f0;">
      <th style="padding:5px 10px 5px 0;text-align:left;font-size:13px;">Date</th>
      <th style="padding:5px 10px 5px 0;text-align:left;font-size:13px;">Name</th>
      <th style="padding:5px 10px 5px 0;text-align:left;font-size:13px;">Category</th>
      <th style="padding:5px 10px 5px 0;text-align:left;font-size:13px;">Source</th>
    </tr>
  </thead>
  <tbody>{event_rows}</tbody>
</table>
<p style="margin-top:8px;">
  <a href="https://hub.sportsd3c0d3d.ie/admin/events">Review at /admin/events &rarr;</a>
</p>"""
    else:
        section6 = "<h3>New Events for Review</h3><p style='color:#666;'>No new events this run.</p>"

    # ── 7. Silent adapters ─────────────────────────────────────────────────────
    silent = [r for r in adapter_results if not r.error and len(r.urls_discovered) == 0]
    if silent:
        items = "".join(f"<li>{_h(r.source_name)}</li>" for r in silent)
        section7 = f"<h3>Sources Discovering Nothing This Run</h3><ul>{items}</ul>"
    else:
        section7 = ""

    # ── Assemble ───────────────────────────────────────────────────────────────
    divider = "\n<hr style='margin:20px 0;border:none;border-top:1px solid #ddd;'>\n"
    body = (
        f"<body style='font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:800px;'>\n"
        + section1 + divider
        + section2 + divider
        + section3
        + (divider + section4 if section4 else "")
        + divider
        + section5 + divider
        + section6
        + (divider + section7 if section7 else "")
        + divider
        + f'<p style="color:#aaa;font-size:11px;">Generated by run_weekly_events.py at {_h(ts_str)}</p>'
        + "\n</body>"
    )
    return body
