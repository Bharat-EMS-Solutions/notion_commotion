import re
import requests
from datetime import date, datetime

NOTION_API_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"
_DATE_RE = re.compile(r"Change to(\d{4}-\d{2}-\d{2})")

# Field config keys:
#   title       – title property name
#   due_date    – date property name
#   status      – status property name
#   done_value  – exact "done" option string
#   owner       – assignee people property (or None to skip section)
#   reviewer    – reviewer people property (or None to skip section)
#   overdue     – formula boolean property (or None to skip)
#   history     – due-date-history rich_text property (or None to skip)
#   description – rich_text description property (or None to skip)
#   priority    – select priority property (or None to skip)
#   team        – multi_select team property (or None to skip)
#   project     – relation property linking to a project page (or None to skip)
#   parent_task – relation property linking to a parent task page (or None to skip)

# Field configs have moved to databases.json.
# get_task_report() accepts a fields dict loaded from there.


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


def _fetch_page_refs(ids: set, headers: dict) -> dict:
    """Fetch {page_id: {name, url}} for a set of page IDs. Silently skips failures."""
    refs: dict = {}
    for pid in ids:
        try:
            resp = requests.get(f"{_BASE_URL}/pages/{pid}", headers=headers, timeout=10)
            if not resp.ok:
                continue
            page = resp.json()
            props = page.get("properties", {})
            title = ""
            for prop in props.values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                    break
            refs[pid] = {"name": title.strip() or "(Untitled)", "url": page.get("url", "")}
        except Exception:
            pass
    return refs


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
        slippage_days, original_date, _ = _slippage(_rich_text(props.get(f["history"], {})))

    description = ""
    if f.get("description"):
        description = _rich_text(props.get(f["description"], {}))[:120]

    priority = ""
    if f.get("priority"):
        priority = (props.get(f["priority"], {}).get("select") or {}).get("name", "")

    teams = []
    if f.get("team"):
        teams = [o.get("name", "") for o in props.get(f["team"], {}).get("multi_select", [])]

    has_owner = False
    if f.get("owner"):
        has_owner = bool(props.get(f["owner"], {}).get("people"))

    has_reviewer = False
    if f.get("reviewer"):
        has_reviewer = bool(props.get(f["reviewer"], {}).get("people"))

    # Store raw IDs for later enrichment — take first relation only
    project_rel = props.get(f.get("project", "") or "", {}).get("relation", [])
    parent_rel  = props.get(f.get("parent_task", "") or "", {}).get("relation", [])

    return {
        "name":           title,
        "url":            page.get("url", ""),
        "due_date":       due_date,
        "status":         status_name,
        "description":    description,
        "priority":       priority,
        "teams":          teams,
        "has_owner":      has_owner,
        "has_reviewer":   has_reviewer,
        "overdue":        overdue,
        "slippage_days":  slippage_days,
        "original_date":  original_date,
        # Temporary IDs — replaced with {name, url} dicts after ref fetch
        "_project_id":    project_rel[0]["id"] if project_rel else None,
        "_parent_id":     parent_rel[0]["id"]  if parent_rel  else None,
    }


def get_task_report(token: str, database_id: str, fields: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    # Fetch DB title
    db_resp = requests.get(f"{_BASE_URL}/databases/{database_id}", headers=headers)
    db_resp.raise_for_status()
    db_name = "".join(
        t.get("plain_text", "") for t in db_resp.json().get("title", [])
    ) or "Notion Database"

    # Query all non-Done tasks (paginated)
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

    # Fetch project and parent task names/URLs (deduplicated)
    relation_ids = {
        t[k] for t in tasks for k in ("_project_id", "_parent_id") if t.get(k)
    }
    refs = _fetch_page_refs(relation_ids, headers) if relation_ids else {}

    # Enrich tasks and remove temporary ID keys
    for t in tasks:
        pid = t.pop("_project_id", None)
        prid = t.pop("_parent_id", None)
        t["project"]     = refs.get(pid)     if pid  else None
        t["parent_task"] = refs.get(prid)    if prid else None

    return {
        "db_name":    db_name,
        "total_open": len(tasks),
        "no_due_date":  [t for t in tasks if not t["due_date"]],
        "overdue":      [t for t in tasks if t["overdue"]],
        "max_slippage": sorted(
            [t for t in tasks if t["slippage_days"] > 0],
            key=lambda t: t["slippage_days"],
            reverse=True,
        )[:10],
        # None = field absent from this DB → section omitted in email
        "no_owner":    [t for t in tasks if not t["has_owner"]]    if fields.get("owner")    else None,
        "no_reviewer": [t for t in tasks if not t["has_reviewer"]] if fields.get("reviewer") else None,
    }
