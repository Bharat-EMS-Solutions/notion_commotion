import msal
import requests

_GRAPH_SEND_MAIL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
_SCOPES = ["https://graph.microsoft.com/.default"]


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


def _build_html(tasks: list[dict]) -> str:
    rows = "\n".join(
        f'  <tr><td style="padding:8px 16px;border-bottom:1px solid #e5e7eb;">'
        f'<a href="{t["url"]}" style="color:#2563eb;text-decoration:none;">{t["name"]}</a>'
        f"</td></tr>"
        for t in tasks
    )
    return f"""<html><body style="font-family:sans-serif;font-size:14px;color:#111;">
<p>Hi,</p>
<p>The following <strong>{len(tasks)} task(s)</strong> are missing a due date
and have not been marked as Done. Please add a due date to each one.</p>
<table style="border-collapse:collapse;min-width:400px;">
  <thead>
    <tr>
      <th style="padding:8px 16px;text-align:left;background:#f3f4f6;
                 border-bottom:2px solid #d1d5db;">Task</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
<p style="margin-top:16px;">You can open each task directly by clicking its name above.</p>
</body></html>"""


def send_reminder_email(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    sender_email: str,
    recipient_email: str,
    tasks: list[dict],
) -> None:
    token = _acquire_token(tenant_id, client_id, client_secret)
    payload = {
        "message": {
            "subject": f"[Action Required] {len(tasks)} task(s) missing a due date",
            "body": {"contentType": "HTML", "content": _build_html(tasks)},
            "toRecipients": [{"emailAddress": {"address": recipient_email}}],
        }
    }
    url = _GRAPH_SEND_MAIL.format(sender=sender_email)
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
