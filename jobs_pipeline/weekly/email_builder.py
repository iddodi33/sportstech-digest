"""email_builder.py — build the weekly jobs run HTML summary email."""

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
    if status == "credit_exhausted":
        return '<td style="padding:4px 10px;color:#b85c00;font-weight:bold;">CREDIT EXHAUSTED</td>'
    return '<td style="padding:4px 10px;color:#2a7a2a;">success</td>'


def build_email(
    adapter_results: list[dict],
    classifier_result: dict,
    sweep_result: dict,
    snapshot: dict,
    run_started_at: datetime,
    total_runtime_seconds: float,
) -> str:
    ts_str  = run_started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = run_started_at.strftime("%d %b %Y")

    failed_adapters = [r for r in adapter_results if r["status"] == "failed"]
    credit_exhausted = classifier_result["status"] == "credit_exhausted"
    failed_steps = (
        len(failed_adapters)
        + (1 if classifier_result["status"] == "failed" else 0)
        + (1 if sweep_result["status"] == "failed" else 0)
    )

    if credit_exhausted:
        status_label = "Classifier credit exhausted — partial run"
        status_color = "#b85c00"
    elif failed_steps:
        status_label = f"{failed_steps} step(s) failed"
        status_color = "#cc0000"
    else:
        status_label = "All steps successful"
        status_color = "#2a7a2a"

    # ── 1. Headline ────────────────────────────────────────────────────────────
    section1 = f"""\
<h2 style="margin-bottom:4px;">Sports D3c0d3d &mdash; Weekly Jobs Run ({_h(date_str)})</h2>
<table style="border-collapse:collapse;margin-bottom:8px;">
  <tr>
    <td style="padding:3px 16px 3px 0;"><strong>Total runtime</strong></td>
    <td>{_h(_fmt_rt(total_runtime_seconds))}</td>
  </tr>
  <tr>
    <td style="padding:3px 16px 3px 0;"><strong>Status</strong></td>
    <td style="color:{status_color};font-weight:bold;">{_h(status_label)}</td>
  </tr>
</table>"""

    # ── 2. Pipeline state ──────────────────────────────────────────────────────
    never = snapshot.get("sources_never_scraped", [])
    if never:
        never_str = f"{len(never)}: " + ", ".join(_h(n) for n in never[:12])
        if len(never) > 12:
            never_str += f" (+ {len(never) - 12} more)"
    else:
        never_str = "none"

    section2 = f"""\
<h3>Pipeline State</h3>
<table style="border-collapse:collapse;">
  <tr><td style="padding:3px 20px 3px 0;">Approved jobs</td><td><strong>{snapshot.get("approved_jobs", 0)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Pending review</td><td><strong>{snapshot.get("pending_jobs", 0)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Archived (lifetime)</td><td><strong>{snapshot.get("archived_jobs", 0)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Pending with no job_function</td><td><strong>{snapshot.get("pending_null_function", 0)}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Sources never scraped</td><td>{never_str}</td></tr>
</table>"""

    # ── 3. Adapter table ───────────────────────────────────────────────────────
    if not adapter_results:
        section3 = "<h3>Adapter Run Details</h3><p style=\"color:#666;\">Adapters skipped (--skip-adapters).</p>"
    else:
        rows = ""
        for r in adapter_results:
            err_style = "color:#cc0000;" if r["companies_with_errors"] > 0 else ""
            rows += (
                f"  <tr>"
                f"<td style='padding:4px 10px;'>{_h(r['step_name'])}</td>"
                f"{_status_cell(r['status'])}"
                f"<td style='padding:4px 10px;'>{_h(_fmt_rt(r['runtime_seconds']))}</td>"
                f"<td style='padding:4px 10px;text-align:right;'>{r['jobs_scraped']}</td>"
                f"<td style='padding:4px 10px;text-align:right;'>{r['jobs_new']}</td>"
                f"<td style='padding:4px 10px;text-align:right;'>{r['jobs_updated']}</td>"
                f"<td style='padding:4px 10px;text-align:right;'>{r['companies_processed']}</td>"
                f"<td style='padding:4px 10px;text-align:right;{err_style}'>{r['companies_with_errors']}</td>"
                f"</tr>\n"
            )
        section3 = f"""\
<h3>Adapter Run Details</h3>
<table style="border-collapse:collapse;font-size:14px;">
  <thead>
    <tr style="background:#f0f0f0;">
      <th style="padding:6px 10px;text-align:left;">Adapter</th>
      <th style="padding:6px 10px;text-align:left;">Status</th>
      <th style="padding:6px 10px;text-align:left;">Runtime</th>
      <th style="padding:6px 10px;text-align:right;">Scraped</th>
      <th style="padding:6px 10px;text-align:right;">New</th>
      <th style="padding:6px 10px;text-align:right;">Updated</th>
      <th style="padding:6px 10px;text-align:right;">Companies</th>
      <th style="padding:6px 10px;text-align:right;">Errors</th>
    </tr>
  </thead>
  <tbody>
{rows}  </tbody>
</table>"""

    # ── 4. Adapter errors ──────────────────────────────────────────────────────
    if failed_adapters:
        items = "".join(
            f"<li><strong>{_h(r['step_name'])}</strong>: {_h(r['error_message'] or 'unknown')}</li>"
            for r in failed_adapters
        )
        section4 = f"<h3>Adapter Errors</h3><ul>{items}</ul>"
    else:
        section4 = ""

    # ── 5. Classifier ──────────────────────────────────────────────────────────
    c = classifier_result
    by_reason = c.get("rejected_by_reason") or {}

    credit_banner = ""
    if c["status"] == "credit_exhausted":
        credit_banner = (
            '<p style="color:#cc0000;font-weight:bold;border:1px solid #cc0000;'
            'padding:8px;border-radius:4px;">'
            f"&#9888; Classifier ran out of Anthropic credits after processing "
            f"{c['jobs_processed']} job(s). Remaining pending jobs will carry over to next week. "
            'Check <a href="https://console.anthropic.com/settings/billing">Anthropic billing</a>.'
            "</p>"
        )
    elif c["status"] == "failed" and c.get("error_message"):
        credit_banner = f'<p style="color:#cc0000;">Error: {_h(c["error_message"])}</p>'

    null_fn_note = ""
    null_fn = c.get("jobs_with_null_function")
    if null_fn is not None and null_fn > 0:
        null_fn_note = (
            f'<p style="color:#888;font-size:13px;">'
            f"{null_fn} pending job(s) currently have no job_function — review in admin panel."
            f"</p>"
        )

    section5 = f"""\
<h3>Classifier</h3>
{credit_banner}
<table style="border-collapse:collapse;">
  <tr><td style="padding:3px 20px 3px 0;">Status</td><td>{_h(c["status"])}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Runtime</td><td>{_h(_fmt_rt(c["runtime_seconds"]))}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Jobs processed</td><td>{c["jobs_processed"]}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Passed (pending, awaiting review)</td><td>{c["approved"]}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Rejected (total)</td><td>{c["rejected_total"]}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">&nbsp;&nbsp;&mdash; too_junior</td><td>{by_reason.get("too_junior", 0)}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">&nbsp;&nbsp;&mdash; fdi_geography</td><td>{by_reason.get("fdi_geography", 0)}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">&nbsp;&nbsp;&mdash; not_sportstech</td><td>{by_reason.get("not_sportstech", 0)}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Haiku errors (skipped, retry next week)</td><td>{by_reason.get("haiku_errors", 0)}</td></tr>
</table>
{null_fn_note}"""

    # ── 6. Archive sweep ───────────────────────────────────────────────────────
    sw = sweep_result
    breakdown = sw.get("breakdown_by_source") or {}

    sweep_error = ""
    if sw["status"] == "failed" and sw.get("error_message"):
        sweep_error = f'<p style="color:#cc0000;">Error: {_h(sw["error_message"])}</p>'

    breakdown_html = ""
    if breakdown:
        brows = "".join(
            f"<tr>"
            f"<td style='padding:2px 16px 2px 0;'>{_h(name)}</td>"
            f"<td style='text-align:right;'>{count}</td>"
            f"</tr>"
            for name, count in sorted(breakdown.items(), key=lambda x: -x[1])
        )
        breakdown_html = (
            "<p style='margin:8px 0 2px;font-size:13px;color:#444;'>"
            "<strong>Breakdown by company:</strong></p>"
            f"<table style='border-collapse:collapse;font-size:13px;'>{brows}</table>"
        )

    section6 = f"""\
<h3>Archive Sweep</h3>
{sweep_error}
<table style="border-collapse:collapse;">
  <tr><td style="padding:3px 20px 3px 0;">Status</td><td>{_h(sw["status"])}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Runtime</td><td>{_h(_fmt_rt(sw["runtime_seconds"]))}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Archived this run</td><td><strong>{sw["jobs_archived"]}</strong></td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Skipped (no scrape history)</td><td>{sw["skipped_no_history"]}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Skipped (source health gate)</td><td>{sw["skipped_health_gate"]}</td></tr>
  <tr><td style="padding:3px 20px 3px 0;">Skipped (not stale yet)</td><td>{sw["skipped_not_stale"]}</td></tr>
</table>
{breakdown_html}"""

    # ── 7. Companies needing attention ─────────────────────────────────────────
    error_adapters = [
        (r["step_name"], r["companies_with_errors"])
        for r in adapter_results
        if r["companies_with_errors"] > 0
    ]
    if error_adapters:
        items = "".join(
            f"<li><strong>{_h(name)}</strong>: {count} source(s) with per-job errors</li>"
            for name, count in sorted(error_adapters, key=lambda x: -x[1])
        )
        section7 = f"<h3>Companies Needing Attention</h3><ul>{items}</ul>"
    else:
        section7 = (
            "<h3>Companies Needing Attention</h3>"
            '<p style="color:#666;">No per-job errors across all adapters this run.</p>'
        )

    # ── Assemble ───────────────────────────────────────────────────────────────
    divider = "\n<hr style='margin:20px 0;border:none;border-top:1px solid #ddd;'>\n"
    body = (
        f"<body style='font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:860px;'>\n"
        + section1 + divider
        + section2 + divider
        + section3
        + (divider + section4 if section4 else "")
        + divider
        + section5 + divider
        + section6 + divider
        + section7 + divider
        + f'<p style="color:#aaa;font-size:11px;">Generated by run_weekly.py at {_h(ts_str)}</p>'
        + "\n</body>"
    )
    return body
