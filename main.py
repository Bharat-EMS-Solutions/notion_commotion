#!/usr/bin/env python3
"""
Entry point — query Notion for tasks missing a due date and email a reminder.

Scheduling examples:
  cron (Linux/macOS):   0 9 * * 1-5  /usr/bin/python3 /path/to/main.py
  Task Scheduler (Win): Action → python.exe  Arguments → C:\path\to\main.py
"""
import logging
import os
import sys

from dotenv import load_dotenv

from mailer import send_reminder_email
from notion_client import get_tasks_missing_due_date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_REQUIRED_ENV = [
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "SENDER_EMAIL",
    "RECIPIENT_EMAIL",
]


def main() -> None:
    load_dotenv()

    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    log.info("Querying Notion database for tasks missing a due date...")
    try:
        tasks = get_tasks_missing_due_date(
            token=os.environ["NOTION_TOKEN"],
            database_id=os.environ["NOTION_DATABASE_ID"],
        )
    except Exception as exc:
        log.error("Notion query failed: %s", exc)
        sys.exit(1)

    if not tasks:
        log.info("No tasks missing a due date — nothing to send.")
        return

    log.info("Found %d task(s) without a due date. Sending reminder...", len(tasks))
    try:
        send_reminder_email(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
            sender_email=os.environ["SENDER_EMAIL"],
            recipient_email=os.environ["RECIPIENT_EMAIL"],
            tasks=tasks,
        )
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        sys.exit(1)

    log.info("Reminder email sent successfully.")


if __name__ == "__main__":
    main()
