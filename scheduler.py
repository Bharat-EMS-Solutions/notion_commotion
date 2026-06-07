"""
scheduler.py — APScheduler integration.  Started inside app.py; runs as a
background thread for the entire lifetime of the Flask process.

Job schedule (IST = Asia/Kolkata, Mon–Sat):
  job_events        every 15 min, 09:00–19:00    Req 1 + Req 4
  job_health_daily  daily 08:00                   Req 2 + Req 5 + existing health reports
  job_slippage      daily 09:15                   Req 3

Each job writes to JOB_STATUS (shown on /jobs dashboard) and to the
standard Python logger.  max_instances=1 prevents overlap if a job is slow.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

IST          = pytz.timezone("Asia/Kolkata")
_APP_CONFIG  = Path(__file__).parent / "config.json"
_DB_CONFIG   = Path(__file__).parent / "databases.json"

# ── In-memory status store (shown on /jobs page) ──────────────────────────
JOB_STATUS: dict = {
    "job_events": {
        "label":       "Task Change Detection",
        "description": "Req 1 + Req 4 — new tasks, reassignments, project starts",
        "schedule":    "Every 15 min · Mon–Sat · 9 AM–7 PM IST",
        "last_run":    None,
        "last_status": "never",
        "last_error":  "",
    },
    "job_health_daily": {
        "label":       "Daily Digest + Health Reports + Reminders",
        "description": "Req 2 + Req 5 + existing Task Health Reports to executives",
        "schedule":    "Daily 8:00 AM IST · Mon–Sat",
        "last_run":    None,
        "last_status": "never",
        "last_error":  "",
    },
    "job_slippage": {
        "label":       "Slippage Digest",
        "description": "Req 3 — due-date slippage report to executives, leaders, PMs",
        "schedule":    "Daily 9:15 AM IST · Mon–Sat",
        "last_run":    None,
        "last_status": "never",
        "last_error":  "",
    },
}


# ── Shared helpers ─────────────────────────────────────────────────────────

def _mark(job_id: str, ok: bool, error: str = ""):
    JOB_STATUS[job_id]["last_run"]    = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
    JOB_STATUS[job_id]["last_status"] = "ok" if ok else "error"
    JOB_STATUS[job_id]["last_error"]  = error


def _load_context():
    """
    Load config + DB entries (with resolved Notion names) + Notion token.
    Returns (cfg, db_entries, notion_token).
    """
    import requests as _req

    cfg        = json.loads(_APP_CONFIG.read_text())
    raw_dbs    = json.loads(_DB_CONFIG.read_text())
    notion_token = os.environ.get("NOTION_TOKEN", "")
    headers    = {"Authorization": f"Bearer {notion_token}", "Notion-Version": "2022-06-28"}

    db_entries = []
    for entry in raw_dbs:
        env_var = entry.get("env_var", "")
        db_id   = os.getenv(env_var)
        if not db_id:
            log.warning("[Scheduler] %r not set — skipping.", env_var)
            continue
        db_name = env_var
        try:
            r = _req.get(
                f"https://api.notion.com/v1/databases/{db_id}",
                headers=headers, timeout=10,
            )
            if r.ok:
                db_name = (
                    "".join(t.get("plain_text", "") for t in r.json().get("title", []))
                    or env_var
                )
        except Exception:
            pass
        db_entries.append({
            "env_var": env_var,
            "db_id":   db_id,
            "db_name": db_name,
            "fields":  entry.get("fields", {}),
            "digest":  entry.get("digest", False),
        })

    return cfg, db_entries, notion_token


def _graph_token(cfg_unused=None):
    from mailer import _acquire_token
    return _acquire_token(
        os.environ["AZURE_TENANT_ID"],
        os.environ["AZURE_CLIENT_ID"],
        os.environ["AZURE_CLIENT_SECRET"],
    )


# ── Job 1 — Task change detection ─────────────────────────────────────────
# Req 1: Task assigned / reassigned  →  assignee + admins + PM
# Req 4: Task moves to In Progress   →  detailed kickoff email to PMs/assignees

def job_events():
    log.info("[Scheduler] job_events — start")
    try:
        from state_cache      import StateCache
        from event_detector   import detect_changes
        from event_dispatcher import dispatch_events

        cfg, db_entries, notion_token = _load_context()
        token  = _graph_token()
        cache  = StateCache()
        by_db  = detect_changes(notion_token, db_entries, cache)
        errors = []

        for entry in db_entries:
            ev_var = entry["env_var"]
            events = by_db.get(ev_var, [])
            if not events:
                continue
            errs = dispatch_events(
                events       = events,
                cfg          = cfg,
                token        = token,
                notion_token = notion_token,
                db_id        = entry["db_id"],
                db_env_var   = ev_var,
                fields       = entry["fields"],
                db_name      = entry["db_name"],
            )
            errors.extend(errs)

        cache.close()

        if errors:
            raise RuntimeError("; ".join(errors))

        _mark("job_events", ok=True)
        log.info("[Scheduler] job_events — OK")

    except Exception as exc:
        _mark("job_events", ok=False, error=str(exc))
        log.error("[Scheduler] job_events — FAILED: %s", exc)


# ── Job 2 — Daily digest + health reports + due-soon reminders ────────────
# Req 2: Owner daily digest (in-progress tasks per person)
# Req 5: Due-in-2-days reminder → assignees + PMs
# Existing: Task Health Report (both DBs) → executives

def job_health_daily():
    log.info("[Scheduler] job_health_daily — start")
    errors = []

    try:
        cfg, db_entries, notion_token = _load_context()
        token = _graph_token()

        exec_emails = (
            cfg.get("roles", {}).get("executive", [])
            or cfg.get("recipient_emails", [])
        )

        # ── 2a. Task Health Report → executives (existing main.py behaviour) ──────
        from notion_client import get_task_report
        from mailer        import send_reminder_email

        for entry in db_entries:
            try:
                report = get_task_report(
                    token       = notion_token,
                    database_id = entry["db_id"],
                    fields      = entry["fields"],
                )
                db_recipients = (
                    entry.get("recipient_emails") or exec_emails
                )
                send_reminder_email(
                    tenant_id        = os.environ["AZURE_TENANT_ID"],
                    client_id        = os.environ["AZURE_CLIENT_ID"],
                    client_secret    = os.environ["AZURE_CLIENT_SECRET"],
                    sender_email     = cfg["sender_email"],
                    recipient_emails = db_recipients,
                    report           = report,
                )
                log.info("[Scheduler] Health report sent — %s", entry["db_name"])
            except Exception as exc:
                errors.append(f"HealthReport|{entry['db_name']}: {exc}")
                log.error("[Scheduler] Health report failed — %s: %s", entry["db_name"], exc)

        # ── 2b. Daily owner digest → individual owners (Req 2) ──────────────────
        from notion_client import get_in_progress_tasks_by_owner
        from mailer        import send_owner_daily_digests

        digest_entries = [e for e in db_entries if e.get("digest")]
        owner_map: dict = {}
        for entry in digest_entries:
            try:
                by_owner = get_in_progress_tasks_by_owner(
                    notion_token, entry["db_id"], entry["fields"], entry["db_name"]
                )
                for email, info in by_owner.items():
                    if email not in owner_map:
                        owner_map[email] = {"owner_name": info["owner_name"], "tasks": []}
                    owner_map[email]["tasks"].extend(info["tasks"])
            except Exception as exc:
                errors.append(f"OwnerDigest|{entry['db_name']}: {exc}")
                log.error("[Scheduler] Owner digest query failed — %s: %s",
                          entry["db_name"], exc)

        if owner_map:
            digest_errors = send_owner_daily_digests(
                tenant_id     = os.environ["AZURE_TENANT_ID"],
                client_id     = os.environ["AZURE_CLIENT_ID"],
                client_secret = os.environ["AZURE_CLIENT_SECRET"],
                sender_email  = cfg["sender_email"],
                owner_map     = owner_map,
                app_base_url  = os.environ.get("APP_BASE_URL", "http://localhost:5000"),
                originator_id = os.environ.get("ACTIONABLE_MSG_ORIGINATOR", ""),
            )
            errors.extend(digest_errors)
            log.info("[Scheduler] Owner digest sent to %d owner(s).", len(owner_map))
        else:
            log.info("[Scheduler] No in-progress tasks with owner emails — digest skipped.")

        # ── 2c. Due-in-2-days reminders → assignees + PMs (Req 5) ───────────────
        from state_cache      import StateCache
        from event_detector   import detect_due_soon
        from event_dispatcher import dispatch_due_soon
        from notion_client    import get_all_tasks_for_mis

        due_days = cfg.get("mis", {}).get("due_soon_days", 2)
        cache    = StateCache()
        due_rows = detect_due_soon(cache, days=due_days)

        if due_rows:
            full_tasks: dict = {}
            for entry in db_entries:
                try:
                    tasks = get_all_tasks_for_mis(
                        notion_token, entry["db_id"], entry["fields"], entry["db_name"]
                    )
                    full_tasks.update({t["id"]: t for t in tasks})
                except Exception as exc:
                    errors.append(f"DueSoon|{entry['db_name']}: {exc}")

            due_errors = dispatch_due_soon(due_rows, full_tasks, cfg, token, cache)
            errors.extend(due_errors)
            log.info("[Scheduler] Due-soon reminders dispatched for %d task(s).", len(due_rows))

        cache.close()

        if errors:
            raise RuntimeError("; ".join(errors))

        _mark("job_health_daily", ok=True)
        log.info("[Scheduler] job_health_daily — OK")

    except Exception as exc:
        _mark("job_health_daily", ok=False, error=str(exc))
        log.error("[Scheduler] job_health_daily — FAILED: %s", exc)


# ── Job 3 — Slippage digest ───────────────────────────────────────────────
# Req 3: Correlate due dates + history, notice slippage → executives + leaders + PMs

def job_slippage():
    log.info("[Scheduler] job_slippage — start")
    try:
        from event_detector   import detect_slippage
        from event_dispatcher import dispatch_slippage

        cfg, db_entries, notion_token = _load_context()
        min_days = cfg.get("mis", {}).get("slippage_min_days", 2)
        token    = _graph_token()
        slipped  = detect_slippage(notion_token, db_entries, min_days=min_days)
        errors   = dispatch_slippage(slipped, cfg, token)

        if errors:
            raise RuntimeError("; ".join(errors))

        _mark("job_slippage", ok=True)
        log.info("[Scheduler] job_slippage — OK")

    except Exception as exc:
        _mark("job_slippage", ok=False, error=str(exc))
        log.error("[Scheduler] job_slippage — FAILED: %s", exc)


# ── Scheduler bootstrap ───────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    """
    Create, configure, and start the APScheduler BackgroundScheduler.
    Must be called exactly once from app.py (guarded against Flask reloader).
    Returns the running scheduler instance so app.py can shut it down cleanly.
    """
    scheduler = BackgroundScheduler(
        timezone    = IST,
        job_defaults = {"max_instances": 1, "coalesce": True},
    )

    # Req 1 + Req 4 — every 15 min, Mon–Sat, 9 AM–7 PM IST
    scheduler.add_job(
        job_events,
        CronTrigger(day_of_week="mon-sat", hour="9-18", minute="*/15", timezone=IST),
        id="job_events", name="Task Change Detection", replace_existing=True,
    )

    # Req 2 + Req 5 + Health reports — daily 8:00 AM IST
    scheduler.add_job(
        job_health_daily,
        CronTrigger(day_of_week="mon-sat", hour=8, minute=0, timezone=IST),
        id="job_health_daily", name="Daily Digest + Health Reports", replace_existing=True,
    )

    # Req 3 — slippage digest, daily 9:15 AM IST (after health report finishes)
    scheduler.add_job(
        job_slippage,
        CronTrigger(day_of_week="mon-sat", hour=9, minute=15, timezone=IST),
        id="job_slippage", name="Slippage Digest", replace_existing=True,
    )

    scheduler.start()
    log.info(
        "[Scheduler] Started — %d job(s): %s",
        len(scheduler.get_jobs()),
        ", ".join(j.id for j in scheduler.get_jobs()),
    )
    return scheduler
