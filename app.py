"""
Flask dashboard — streams a loading UI immediately, then injects the report
once all Notion queries complete. Progress bar updates after each DB query.

Run:  python3 app.py   (then open http://localhost:5000)
"""
import json
import logging
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, request, stream_with_context
from markupsafe import Markup
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))

from mailer import _ALL_SECTIONS, _PRIORITY_COLORS, _group_tasks
from notion_client import get_task_report

load_dotenv()

app = Flask(__name__)
log = logging.getLogger(__name__)

_DB_CONFIG_FILE = Path(__file__).parent / "databases.json"


def _load_databases():
    entries = json.loads(_DB_CONFIG_FILE.read_text())
    return [
        (os.getenv(e.get("env_var", "")), e.get("fields", {}))
        for e in entries
        if os.getenv(e.get("env_var", ""))
    ]


# ---------------------------------------------------------------------------
# HTML rendering helpers (same logic as email, adapted for the web)
# ---------------------------------------------------------------------------

def _task_row_html(task: dict, section_key: str, color: str, indented: bool) -> str:
    crumbs = []
    if task.get("project"):
        p = task["project"]
        crumbs.append(f'<a href="{p["url"]}">{p["name"]}</a>' if p.get("url") else p["name"])
    if task.get("parent_task"):
        pt = task["parent_task"]
        crumbs.append(f'<a href="{pt["url"]}">{pt["name"]}</a>' if pt.get("url") else pt["name"])
    crumb_html = f'<div class="task-crumb">{" › ".join(crumbs)}</div>' if crumbs else ""

    chips = []
    if task.get("status"):
        chips.append(f'<span class="badge" style="background:#6b7280">{task["status"]}</span>')
    if task.get("priority"):
        pc = _PRIORITY_COLORS.get(task["priority"], "#9ca3af")
        chips.append(f'<span class="badge" style="background:{pc}">{task["priority"]}</span>')
    for team in (task.get("teams") or [])[:2]:
        chips.append(f'<span class="badge" style="background:#0ea5e9">{team}</span>')
    chips_html = f'<div class="task-meta">{"".join(chips)}</div>' if chips else ""

    desc_html = ""
    if task.get("description"):
        ellipsis = "…" if len(task["description"]) >= 120 else ""
        desc_html = f'<div class="task-desc">{task["description"]}{ellipsis}</div>'

    slip_html = ""
    if section_key == "max_slippage" and task.get("slippage_days"):
        slip_html = (
            f'<div class="task-slip">Originally: {task.get("original_date","?")} '
            f'→ slipped <strong>{task["slippage_days"]} day(s)</strong></div>'
        )

    indent_class = "indented" if indented else ""
    due = task.get("due_date") or "—"
    return (
        f'<div class="task-row {indent_class}" style="--color:{color}">'
        f'<div class="task-main">'
        f'{crumb_html}'
        f'<a class="task-name" href="{task["url"]}" target="_blank">{task["name"]}</a>'
        f'{chips_html}{desc_html}{slip_html}'
        f'</div>'
        f'<div class="task-due">{due}</div>'
        f'</div>'
    )


def _render_report_html(reports: list, active_idx: int) -> str:
    report     = reports[active_idx]
    today      = date.today().strftime("%B %d, %Y")
    fetched_at = datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
    db_name    = report["db_name"]
    total   = report["total_open"]

    active_sections = [
        (k, l, c, d) for k, l, c, d in _ALL_SECTIONS
        if report.get(k) is not None
    ]

    # ---- top bar ----
    tabs_html = ""
    if len(reports) > 1:
        tabs = "".join(
            f'<a href="?db={i}" class="db-tab {"active" if i == active_idx else ""}">'
            f'{r["db_name"]}</a>'
            for i, r in enumerate(reports)
        )
        tabs_html = f'<div class="db-tabs">{tabs}</div>'

    topbar = (
        f'<div class="topbar">'
        f'<div>'
        f'<div class="topbar-label">Task Health Report</div>'
        f'<h1>{db_name}</h1>'
        f'<div class="topbar-meta">{today} &nbsp;·&nbsp; {total} open tasks'
        f' &nbsp;·&nbsp; Last fetched: {fetched_at}</div>'
        f'</div>'
        f'<a href="?db={active_idx}" class="refresh-btn">↺ Refresh</a>'
        f'</div>'
        f'{tabs_html}'
    )

    # ---- stat bar ----
    stats = "".join(
        f'<div class="stat-card">'
        f'<div class="stat-num" style="color:{c}">{len(report[k])}</div>'
        f'<div class="stat-lbl">{l}</div>'
        f'</div>'
        for k, l, c, _ in active_sections
    )
    stat_bar = f'<div class="stat-bar">{stats}</div>'

    # ---- sections ----
    sections_html = ""
    for key, label, color, desc in active_sections:
        tasks = report[key]
        badge_color = color if tasks else "#10b981"
        badge = f'<span class="badge" style="background:{badge_color}">{len(tasks)}</span>'

        if not tasks:
            body = '<div class="empty">✓ All clear</div>'
        else:
            rows = []
            for proj_name, proj_url, direct, parent_groups in _group_tasks(tasks):
                proj_total = len(direct) + sum(len(pg[2]) for pg in parent_groups)
                proj_link = f'<a href="{proj_url}" target="_blank">{proj_name}</a>' if proj_url else proj_name
                rows.append(
                    f'<div class="proj-header" style="--color:{color}">'
                    f'{proj_link}'
                    f'<span class="badge" style="background:{color};font-size:10px;margin-left:8px">{proj_total}</span>'
                    f'</div>'
                )
                for t in direct:
                    rows.append(_task_row_html(t, key, color, False))
                for par_name, par_url, par_tasks in parent_groups:
                    par_link = f'<a href="{par_url}" target="_blank">{par_name}</a>' if par_url else par_name
                    rows.append(
                        f'<div class="parent-header">'
                        f'↳ {par_link}'
                        f'<span class="parent-count">({len(par_tasks)})</span>'
                        f'</div>'
                    )
                    for t in par_tasks:
                        rows.append(_task_row_html(t, key, color, True))
            body = "".join(rows)

        sections_html += (
            f'<div class="section" style="--color:{color}">'
            f'<div class="section-header" onclick="toggleSection(this)">'
            f'<div><h2>{label}</h2><span class="section-desc">{desc}</span></div>'
            f'<div style="display:flex;align-items:center;gap:10px">{badge}'
            f'<span class="toggle-icon" style="transform:rotate(-90deg)">▼</span></div>'
            f'</div>'
            f'<div class="section-body collapsed">{body}</div>'
            f'</div>'
        )

    return topbar + f'<div class="content">{stat_bar}{sections_html}</div>'


# ---------------------------------------------------------------------------
# Page shell (sent immediately — user sees this while Notion is queried)
# ---------------------------------------------------------------------------

_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Task Health Report</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f3f4f6;color:#111827;font-size:14px}

/* ---- Loading overlay ---- */
#loading{position:fixed;inset:0;background:#111827;display:flex;
          flex-direction:column;align-items:center;justify-content:center;
          z-index:999;transition:opacity .4s}
#loading.done{opacity:0;pointer-events:none}
.loading-title{font-size:11px;font-weight:600;color:#6b7280;
               text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.loading-app{font-size:26px;font-weight:800;color:#fff;margin-bottom:32px}
.progress-track{width:320px;height:4px;background:#374151;border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);
               border-radius:2px;width:0%;transition:width .5s ease}
.loading-status{margin-top:14px;font-size:13px;color:#9ca3af;min-height:20px;
                text-align:center}
.loading-dots::after{content:'';animation:dots 1.2s steps(4,end) infinite}
@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}
                100%{content:''}}

/* ---- App shell (hidden until report loads) ---- */
#app{display:none}

/* ---- App styles ---- */
.topbar{background:#111827;color:#fff;padding:18px 32px;
        display:flex;align-items:center;justify-content:space-between}
.topbar-label{font-size:11px;font-weight:600;color:#6b7280;
              text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.topbar h1{font-size:20px;font-weight:800}
.topbar-meta{font-size:12px;color:#9ca3af;margin-top:4px}
.refresh-btn{background:#374151;color:#fff;border:none;border-radius:6px;
             padding:7px 16px;font-size:12px;cursor:pointer;text-decoration:none}
.refresh-btn:hover{background:#4b5563}
.db-tabs{background:#fff;border-bottom:1px solid #e5e7eb;padding:0 32px;display:flex}
.db-tab{padding:14px 20px;font-size:13px;font-weight:600;cursor:pointer;
        border-bottom:3px solid transparent;color:#6b7280;text-decoration:none}
.db-tab.active{color:#111827;border-bottom-color:#111827}
.db-tab:hover:not(.active){color:#374151}
.content{padding:28px 32px;max-width:1400px}
.stat-bar{display:flex;gap:16px;margin-bottom:28px;flex-wrap:wrap}
.stat-card{background:#fff;border-radius:10px;padding:16px 24px;flex:1;min-width:140px;
           box-shadow:0 1px 3px rgba(0,0,0,.07);text-align:center}
.stat-num{font-size:32px;font-weight:800}
.stat-lbl{font-size:11px;color:#9ca3af;margin-top:4px}
.section{background:#fff;border-radius:10px;margin-bottom:24px;
         box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}
.section-header{padding:14px 20px;display:flex;align-items:center;
                justify-content:space-between;border-bottom:1px solid #f3f4f6;
                cursor:pointer;user-select:none}
.section-header h2{font-size:14px;font-weight:700;display:inline}
.section-desc{font-size:12px;color:#9ca3af;margin-left:10px;font-weight:400}
.section-body{display:block}
.section-body.collapsed{display:none}
.toggle-icon{font-size:11px;color:#9ca3af;display:inline-block;transition:transform .15s}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;color:#fff;
       font-size:11px;font-weight:700;white-space:nowrap}
.proj-header{padding:8px 20px;background:#f9fafb;border-bottom:1px solid #e5e7eb;
             border-left:4px solid var(--color);display:flex;align-items:center;
             font-size:13px;font-weight:700}
.proj-header a{color:#111827;text-decoration:none}
.proj-header a:hover{text-decoration:underline}
.parent-header{padding:6px 20px 6px 36px;background:#fafafa;
               border-bottom:1px solid #f3f4f6;font-size:12px;font-weight:600;color:#374151}
.parent-header a{color:#374151;text-decoration:none}
.parent-header a:hover{text-decoration:underline}
.parent-count{color:#9ca3af;font-weight:400;margin-left:4px}
.task-row{display:flex;align-items:flex-start;padding:10px 20px;
          border-bottom:1px solid #f3f4f6;border-left:3px solid var(--color);gap:12px}
.task-row.indented{padding-left:44px}
.task-row:last-child{border-bottom:none}
.task-row:hover{background:#fafafa}
.task-main{flex:1;min-width:0}
.task-crumb{font-size:10px;color:#d1d5db;margin-bottom:2px}
.task-crumb a{color:#d1d5db;text-decoration:none}
.task-name{font-size:13px;font-weight:600;color:#111827;text-decoration:none}
.task-name:hover{text-decoration:underline;color:#1d4ed8}
.task-meta{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.task-desc{font-size:11px;color:#6b7280;margin-top:3px}
.task-slip{font-size:11px;color:#dc2626;margin-top:3px}
.task-due{font-size:12px;color:#6b7280;white-space:nowrap;padding-top:1px;
          min-width:90px;text-align:right}
.empty{padding:16px 20px;color:#9ca3af;font-size:13px}
</style>
</head>
<body>

<div id="loading">
  <div class="loading-title">Task Health Report</div>
  <div class="loading-app">Notion</div>
  <div class="progress-track"><div class="progress-fill" id="prog"></div></div>
  <div class="loading-status" id="status">Connecting to Notion<span class="loading-dots"></span></div>
</div>

<div id="app"></div>

<!-- padding: forces browser to render the loading UI before blocking on the rest -->
"""  + "<!--" + " " * 4096 + "-->"


_SCRIPTS = """
<script>
function setProgress(pct, msg) {
  document.getElementById('prog').style.width = pct + '%';
  document.getElementById('status').textContent = msg;
}
function showReport(html) {
  setProgress(100, 'Done');
  document.getElementById('app').innerHTML = html;
  document.getElementById('app').style.display = 'block';
  setTimeout(function() {
    document.getElementById('loading').classList.add('done');
    setTimeout(function() {
      document.getElementById('loading').style.display = 'none';
    }, 400);
  }, 200);
}
function toggleSection(header) {
  var body = header.nextElementSibling;
  var icon = header.querySelector('.toggle-icon');
  var collapsed = body.classList.toggle('collapsed');
  icon.style.transform = collapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
}
</script>
"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    databases = _load_databases()
    if not databases:
        return "No databases configured — check databases.json and .env.", 500

    try:
        active_idx = max(0, min(int(request.args.get("db", 0)), len(databases) - 1))
    except ValueError:
        active_idx = 0

    token = os.environ.get("NOTION_TOKEN", "")
    n = len(databases)

    def generate():
        # 1 — Send loading shell immediately (user sees spinner straight away)
        yield _SHELL + _SCRIPTS

        # 2 — Query each database, streaming progress after each one
        reports = []
        for i, (db_id, fields) in enumerate(databases):
            pct = int(10 + (i / n) * 75)
            db_label = f"database {i + 1} of {n}"
            yield f'<script>setProgress({pct}, "Querying {db_label}...");</script>\n'
            try:
                report = get_task_report(token, db_id, fields)
                reports.append(report)
                yield f'<script>setProgress({pct + int(75 / n)}, "Queried: {report["db_name"]}");</script>\n'
            except Exception as exc:
                log.error("Failed to query %s: %s", db_id, exc)

        if not reports:
            yield '<script>setProgress(100, "Error: all queries failed.");</script>\n'
            return

        # 3 — Render and inject report
        yield '<script>setProgress(92, "Rendering report...");</script>\n'
        safe_idx = min(active_idx, len(reports) - 1)
        html = _render_report_html(reports, safe_idx)
        yield f'<script>showReport({json.dumps(html)});</script>\n'
        yield '</body></html>'

    return Response(stream_with_context(generate()), mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
