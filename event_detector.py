"""
event_detector.py — Query Notion and produce typed MIS events via StateCache diff.

Three modes, each called by mis_runner.py:

    detect_changes(token, db_entries, cache)
        → queries all non-done tasks per DB, diffs against cache
        → returns {db_env_var: [TaskCreated | AssigneeChanged | StatusStarted events]}

    detect_due_soon(cache, days=2)
        → reads the local cache only (no Notion call)
        → returns rows for tasks due in {days} days not yet notified

    detect_slippage(token, db_entries, min_days=1)
        → queries all non-done tasks, filters by slippage_days >= min_days
        → returns flat sorted list for the slippage digest
"""
import logging

log = logging.getLogger(__name__)


def detect_changes(token: str, db_entries: list, cache) -> dict:
    """
    For each configured DB:
      1. Fetch all non-done tasks from Notion (via get_all_tasks_for_mis).
      2. If this is the first run for that DB → snapshot silently, emit NO events.
      3. Otherwise → diff against cache and return typed events.

    Returns:
        {db_env_var: [event_dict, ...]}
    where each event_dict is {"type": str, "task": dict}
    """
    from notion_client import get_all_tasks_for_mis

    result: dict = {}

    for entry in db_entries:
        ev_var     = entry["env_var"]
        db_id      = entry["db_id"]
        fields     = entry["fields"]
        db_name    = entry["db_name"]
        in_prog    = fields.get("in_progress_value", "In progress")

        log.info("[%s] Querying Notion for all non-done tasks...", ev_var)
        try:
            tasks = get_all_tasks_for_mis(token, db_id, fields, db_name)
        except Exception as exc:
            log.error("[%s] Notion query failed: %s", ev_var, exc)
            result[ev_var] = []
            continue

        log.info("[%s] %d task(s) fetched.", ev_var, len(tasks))

        if cache.is_first_run(ev_var):
            log.info(
                "[%s] First run — silently snapshotting %d task(s). "
                "No emails sent this round.", ev_var, len(tasks),
            )
            cache.snapshot_silent(ev_var, tasks, in_progress_value=in_prog)
            result[ev_var] = []
        else:
            events = cache.diff(ev_var, tasks, in_progress_value=in_prog)
            result[ev_var] = events

    return result


def detect_due_soon(cache, days: int = 2) -> list:
    """
    Read the local cache (no Notion call) and return rows for tasks
    whose due_date == today + {days} days and that have not yet
    received a DueSoon notification for that date.

    Returns:
        [{"page_id": str, "db_env_var": str, "due_date": str, ...}, ...]
    """
    rows = cache.due_soon_tasks(days=days)
    log.info("[DueSoon] %d task(s) due in %d day(s) need notification.", len(rows), days)
    return rows


def detect_slippage(token: str, db_entries: list, min_days: int = 1) -> list:
    """
    Fetch all non-done tasks across all DBs and return those whose
    slippage_days >= min_days, sorted by slippage_days descending.

    Each returned task dict has an extra "owner_name" key (comma-joined
    assignee names) ready for use in the slippage digest table.

    Returns:
        [task_dict, ...]   sorted by slippage_days desc
    """
    from notion_client import get_all_tasks_for_mis

    slipped: list = []

    for entry in db_entries:
        log.info("[%s] Querying Notion for slippage...", entry["env_var"])
        try:
            tasks = get_all_tasks_for_mis(
                token, entry["db_id"], entry["fields"], entry["db_name"]
            )
        except Exception as exc:
            log.error("[%s] Slippage query failed: %s", entry["env_var"], exc)
            continue

        for t in tasks:
            if (t.get("slippage_days") or 0) >= min_days:
                owners = t.get("assignees") or []
                t["owner_name"] = (
                    ", ".join(o["name"] for o in owners if o.get("name")) or "—"
                )
                slipped.append(t)

    slipped.sort(key=lambda t: t.get("slippage_days", 0), reverse=True)
    log.info("[Slippage] %d slipped task(s) found (min_days=%d).", len(slipped), min_days)
    return slipped
