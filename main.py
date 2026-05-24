#!/usr/bin/env python3
"""
Entry point — load database configs from databases.json, query each one,
and email a per-DB task health report via Microsoft 365.

To add a database:
  1. Add its ID to .env:            NOTION_DATABASE_3_ID=<id>
  2. Add an entry to databases.json with "env_var": "NOTION_DATABASE_3_ID"
     and the correct field name mappings for that database.
  No Python changes needed.

Scheduling examples:
  cron (Linux/macOS):   0 9 * * 1-5  /usr/bin/python3 /path/to/main.py
  Task Scheduler (Win): Action -> python.exe  Arguments -> C:/path/to/main.py
"""
import json
import logging
import os
import sys
from pathlib import Path

_CONFIG_FILE   = Path(__file__).parent / "config.json"

from dotenv import load_dotenv

from mailer import send_reminder_email
from notion_client import get_task_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_REQUIRED_ENV = [
    "NOTION_TOKEN",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
]

_DB_CONFIG_FILE = Path(__file__).parent / "databases.json"


def load_app_config() -> dict:
    """Read config.json and return its contents."""
    if not _CONFIG_FILE.exists():
        log.error("config.json not found — copy config.json.example to config.json and fill it in")
        sys.exit(1)
    try:
        cfg = json.loads(_CONFIG_FILE.read_text())
    except json.JSONDecodeError as exc:
        log.error("config.json is not valid JSON: %s", exc)
        sys.exit(1)
    required = ["sender_email", "recipient_emails"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        log.error("config.json is missing required keys: %s", ", ".join(missing))
        sys.exit(1)
    return cfg


def load_databases() -> list[tuple[str, dict, list[str]]]:
    """
    Read databases.json and resolve each entry's env_var to an actual database ID.
    Entries whose env var is missing are skipped with a warning.
    Returns [(database_id, fields_dict, recipient_emails), ...]
    recipient_emails is the DB-level override list (may be empty — caller falls back to global).
    """
    if not _DB_CONFIG_FILE.exists():
        log.error("databases.json not found at %s", _DB_CONFIG_FILE)
        sys.exit(1)

    try:
        entries = json.loads(_DB_CONFIG_FILE.read_text())
    except json.JSONDecodeError as exc:
        log.error("databases.json is not valid JSON: %s", exc)
        sys.exit(1)

    databases = []
    for entry in entries:
        env_var = entry.get("env_var", "")
        db_id   = os.getenv(env_var)
        if not db_id:
            log.warning("Skipping entry — env var %r is not set", env_var)
            continue
        fields     = entry.get("fields", {})
        recipients = entry.get("recipient_emails", [])
        databases.append((db_id, fields, recipients))

    if not databases:
        log.error("No databases resolved. Check databases.json and .env.")
        sys.exit(1)

    return databases


def main() -> None:
    load_dotenv()

    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    cfg = load_app_config()
    databases = load_databases()
    log.info("Loaded %d database(s) from databases.json", len(databases))

    mail_kwargs = dict(
        tenant_id        = os.environ["AZURE_TENANT_ID"],
        client_id        = os.environ["AZURE_CLIENT_ID"],
        client_secret    = os.environ["AZURE_CLIENT_SECRET"],
        sender_email     = cfg["sender_email"],
        recipient_emails = cfg["recipient_emails"],
    )

    failed = False
    for db_id, fields, db_recipients in databases:
        log.info("Querying %s...", db_id)
        try:
            report = get_task_report(
                token=os.environ["NOTION_TOKEN"],
                database_id=db_id,
                fields=fields,
            )
        except Exception as exc:
            log.error("Notion query failed for %s: %s", db_id, exc)
            failed = True
            continue

        log.info(
            "[%s] %d open tasks | no_due_date=%d no_owner=%s no_reviewer=%s overdue=%d slippage=%d",
            report["db_name"],
            report["total_open"],
            len(report["no_due_date"]),
            len(report["no_owner"])    if report["no_owner"]    is not None else "n/a",
            len(report["no_reviewer"]) if report["no_reviewer"] is not None else "n/a",
            len(report["overdue"]),
            len(report["max_slippage"]),
        )

        recipients = db_recipients or mail_kwargs["recipient_emails"]
        log.info("[%s] Sending to %d recipient(s)...", report["db_name"], len(recipients))
        try:
            send_reminder_email(**{**mail_kwargs, "recipient_emails": recipients}, report=report)
            log.info("[%s] Email sent.", report["db_name"])
        except Exception as exc:
            log.error("Failed to send email for %s: %s", report["db_name"], exc)
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
