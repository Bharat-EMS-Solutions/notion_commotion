from datetime import date

import msal
import requests

_GRAPH_SEND_MAIL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
_SCOPES = ["https://graph.microsoft.com/.default"]

# All possible sections: (report_key, label, accent_color, description)
# Sections whose report value is None are skipped automatically.
_ALL_SECTIONS = [
    ("no_due_date",  "No Due Date",      "#f59e0b", "Tasks with no due date assigned"),
    ("no_owner",     "No Owner",         "#8b5cf6", "Tasks with no owner assigned"),
    ("no_reviewer",  "No Reviewer",      "#3b82f6", "Tasks with no reviewer assigned"),
    ("overdue",      "Overdue",          "#ef4444", "Past due date and not yet Done"),
    ("max_slippage", "Maximum Slippage", "#dc2626", "Top tasks ranked by days slipped"),
]

_PRIORITY_COLORS = {
    "High":   "#ef4444",
    "Medium": "#f59e0b",
    "Low":    "#10b981",
}


def _acquire_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(
            f"Token acquisition failed: {result.get('error_description', result.get('error'))}"
        )
    return result["access_token"]


def _badge(text: str, color: str, small: bool = False) -> str:
    size = "11px" if small else "12px"
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'background:{color};color:#fff;font-size:{size};font-weight:700;'
        f'white-space:nowrap;">{text}</span>'
    )


def _task_row(task: dict, color: str, show_slippage: bool = False) -> str:
    # --- Meta chips (status, priority, teams) ---
    chips = []
    if task.get("status"):
        chips.append(_badge(task["status"], "#6b7280", small=True))
    if task.get("priority"):
        p_color = _PRIORITY_COLORS.get(task["priority"], "#9ca3af")
        chips.append(_badge(task["priority"], p_color, small=True))
    if task.get("teams"):
        for team in task["teams"][:2]:
            chips.append(_badge(team, "#0ea5e9", small=True))
    chips_html = (
        '<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap;">'
        + " ".join(f'<span style="margin-right:4px;">{c}</span>' for c in chips)
        + "</div>"
        if chips else ""
    )

    # --- Description ---
    desc_html = ""
    if task.get("description"):
        desc_html = (
            f'<div style="font-size:11px;color:#6b7280;margin-top:3px;'
            f'white-space:normal;">{task["description"]}'
            f'{"…" if len(task["description"]) >= 120 else ""}</div>'
        )

    # --- Slippage note ---
    slip_html = ""
    if show_slippage and task.get("slippage_days"):
        slip_html = (
            f'<div style="font-size:11px;color:#dc2626;margin-top:3px;">'
            f'Originally: {task.get("original_date","?")} '
            f'&#8594; slipped <strong>{task["slippage_days"]} day(s)</strong></div>'
        )

    # --- Due date cell ---
    due = task.get("due_date") or ""
    due_style = "font-size:12px;color:#6b7280;white-space:nowrap;padding:8px 12px;vertical-align:top;"
    due_cell = f'<td style="{due_style}">{due if due else "—"}</td>'

    return (
        f'<tr style="border-bottom:1px solid #f3f4f6;">'
        f'<td style="padding:10px 12px;border-left:3px solid {color};vertical-align:top;">'
        f'<a href="{task["url"]}" style="color:#111827;text-decoration:none;'
        f'font-size:13px;font-weight:600;">{task["name"]}</a>'
        f'{chips_html}{desc_html}{slip_html}'
        f'</td>'
        f'{due_cell}'
        f'</tr>'
    )


def _section_card(key: str, label: str, color: str, desc: str, tasks: list[dict]) -> str:
    if not tasks:
        body = (
            '<tr><td colspan="2" style="padding:16px 12px;color:#9ca3af;font-size:13px;">'
            '&#10003;&nbsp;All clear</td></tr>'
        )
    else:
        body = "\n".join(_task_row(t, color, show_slippage=(key == "max_slippage")) for t in tasks)

    count_color = color if tasks else "#10b981"
    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:24px;border-radius:8px;border:1px solid #e5e7eb;overflow:hidden;">
  <tr style="background:#f9fafb;">
    <td style="padding:12px 16px;">
      <span style="font-size:14px;font-weight:700;color:#111827;">{label}</span>
      <span style="font-size:12px;color:#9ca3af;margin-left:8px;">{desc}</span>
    </td>
    <td style="padding:12px 16px;text-align:right;white-space:nowrap;">
      {_badge(str(len(tasks)), count_color)}
    </td>
  </tr>
  <tr><td colspan="2" style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0">{body}</table>
  </td></tr>
</table>"""


def _build_html(report: dict) -> str:
    today_str = date.today().strftime("%B %d, %Y")
    db_name   = report.get("db_name", "Notion")
    total     = report["total_open"]

    active_sections = [
        (key, label, color, desc)
        for key, label, color, desc in _ALL_SECTIONS
        if report.get(key) is not None
    ]

    stats = "".join(
        f'<td style="text-align:center;padding:14px 16px;">'
        f'<div style="font-size:26px;font-weight:800;color:{color};">'
        f'{len(report[key])}</div>'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">{label}</div>'
        f'</td>'
        for key, label, color, _ in active_sections
    )

    sections_html = "".join(
        _section_card(key, label, color, desc, report[key])
        for key, label, color, desc in active_sections
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 0;">
<tr><td align="center">
<table width="660" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:12px;overflow:hidden;
              box-shadow:0 1px 4px rgba(0,0,0,.08);">

  <tr>
    <td style="background:#111827;padding:28px 32px;">
      <div style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;
                  letter-spacing:.08em;margin-bottom:6px;">Task Health Report</div>
      <div style="font-size:22px;font-weight:800;color:#fff;">{db_name}</div>
      <div style="font-size:13px;color:#9ca3af;margin-top:6px;">
        {today_str}&nbsp;&nbsp;&#183;&nbsp;&nbsp;{total} open tasks
      </div>
    </td>
  </tr>

  <tr>
    <td style="padding:0;border-bottom:2px solid #e5e7eb;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>{stats}</tr>
      </table>
    </td>
  </tr>

  <tr>
    <td style="padding:24px 24px 8px;">{sections_html}</td>
  </tr>

  <tr>
    <td style="padding:16px 24px 28px;text-align:center;font-size:11px;color:#9ca3af;">
      Auto-generated from Notion &mdash; excludes tasks marked <em>Done</em>
      and blank/untitled entries.
    </td>
  </tr>

</table>
</td></tr>
</table>
</body></html>"""


def send_reminder_email(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    sender_email: str,
    recipient_email: str,
    report: dict,
) -> None:
    token = _acquire_token(tenant_id, client_id, client_secret)
    active_keys = [k for k, *_ in _ALL_SECTIONS if report.get(k) is not None]
    total_issues = sum(len(report[k]) for k in active_keys)
    db_name = report.get("db_name", "Notion")
    payload = {
        "message": {
            "subject": (
                f"[{db_name}] Task Health Report — "
                f"{total_issues} item(s) need attention "
                f"({date.today().strftime('%d %b %Y')})"
            ),
            "body": {"contentType": "HTML", "content": _build_html(report)},
            "toRecipients": [{"emailAddress": {"address": recipient_email}}],
        }
    }
    resp = requests.post(
        _GRAPH_SEND_MAIL.format(sender=sender_email),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
