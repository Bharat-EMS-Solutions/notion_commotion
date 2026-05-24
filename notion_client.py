import re
import requests
from datetime import date, datetime

NOTION_API_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"
_DATE_RE = re.compile(r"Change to(\d{4}-\d{2}-\d{2})")

# Field config keys and their meaning:
#   title      – name of the title property
#   due_date   – name of the date property
#   status     – name of the status property
#   done_value – exact option string that means "Done"
#   owner      – name of the assignee/people property (or None to skip)
#   reviewer   – name of the reviewer property (or None to skip)
#   overdue    – name of the formula boolean property (or None to skip)
#   history    – name of the due-date-history rich_text property (or None to skip)

DB1_FIELDS = {
    "title":      "Task name",
    "due_date":   "Task Due date",
    "status":     "Status",
    "done_value": "Done",
    "owner":      "Owner",
    "reviewer":   "Reviewer",
    "overdue":    "Overdue",
    "history":    "Due Date History",
    "description":"Description",
    "priority":   "Priority",
    "team":       "Team",
}

DB2_FIELDS = {
    "title":      "Task name",
    "due_date":   "Due date",
    "status":     "Status",
    "done_value": "Done",
    "owner":      "Assignee",
    "reviewer":   None,
    "overdue":    "Overdue",
    "history":    "Due Date History",
    "description":"Description",
    "priority":   "Priority",
    "team":       "Team",
}


def _slippage(history_text: str) -> tuple[int, str | None, str | None]:
    """(days_slipped, original_date, latest_date). History entries are newest-first."""
    dates = _DATE_RE.findall(history_text)
    if len(dates) < 2:
        return 0, dates[0] if dates else None, dates[0] if dates else None
    try:
        delta = (
            datetime.strptime(dates[0], "%Y-%m-%d").date()
            - datetime.strptime(dates[-1], "%Y-%m-%d").date()
        )
        return delta.days, dates[-1], dates[0]
    except ValueError:
        return 0, dates[-1], dates[0]


def _rich_text(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


def _extract(page: dict, f: dict) -> dict | None:
    props = page.get("properties", {})

    title = "".join(
        t.get("plain_text", "") for t in props.get(f["title"], {}).get("title", [])
    ).strip()
    if not title or title == "(Untitled)":
        return None

    due_obj = props.get(f["due_date"], {}).get("date")
    due_date = due_obj.get("start") if due_obj else None

    status_name = props.get(f["status"], {}).get("status", {}).get("name", "") or ""

    overdue = False
    if f.get("overdue"):
        overdue = props.get(f["overdue"], {}).get("formula", {}).get("boolean", False)

    slippage_days, original_date = 0, None
    if f.get("history"):
        history_text = _rich_text(props.get(f["history"], {}))
        slippage_days, original_date, _ = _slippage(history_text)

    description = ""
    if f.get("description"):
        description = _rich_text(props.get(f["description"], {}))[:120]

    priority = ""
    if f.get("priority"):
        priority = props.get(f["priority"], {}).get("select", {}) or {}
        priority = priority.get("name", "")

    teams = []
    if f.get("team"):
        teams = [o.get("name", "") for o in props.get(f["team"], {}).get("multi_select", [])]

    has_owner = False
    if f.get("owner"):
        has_owner = bool(props.get(f["owner"], {}).get("people"))

    has_reviewer = False
    if f.get("reviewer"):
        has_reviewer = bool(props.get(f["reviewer"], {}).get("people"))

    return {
        "name":          title,
        "url":           page.get("url", ""),
        "due_date":      due_date,
        "status":        status_name,
        "description":   description,
        "priority":      priority,
        "teams":         teams,
        "has_owner":     has_owner,
        "has_reviewer":  has_reviewer,
        "overdue":       overdue,
        "slippage_days": slippage_days,
        "original_date": original_date,
    }


def get_task_report(token: str, database_id: str, fields: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    # Fetch DB title
    db_resp = requests.get(
        f"{_BASE_URL}/databases/{database_id}", headers=headers
    )
    db_resp.raise_for_status()
    db_name = "".join(
        t.get("plain_text", "") for t in db_resp.json().get("title", [])
    ) or "Notion Database"

    # Query all non-Done tasks
    payload: dict = {
        "filter": {
            "property": fields["status"],
            "status": {"does_not_equal": fields["done_value"]},
        }
    }
    url = f"{_BASE_URL}/databases/{database_id}/query"
    tasks: list[dict] = []

    while True:
        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            task = _extract(page, fields)
            if task:
                tasks.append(task)
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    report: dict = {
        "db_name":    db_name,
        "total_open": len(tasks),
        "no_due_date": [t for t in tasks if not t["due_date"]],
        "overdue":     [t for t in tasks if t["overdue"]],
        "max_slippage": sorted(
            [t for t in tasks if t["slippage_days"] > 0],
            key=lambda t: t["slippage_days"],
            reverse=True,
        )[:10],
        # None means the field doesn't exist in this DB — mailer will skip the section
        "no_owner":    [t for t in tasks if not t["has_owner"]] if fields.get("owner") else None,
        "no_reviewer": [t for t in tasks if not t["has_reviewer"]] if fields.get("reviewer") else None,
    }
    return report
