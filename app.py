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

from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))

from mailer import _ALL_SECTIONS, _PRIORITY_COLORS, _group_tasks, send_reminder_email
from notion_client import get_task_report, get_users, scan_user_mentions

load_dotenv()

app = Flask(__name__)
log = logging.getLogger(__name__)

_DB_CONFIG_FILE  = Path(__file__).parent / "databases.json"
_APP_CONFIG_FILE = Path(__file__).parent / "config.json"


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



def _render_sections(report: dict) -> str:
    """Render stat bar + sections for one report (no topbar)."""
    active_sections = [
        (k, l, c, d) for k, l, c, d in _ALL_SECTIONS
        if report.get(k) is not None
    ]
    stats = "".join(
        f'<div class="stat-card"><div class="stat-num" style="color:{c}">{len(report[k])}</div>'
        f'<div class="stat-lbl">{l}</div></div>'
        for k, l, c, _ in active_sections
    )
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
                    f'<div class="proj-header" style="--color:{color}" onclick="toggleGroup(this)">'
                    f'{proj_link}'
                    f'<div style="display:flex;align-items:center;gap:8px;flex-shrink:0">'
                    f'<span class="badge" style="background:{color};font-size:10px">{proj_total}</span>'
                    f'<span class="toggle-icon">▼</span>'
                    f'</div>'
                    f'</div>'
                )
                rows.append('<div class="proj-body">')
                for t in direct:
                    rows.append(_task_row_html(t, key, color, False))
                for par_name, par_url, par_tasks in parent_groups:
                    par_link = f'<a href="{par_url}" target="_blank">{par_name}</a>' if par_url else par_name
                    rows.append(
                        f'<div class="parent-header" onclick="toggleGroup(this)">'
                        f'<span>↳ {par_link}<span class="parent-count"> ({len(par_tasks)})</span></span>'
                        f'<span class="toggle-icon">▼</span>'
                        f'</div>'
                    )
                    rows.append('<div class="parent-body">')
                    for t in par_tasks:
                        rows.append(_task_row_html(t, key, color, True))
                    rows.append('</div>')
                rows.append('</div>')
            body = "".join(rows)
        sections_html += (
            f'<div class="section" style="--color:{color}">'
            f'<div class="section-header" onclick="toggleSection(this)">'
            f'<div><h2>{label}</h2><span class="section-desc">{desc}</span></div>'
            f'<div style="display:flex;align-items:center;gap:10px">{badge}'
            f'<span class="toggle-icon" style="transform:rotate(-90deg)">▼</span></div>'
            f'</div><div class="section-body collapsed">{body}</div></div>'
        )
    return f'<div class="stat-bar">{stats}</div>{sections_html}'


def _render_report_html(reports: list, active_idx: int) -> str:
    """Full page: topbar + JS-switched panels. No reload on tab click."""
    today      = date.today().strftime("%B %d, %Y")
    fetched_at = datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")

    topbar = (
        f'<div class="topbar">'
        f'<div>'
        f'<div class="topbar-label">Task Health Report</div>'
        f'<h1 id="topbar-title">{reports[active_idx]["db_name"]}</h1>'
        f'<div class="topbar-meta">'
        f'{today} &nbsp;·&nbsp; '
        f'<span id="topbar-total">{reports[active_idx]["total_open"]}</span> open tasks'
        f' &nbsp;·&nbsp; Last fetched: {fetched_at}'
        f'</div></div>'
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<span id="depth-lbl" style="font-size:11px;color:#9ca3af;white-space:nowrap">'
        f'All collapsed</span>'
        f'<button id="expand-btn"   onclick="expandStep()"   class="ctrl-btn">Expand +</button>'
        f'<button id="collapse-btn" onclick="collapseStep()" class="ctrl-btn" disabled>Collapse −</button>'
        f'<button id="send-btn" onclick="sendReport()" class="ctrl-btn">✉ Send Report</button>'
        f'<a href="/mentions" class="refresh-btn">⊕ Mentions</a>'
        f'<a href="/" class="refresh-btn">↺ Refresh</a>'
        f'</div>'
        f'</div>'
    )

    tabs_html = ""
    if len(reports) > 1:
        tabs = "".join(
            f'<button class="db-tab {"active" if i == active_idx else ""}" onclick="switchTab({i})">'
            f'{r["db_name"]}</button>'
            for i, r in enumerate(reports)
        )
        tabs_html = f'<div class="db-tabs">{tabs}</div>'

    panels = "".join(
        f'<div class="db-panel" id="panel-{i}" '
        f'data-name="{r["db_name"]}" data-total="{r["total_open"]}" '
        f'style="{"display:block" if i == active_idx else "display:none"}">'
        f'{_render_sections(r)}</div>'
        for i, r in enumerate(reports)
    )

    return topbar + tabs_html + f'<div class="content">{panels}</div>'



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
             padding:7px 16px;font-size:12px;cursor:pointer;text-decoration:none;
             font-family:inherit}
.refresh-btn:hover{background:#4b5563}
.ctrl-btn{background:#1f2937;color:#d1d5db;border:1px solid #374151;border-radius:6px;
          padding:7px 12px;font-size:12px;cursor:pointer;font-family:inherit;white-space:nowrap}
.ctrl-btn:hover:not(:disabled){background:#374151;color:#fff}
.ctrl-btn:disabled{opacity:.35;cursor:default}
.db-tabs{background:#fff;border-bottom:1px solid #e5e7eb;padding:0 32px;display:flex}
.db-tab{padding:14px 20px;font-size:13px;font-weight:600;cursor:pointer;
        border:none;border-bottom:3px solid transparent;background:none;
        color:#6b7280;font-family:inherit;text-decoration:none;outline:none}
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
             justify-content:space-between;font-size:13px;font-weight:700;
             cursor:pointer;user-select:none}
.proj-header a{color:#111827;text-decoration:none}
.proj-header a:hover{text-decoration:underline}
.proj-body.collapsed{display:none}
.parent-header{padding:6px 20px 6px 36px;background:#fafafa;
               border-bottom:1px solid #f3f4f6;font-size:12px;font-weight:600;color:#374151;
               display:flex;align-items:center;justify-content:space-between;
               cursor:pointer;user-select:none}
.parent-header a{color:#374151;text-decoration:none}
.parent-header a:hover{text-decoration:underline}
.parent-count{color:#9ca3af;font-weight:400;margin-left:4px}
.parent-body.collapsed{display:none}
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
/* ---- Depth control: 0=all collapsed, 1=sections open, 2=+projects, 3=fully expanded ---- */
var _depth = 0;
var _DEPTH_LABELS = ['All collapsed', 'Sections open', '+ Projects open', 'Fully expanded'];

function _activePanel() {
  var panels = document.querySelectorAll('.db-panel');
  for (var i = 0; i < panels.length; i++) {
    if (panels[i].style.display !== 'none') return panels[i];
  }
  return panels[0];
}

function _applyDepth(panel, d) {
  function sync(sel, collapsed) {
    panel.querySelectorAll(sel).forEach(function(body) {
      body.classList.toggle('collapsed', collapsed);
      var hdr = body.previousElementSibling;
      if (hdr) {
        var icon = hdr.querySelector('.toggle-icon');
        if (icon) icon.style.transform = collapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
      }
    });
  }
  sync('.section-body', d < 1);
  sync('.proj-body',    d < 2);
  sync('.parent-body',  d < 3);
}

function _updateDepthBtns() {
  var cb  = document.getElementById('collapse-btn');
  var eb  = document.getElementById('expand-btn');
  var lbl = document.getElementById('depth-lbl');
  if (cb)  cb.disabled  = _depth <= 0;
  if (eb)  eb.disabled  = _depth >= 3;
  if (lbl) lbl.textContent = _DEPTH_LABELS[_depth];
}

function collapseStep() {
  if (_depth <= 0) return;
  _depth--;
  _applyDepth(_activePanel(), _depth);
  _updateDepthBtns();
}

function expandStep() {
  if (_depth >= 3) return;
  _depth++;
  _applyDepth(_activePanel(), _depth);
  _updateDepthBtns();
}

/* ---- Tab switching ---- */
function switchTab(i) {
  var panels = document.querySelectorAll('.db-panel');
  var tabs   = document.querySelectorAll('.db-tab');
  panels.forEach(function(p, idx) { p.style.display = idx === i ? 'block' : 'none'; });
  tabs.forEach(function(t, idx)   { t.classList.toggle('active', idx === i); });
  var panel = document.getElementById('panel-' + i);
  document.getElementById('topbar-title').textContent = panel.dataset.name;
  document.getElementById('topbar-total').textContent = panel.dataset.total;
  _applyDepth(panel, _depth);
}

/* ---- Manual toggles (individual headers) ---- */
function toggleSection(header) {
  var body = header.nextElementSibling;
  var icon = header.querySelector('.toggle-icon');
  var collapsed = body.classList.toggle('collapsed');
  icon.style.transform = collapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
}
function toggleGroup(header) {
  var body = header.nextElementSibling;
  var icon = header.querySelector('.toggle-icon');
  var collapsed = body.classList.toggle('collapsed');
  icon.style.transform = collapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
}

/* ---- Send report ---- */
function sendReport() {
  if (!confirm('Send the task health report email to all configured recipients?')) return;
  var btn = document.getElementById('send-btn');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  fetch('/send-report', {method:'POST'})
    .then(function(r){return r.json();})
    .then(function(d){
      btn.textContent = d.ok ? '✓ Sent' : '✗ Failed';
      btn.style.background = d.ok ? '#10b981' : '#dc2626';
      btn.style.color = '#fff';
      if (!d.ok) alert('Send error: ' + d.msg);
    })
    .catch(function(){
      btn.textContent = '✗ Error';
      btn.style.background = '#dc2626';
      btn.disabled = false;
    });
}

/* ---- Loading / report injection ---- */
function setProgress(pct, msg) {
  document.getElementById('prog').style.width = pct + '%';
  document.getElementById('status').textContent = msg;
}
function showReport(html) {
  setProgress(100, 'Done');
  document.getElementById('app').innerHTML = html;
  document.getElementById('app').style.display = 'block';
  _updateDepthBtns();
  setTimeout(function() {
    document.getElementById('loading').classList.add('done');
    setTimeout(function() { document.getElementById('loading').style.display = 'none'; }, 400);
  }, 200);
}
</script>
"""


# ---------------------------------------------------------------------------
# Mentions feature
# ---------------------------------------------------------------------------

_MENTIONS_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mentions Scan</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f3f4f6;color:#111827;font-size:14px}
.topbar{background:#111827;color:#fff;padding:18px 32px;
        display:flex;align-items:center;justify-content:space-between}
.topbar-label{font-size:11px;font-weight:600;color:#6b7280;
              text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.topbar h1{font-size:20px;font-weight:800}
.topbar-meta{font-size:12px;color:#9ca3af;margin-top:4px}
.nav-btn{background:#374151;color:#fff;border:none;border-radius:6px;
         padding:7px 14px;font-size:12px;cursor:pointer;text-decoration:none;
         font-family:inherit;white-space:nowrap}
.nav-btn:hover{background:#4b5563}
.progress-wrap{height:3px;background:#374151}
.progress-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);
               width:0%;transition:width .4s ease}
.content{padding:28px 32px;max-width:960px}
.found-bar{font-size:12px;color:#9ca3af;margin-bottom:12px;min-height:18px}
.mention-card{background:#fff;border-radius:10px;margin-bottom:12px;
              box-shadow:0 1px 3px rgba(0,0,0,.07);padding:14px 20px;
              display:flex;align-items:flex-start;gap:12px}
.mention-main{flex:1;min-width:0}
.mention-title{font-size:13px;font-weight:600;color:#111827;text-decoration:none;display:block;margin-bottom:6px}
.mention-title:hover{color:#1d4ed8;text-decoration:underline}
.mention-tags{display:flex;flex-wrap:wrap;gap:6px}
.tag{display:inline-block;padding:2px 10px;border-radius:10px;
     font-size:11px;font-weight:600;white-space:nowrap}
.tag-prop{background:#dbeafe;color:#1d4ed8}
.tag-block{background:#f3e8ff;color:#7c3aed}
.mention-date{font-size:11px;color:#9ca3af;white-space:nowrap;padding-top:2px;min-width:160px;text-align:right}
.done-msg{font-size:13px;color:#10b981;font-weight:600;margin-top:8px}
.empty-msg{color:#9ca3af;font-size:13px;margin-top:8px}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <div class="topbar-label">Task Health Report</div>
    <h1>Scanning for <span id="user-name">…</span></h1>
    <div class="topbar-meta" id="scan-status">Starting…</div>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <a href="/mentions" class="nav-btn">↩ Change user</a>
    <a href="/" class="nav-btn">Dashboard</a>
  </div>
</div>
<div class="progress-wrap"><div class="progress-fill" id="prog"></div></div>
<div class="content">
  <div class="found-bar" id="found-bar"></div>
  <div id="results"></div>
</div>
<script>
var _total=0, _found=0;
function setUserName(n){
  document.getElementById('user-name').textContent=n;
  document.title='Scanning: '+n;
}
var _mode='';
function setMode(m){ _mode=m; }
function setTotal(n){
  _total=n;
  document.getElementById('scan-status').textContent=
    'Scanning 0 of '+n+' pages'+(_mode?' ('+_mode+')':'')+' …';
}
function onProgress(n,title){
  document.getElementById('prog').style.width=(_total?n/_total*100:0)+'%';
  document.getElementById('scan-status').textContent='Scanning '+n+' of '+_total+': '+title;
}
function addResult(html){
  _found++;
  document.getElementById('found-bar').textContent=_found+' mention'+(_found!==1?'s':'')+ ' found so far…';
  document.getElementById('results').insertAdjacentHTML('beforeend',html);
}
function onDone(){
  document.getElementById('prog').style.width='100%';
  document.getElementById('scan-status').textContent=
    'Done — scanned '+_total+' pages.';
  var lbl=_found
    ?'<span class="done-msg">'+_found+' mention'+(_found!==1?'s':'')+' found.</span>'
    :'<span class="empty-msg">No mentions found.</span>';
  document.getElementById('found-bar').innerHTML=lbl;
}
</script>
""" + "<!--" + " " * 4096 + "-->"


def _render_mention_card(result: dict) -> str:
    tags = "".join(
        f'<span class="tag tag-prop">{p}</span>'
        for p in result.get("prop_matches", [])
    )
    if result.get("block_match"):
        tags += '<span class="tag tag-block">@mentioned in content</span>'
    return (
        f'<div class="mention-card">'
        f'<div class="mention-main">'
        f'<a class="mention-title" href="{result["url"]}" target="_blank">{result["title"]}</a>'
        f'<div class="mention-tags">{tags}</div>'
        f'</div>'
        f'<div class="mention-date">{result["last_edited"]}</div>'
        f'</div>'
    )


def _render_mentions_picker(users: list) -> str:
    options = '<option value="">— Select a user —</option>' + "".join(
        f'<option value="{u["id"]}">{u["name"]}</option>'
        for u in users
    )
    no_users = "" if users else '<p style="color:#dc2626;font-size:13px;margin-top:8px">Could not load users — check NOTION_TOKEN.</p>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Find Mentions</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f3f4f6;color:#111827;font-size:14px}}
.topbar{{background:#111827;color:#fff;padding:18px 32px;
        display:flex;align-items:center;justify-content:space-between}}
.topbar-label{{font-size:11px;font-weight:600;color:#6b7280;
              text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}}
.topbar h1{{font-size:20px;font-weight:800}}
.nav-btn{{background:#374151;color:#fff;border:none;border-radius:6px;
          padding:7px 14px;font-size:12px;cursor:pointer;text-decoration:none;
          font-family:inherit}}
.nav-btn:hover{{background:#4b5563}}
.content{{padding:28px 32px;max-width:480px}}
.card{{background:#fff;border-radius:10px;padding:24px;
       box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.card h2{{font-size:15px;font-weight:700;margin-bottom:6px}}
.card p{{font-size:12px;color:#6b7280;margin-bottom:20px;line-height:1.5}}
.picker-form{{display:flex;flex-direction:column;gap:12px}}
.user-select{{padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;
              font-size:14px;font-family:inherit;background:#fff;outline:none}}
.user-select:focus{{border-color:#6b7280;box-shadow:0 0 0 3px rgba(107,114,128,.12)}}
.scan-btn{{padding:11px 20px;background:#111827;color:#fff;border:none;border-radius:8px;
           font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}}
.scan-btn:hover{{background:#374151}}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <div class="topbar-label">Task Health Report</div>
    <h1>Find Mentions</h1>
  </div>
  <button onclick="history.length>1?history.back():location.href='/'" class="nav-btn">↩ Dashboard</button>
</div>
<div class="content">
  <div class="card">
    <h2>Select a user to scan for</h2>
    <p>Scans every accessible page — checks property assignments and @mentions in content.<br>
       <strong>Guests won't appear in the dropdown</strong> — use the name field below instead.</p>
    <form class="picker-form" method="get" action="/mentions">
      <label style="font-size:12px;font-weight:600;color:#374151">Workspace member</label>
      <select class="user-select" name="user_id">
        {options}
      </select>
      <div style="text-align:center;font-size:11px;color:#9ca3af">— or —</div>
      <label style="font-size:12px;font-weight:600;color:#374151">Guest / any name
        <span style="font-weight:400;color:#9ca3af;margin-left:4px">(case-insensitive, partial match)</span>
      </label>
      <input class="user-select" type="text" name="user_name"
             placeholder="Type display name…" style="cursor:text">
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;
                    font-weight:400;color:#374151;cursor:pointer">
        <input type="checkbox" name="deep" value="1" style="width:14px;height:14px">
        Also scan page content for @mentions
        <span style="color:#9ca3af">(slower — fetches every page's blocks)</span>
      </label>
      <button class="scan-btn" type="submit">Scan for mentions →</button>
    </form>
    {no_users}
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/mentions")
def mentions():
    user_id     = request.args.get("user_id", "").strip()
    user_name   = request.args.get("user_name", "").strip()
    check_blocks = request.args.get("deep") == "1"
    token       = os.environ.get("NOTION_TOKEN", "")

    if not user_id and not user_name:
        try:
            users = get_users(token)
        except Exception as exc:
            log.error("Failed to fetch users: %s", exc)
            users = []
        return _render_mentions_picker(users)

    def generate():
        yield _MENTIONS_SHELL

        # Resolve display name for the header
        display_name = user_name
        if user_id and not display_name:
            try:
                users     = get_users(token)
                display_name = next((u["name"] for u in users if u["id"] == user_id), user_id)
            except Exception:
                display_name = user_id
        yield f'<script>setUserName({json.dumps(display_name)});</script>\n'
        mode = "properties + blocks" if check_blocks else "properties only"
        yield f'<script>setMode({json.dumps(mode)});</script>\n'

        for event in scan_user_mentions(
            token, user_id=user_id, user_name=user_name, check_blocks=check_blocks
        ):
            if event["type"] == "total":
                yield f'<script>setTotal({event["total"]});</script>\n'
            elif event["type"] == "progress":
                yield (
                    f'<script>onProgress({event["current"]},'
                    f'{json.dumps(event["title"])});</script>\n'
                )
            elif event["type"] == "result":
                card = _render_mention_card(event)
                safe = json.dumps(card).replace("</", "<\\/")
                yield f'<script>addResult({safe});</script>\n'
            elif event["type"] == "done":
                yield '<script>onDone();</script>\n'

        yield '</body></html>\n'

    return Response(stream_with_context(generate()), mimetype="text/html")


@app.route("/send-report", methods=["POST"])
def send_report():
    token = os.environ.get("NOTION_TOKEN", "")

    # Load app config (sender, global recipients, Azure creds)
    if not _APP_CONFIG_FILE.exists():
        return {"ok": False, "msg": "config.json not found"}, 400
    try:
        cfg = json.loads(_APP_CONFIG_FILE.read_text())
    except Exception as exc:
        return {"ok": False, "msg": f"config.json invalid: {exc}"}, 400

    missing_env = [k for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
                   if not os.environ.get(k)]
    if missing_env:
        return {"ok": False, "msg": f"Missing env vars: {', '.join(missing_env)}"}, 400

    # Load databases with per-DB recipient overrides
    try:
        entries = json.loads(_DB_CONFIG_FILE.read_text())
    except Exception as exc:
        return {"ok": False, "msg": f"databases.json invalid: {exc}"}, 400

    databases = [
        (os.getenv(e.get("env_var", "")), e.get("fields", {}), e.get("recipient_emails", []))
        for e in entries
        if os.getenv(e.get("env_var", ""))
    ]
    if not databases:
        return {"ok": False, "msg": "No databases resolved from databases.json"}, 400

    mail_kwargs = dict(
        tenant_id     = os.environ["AZURE_TENANT_ID"],
        client_id     = os.environ["AZURE_CLIENT_ID"],
        client_secret = os.environ["AZURE_CLIENT_SECRET"],
        sender_email  = cfg["sender_email"],
    )

    sent, errors = 0, []
    for db_id, fields, db_recipients in databases:
        try:
            report = get_task_report(token, db_id, fields)
        except Exception as exc:
            errors.append(f"Query failed ({db_id[:8]}…): {exc}")
            continue
        recipients = db_recipients or cfg["recipient_emails"]
        try:
            send_reminder_email(**mail_kwargs, recipient_emails=recipients, report=report)
            sent += 1
            log.info("[%s] Email sent via dashboard trigger.", report["db_name"])
        except Exception as exc:
            errors.append(f"Email failed ({report['db_name']}): {exc}")

    if errors and sent == 0:
        return {"ok": False, "msg": "; ".join(errors)}, 500

    msg = f"Sent {sent} email(s)."
    if errors:
        msg += " Partial errors: " + "; ".join(errors)
    return {"ok": True, "msg": msg}, 200


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
        safe_json = json.dumps(html).replace("</", "<\\/")
        yield f'<script>showReport({safe_json});</script>\n'
        yield '</body></html>'

    return Response(stream_with_context(generate()), mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
