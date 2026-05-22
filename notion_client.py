import requests

NOTION_API_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"

# If your Status field is a "Select" type instead of Notion's native "Status"
# type, change "status" to "select" in the filter below.
_FILTER = {
    "and": [
        {
            "property": "Task Due Date",
            "date": {"is_empty": True},
        },
        {
            "property": "Status",
            "status": {"does_not_equal": "Done"},
        },
    ]
}


def get_tasks_missing_due_date(token: str, database_id: str) -> list[dict]:
    """Return all non-Done tasks that have no due date set."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }
    url = f"{_BASE_URL}/databases/{database_id}/query"
    payload: dict = {"filter": _FILTER}
    tasks: list[dict] = []

    while True:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        for page in data.get("results", []):
            props = page.get("properties", {})
            title_parts = props.get("Task Name", {}).get("title", [])
            name = "".join(t.get("plain_text", "") for t in title_parts) or "(Untitled)"
            tasks.append({"name": name, "url": page.get("url", "")})

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return tasks
