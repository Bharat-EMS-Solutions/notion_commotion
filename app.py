"""
Flask dashboard — serves the task health report as a live web page.
Reuses the same notion_client query logic as the email pipeline.

Run:  flask run   (or python app.py)
"""
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template_string, abort

from notion_client import get_task_report
from mailer import _group_tasks, _ALL_SECTIONS, _PRIORITY_COLORS

load_dotenv()

app = Flask(__name__)
log = logging.getLogger(__name__)

_CONFIG_FILE   = Path(__file__).parent / "config.json"
_DB_CONFIG_FILE = Path(__file__).parent / "databases.json"


def _load_databases():
    entries = json.loads(_DB_CONFIG_FILE.read_text())
    dbs = []
    for entry in entries:
        db_id = os.getenv(entry.get("env_var", ""))
        if db_id:
            dbs.append((db_id, entry.get("fields", {})))
    return dbs


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Task Health Report</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f3f4f6; color: #111827; font-size: 14px; }

  .topbar { background: #111827; color: #fff; padding: 18px 32px;
             display: flex; align-items: center; justify-content: space-between; }
  .topbar h1 { font-size: 18px; font-weight: 700; }
  .topbar .meta { font-size: 12px; color: #9ca3af; }
  .refresh-btn { background: #374151; color: #fff; border: none; border-radius: 6px;
                 padding: 7px 16px; font-size: 12px; cursor: pointer; text-decoration: none; }
  .refresh-btn:hover { background: #4b5563; }

  .db-tabs { background: #fff; border-bottom: 1px solid #e5e7eb;
              padding: 0 32px; display: flex; gap: 0; }
  .db-tab { padding: 14px 20px; font-size: 13px; font-weight: 600; cursor: pointer;
             border-bottom: 3px solid transparent; color: #6b7280; text-decoration: none;
             white-space: nowrap; }
  .db-tab.active { color: #111827; border-bottom-color: #111827; }
  .db-tab:hover:not(.active) { color: #374151; }

  .content { padding: 28px 32px; max-width: 1400px; }

  .stat-bar { display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }
  .stat-card { background: #fff; border-radius: 10px; padding: 16px 24px; flex: 1;
               min-width: 140px; box-shadow: 0 1px 3px rgba(0,0,0,.07); text-align: center; }
  .stat-card .num { font-size: 32px; font-weight: 800; }
  .stat-card .lbl { font-size: 11px; color: #9ca3af; margin-top: 4px; }

  .section { background: #fff; border-radius: 10px; margin-bottom: 24px;
              box-shadow: 0 1px 3px rgba(0,0,0,.07); overflow: hidden; }
  .section-header { padding: 14px 20px; display: flex; align-items: center;
                     justify-content: space-between; border-bottom: 1px solid #f3f4f6;
                     cursor: pointer; user-select: none; }
  .section-header h2 { font-size: 14px; font-weight: 700; }
  .section-header .sub { font-size: 12px; color: #9ca3af; margin-left: 10px; font-weight: 400; }
  .section-body { display: block; }
  .section-body.collapsed { display: none; }

  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px;
            color: #fff; font-size: 11px; font-weight: 700; white-space: nowrap; }

  .proj-header { padding: 8px 20px; background: #f9fafb; border-bottom: 1px solid #e5e7eb;
                  border-left: 4px solid var(--color); display: flex; align-items: center;
                  gap: 10px; font-size: 13px; font-weight: 700; }
  .proj-header a { color: #111827; text-decoration: none; }
  .proj-header a:hover { text-decoration: underline; }

  .parent-header { padding: 6px 20px 6px 36px; background: #fafafa;
                    border-bottom: 1px solid #f3f4f6; font-size: 12px; font-weight: 600;
                    color: #374151; }
  .parent-header a { color: #374151; text-decoration: none; }
  .parent-header a:hover { text-decoration: underline; }

  .task-row { display: flex; align-items: flex-start; padding: 10px 20px;
               border-bottom: 1px solid #f3f4f6; border-left: 3px solid var(--color);
               gap: 12px; }
  .task-row.indented { padding-left: 44px; }
  .task-row:last-child { border-bottom: none; }
  .task-row:hover { background: #fafafa; }
  .task-main { flex: 1; min-width: 0; }
  .task-name { font-size: 13px; font-weight: 600; color: #111827; text-decoration: none; }
  .task-name:hover { text-decoration: underline; color: #1d4ed8; }
  .task-crumb { font-size: 10px; color: #d1d5db; margin-bottom: 2px; }
  .task-crumb a { color: #d1d5db; text-decoration: none; }
  .task-meta { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .task-desc { font-size: 11px; color: #6b7280; margin-top: 3px; }
  .task-slip { font-size: 11px; color: #dc2626; margin-top: 3px; }
  .task-due { font-size: 12px; color: #6b7280; white-space: nowrap; padding-top: 1px; min-width: 90px; text-align: right; }
  .empty { padding: 16px 20px; color: #9ca3af; font-size: 13px; }

  .toggle-icon { font-size: 11px; color: #9ca3af; transition: transform .15s; }
  .collapsed-section .toggle-icon { transform: rotate(-90deg); }
</style>
</head>
<body>

<div class="topbar">
  <div>
    <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;">Task Health Report</div>
    <h1>{{ reports[active_idx].db_name }}</h1>
    <div class="meta" style="margin-top:4px;">{{ today }} &nbsp;·&nbsp; {{ reports[active_idx].total_open }} open tasks</div>
  </div>
  <a href="?db={{ active_idx }}" class="refresh-btn">↺ Refresh</a>
</div>

{% if reports|length > 1 %}
<div class="db-tabs">
  {% for r in reports %}
  <a href="?db={{ loop.index0 }}" class="db-tab {{ 'active' if loop.index0 == active_idx }}">
    {{ r.db_name }}
  </a>
  {% endfor %}
</div>
{% endif %}

{% set report = reports[active_idx] %}
{% set active_sections = sections | selectattr('key', 'ne', 'skip') | list %}

<div class="content">

  <!-- Stat bar -->
  <div class="stat-bar">
    {% for s in sections %}
    {% if report[s.key] is not none %}
    <div class="stat-card">
      <div class="num" style="color:{{ s.color }};">{{ report[s.key]|length }}</div>
      <div class="lbl">{{ s.label }}</div>
    </div>
    {% endif %}
    {% endfor %}
  </div>

  <!-- Sections -->
  {% for s in sections %}
  {% if report[s.key] is not none %}
  {% set tasks = report[s.key] %}
  <div class="section" style="--color:{{ s.color }};">
    <div class="section-header" onclick="toggle(this)">
      <div>
        <h2 style="display:inline;">{{ s.label }}</h2>
        <span class="sub">{{ s.desc }}</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px;">
        <span class="badge" style="background:{{ s.color if tasks else '#10b981' }};">{{ tasks|length }}</span>
        <span class="toggle-icon">▼</span>
      </div>
    </div>
    <div class="section-body">
      {% if not tasks %}
      <div class="empty">✓ All clear</div>
      {% else %}
      {% set groups = group_tasks(tasks) %}
      {% for proj_name, proj_url, direct, parent_groups in groups %}

        <div class="proj-header" style="--color:{{ s.color }};">
          {% if proj_url %}<a href="{{ proj_url }}" target="_blank">{{ proj_name }}</a>
          {% else %}{{ proj_name }}{% endif %}
          <span class="badge" style="background:{{ s.color }};font-size:10px;">
            {{ direct|length + parent_groups|sum(attribute=2)|length if parent_groups else direct|length }}
          </span>
        </div>

        {% for t in direct %}
        {{ task_row(t, s.key, s.color, false) }}
        {% endfor %}

        {% for par_name, par_url, par_tasks in parent_groups %}
        <div class="parent-header">
          ↳ {% if par_url %}<a href="{{ par_url }}" target="_blank">{{ par_name }}</a>
          {% else %}{{ par_name }}{% endif %}
          <span style="color:#9ca3af;font-weight:400;"> ({{ par_tasks|length }})</span>
        </div>
        {% for t in par_tasks %}
        {{ task_row(t, s.key, s.color, true) }}
        {% endfor %}
        {% endfor %}

      {% endfor %}
      {% endif %}
    </div>
  </div>
  {% endif %}
  {% endfor %}

</div>

<script>
function toggle(header) {
  const section = header.parentElement;
  const body = section.querySelector('.section-body');
  const icon = header.querySelector('.toggle-icon');
  const collapsed = body.classList.toggle('collapsed');
  icon.style.transform = collapsed ? 'rotate(-90deg)' : '';
}
</script>
</body>
</html>"""


def _task_row_html(task, section_key, color, indented):
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
        chips.append(f'<span class="badge" style="background:#6b7280;">{task["status"]}</span>')
    if task.get("priority"):
        pc = _PRIORITY_COLORS.get(task["priority"], "#9ca3af")
        chips.append(f'<span class="badge" style="background:{pc};">{task["priority"]}</span>')
    for team in (task.get("teams") or [])[:2]:
        chips.append(f'<span class="badge" style="background:#0ea5e9;">{team}</span>')
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
        f'<div class="task-row {indent_class}" style="--color:{color};">'
        f'<div class="task-main">'
        f'{crumb_html}'
        f'<a class="task-name" href="{task["url"]}" target="_blank">{task["name"]}</a>'
        f'{chips_html}{desc_html}{slip_html}'
        f'</div>'
        f'<div class="task-due">{due}</div>'
        f'</div>'
    )


@app.route("/")
def index():
    from flask import request
    from datetime import date
    from markupsafe import Markup

    databases = _load_databases()
    if not databases:
        abort(500, "No databases configured.")

    token = os.environ.get("NOTION_TOKEN", "")
    reports = []
    for db_id, fields in databases:
        try:
            reports.append(get_task_report(token, db_id, fields))
        except Exception as exc:
            log.error("Failed to query %s: %s", db_id, exc)

    if not reports:
        abort(500, "All database queries failed.")

    try:
        active_idx = int(request.args.get("db", 0))
        if active_idx >= len(reports):
            active_idx = 0
    except ValueError:
        active_idx = 0

    sections = [
        {"key": k, "label": l, "color": c, "desc": d}
        for k, l, c, d in _ALL_SECTIONS
    ]

    def _proj_task_count(parent_groups):
        return sum(len(pg[2]) for pg in parent_groups)

    return render_template_string(
        _TEMPLATE,
        reports=reports,
        active_idx=active_idx,
        sections=sections,
        today=date.today().strftime("%B %d, %Y"),
        group_tasks=_group_tasks,
        task_row=lambda t, sk, c, i: Markup(_task_row_html(t, sk, c, i)),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
