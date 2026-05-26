"""
Daily owner digest — entry point for the scheduled job.

Queries all configured Notion databases for in-progress tasks, groups them
by owner email, and sends each owner a personalized email with an Actionable
Message card so they can log their hours directly from Outlook.

Usage:
    python daily_digest.py            # dry-run: prints owners + task counts
    python daily_digest.py --send     # actually sends emails via Graph API
"""
import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from mailer import send_owner_daily_digests, build_owner_digest_email
from notion_client import get_in_progress_tasks_by_owner

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

_DB_CONFIG_FILE  = Path(__file__).parent / "databases.json"
_APP_CONFIG_FILE = Path(__file__).parent / "config.json"


def _load_owner_map(token: str) -> dict[str, dict]:
    """Merge in-progress tasks across all configured databases, keyed by owner email."""
    entries = json.loads(_DB_CONFIG_FILE.read_text())
    merged: dict[str, dict] = {}

    import requests as _req
    for entry in entries:
        db_id = os.getenv(entry.get("env_var", ""))
        if not db_id:
            continue
        fields  = entry.get("fields", {})
        db_name = entry.get("env_var", "DB")

        # Resolve friendly DB name from Notion
        try:
            r = _req.get(
                f"https://api.notion.com/v1/databases/{db_id}",
                headers={"Authorization": f"Bearer {token}",
                         "Notion-Version": "2022-06-28"},
                timeout=10,
            )
            if r.ok:
                db_name = "".join(
                    t.get("plain_text", "") for t in r.json().get("title", [])
                ) or db_name
        except Exception:
            pass

        try:
            by_owner = get_in_progress_tasks_by_owner(token, db_id, fields, db_name)
            log.info("[%s] %d owner(s) with in-progress tasks", db_name, len(by_owner))
        except Exception as exc:
            log.error("[%s] Query failed: %s", db_name, exc)
            continue

        for email, info in by_owner.items():
            if email not in merged:
                merged[email] = {"owner_name": info["owner_name"], "tasks": []}
            merged[email]["tasks"].extend(info["tasks"])

    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Send daily hours digest to task owners.")
    parser.add_argument("--send", action="store_true", help="Actually send emails (default: dry-run)")
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        log.error("NOTION_TOKEN not set.")
        return 1

    owner_map = _load_owner_map(token)
    if not owner_map:
        log.info("No in-progress tasks with owner emails found. Nothing to send.")
        return 0

    today_str    = date.today().strftime("%d %b %Y")
    app_base_url = os.environ.get("APP_BASE_URL", "http://localhost:5000")

    log.info("Found %d owner(s) to notify:", len(owner_map))
    for email, info in owner_map.items():
        log.info("  %s (%s) — %d task(s)", info["owner_name"], email, len(info["tasks"]))

    if not args.send:
        log.info("Dry-run complete. Pass --send to send emails.")
        return 0

    # Verify Azure credentials
    missing = [k for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
               if not os.environ.get(k)]
    if missing:
        log.error("Missing env vars for email: %s", ", ".join(missing))
        return 1

    try:
        cfg = json.loads(_APP_CONFIG_FILE.read_text())
    except Exception as exc:
        log.error("config.json error: %s", exc)
        return 1

    errors = send_owner_daily_digests(
        tenant_id     = os.environ["AZURE_TENANT_ID"],
        client_id     = os.environ["AZURE_CLIENT_ID"],
        client_secret = os.environ["AZURE_CLIENT_SECRET"],
        sender_email  = cfg["sender_email"],
        owner_map     = owner_map,
        app_base_url  = app_base_url,
        originator_id = os.environ.get("ACTIONABLE_MSG_ORIGINATOR", ""),
    )

    if errors:
        for e in errors:
            log.error("Send failed: %s", e)
        return 1

    log.info("All %d digest(s) sent successfully.", len(owner_map))
    return 0


if __name__ == "__main__":
    sys.exit(main())
