#!/usr/bin/env python3
"""
Entry point — query Notion databases and email per-DB task health reports.

Scheduling examples:
  cron (Linux/macOS):   0 9 * * 1-5  /usr/bin/python3 /path/to/main.py
  Task Scheduler (Win): Action -> python.exe  Arguments -> C:/path/to/main.py
"""
import logging
import os
import sys

from dotenv import load_dotenv

from mailer import send_reminder_email
from notion_client import DB1_FIELDS, DB2_FIELDS, get_task_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_REQUIRED_ENV = [
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "NOTION_DATABASE_2_ID",
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

    databases = [
        (os.environ["NOTION_DATABASE_ID"],   DB1_FIELDS),
        (os.environ["NOTION_DATABASE_2_ID"], DB2_FIELDS),
    ]

    mail_kwargs = dict(
        tenant_id      = os.environ["AZURE_TENANT_ID"],
        client_id      = os.environ["AZURE_CLIENT_ID"],
        client_secret  = os.environ["AZURE_CLIENT_SECRET"],
        sender_email   = os.environ["SENDER_EMAIL"],
        recipient_email= os.environ["RECIPIENT_EMAIL"],
    )

    failed = False
    for db_id, fields in databases:
        log.info("Querying database %s...", db_id)
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
            len(report["no_owner"]) if report["no_owner"] is not None else "n/a",
            len(report["no_reviewer"]) if report["no_reviewer"] is not None else "n/a",
            len(report["overdue"]),
            len(report["max_slippage"]),
        )

        try:
            send_reminder_email(**mail_kwargs, report=report)
            log.info("[%s] Email sent.", report["db_name"])
        except Exception as exc:
            log.error("Failed to send email for %s: %s", report["db_name"], exc)
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
