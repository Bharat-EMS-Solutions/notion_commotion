#!/usr/bin/env bash
# demo.sh — spin up the stack and fire the daily digest demo email
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
FLASK_PORT=5000

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()  { echo -e "${GREEN}✔${NC}  $*"; }
err() { echo -e "${RED}✘${NC}  $*"; exit 1; }
hdr() { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# ── 1. Ensure Flask is running ───────────────────────────────────────────────
hdr "=== Daily Digest Demo ==="

if ! pgrep -f "python3 app.py" >/dev/null; then
    echo "  Starting Flask..."
    cd "$APP_DIR"
    nohup python3 app.py >/tmp/flask.log 2>&1 &
    sleep 2
fi
curl -sf "http://localhost:$FLASK_PORT/" -o /dev/null \
    || err "Flask failed to start — check /tmp/flask.log"
ok "Flask running on port $FLASK_PORT"

# ── 2. Ensure tunnel is running ──────────────────────────────────────────────
CF_LOG="/tmp/cf_tunnel.log"

if ! pgrep -f "cloudflared tunnel" >/dev/null; then
    echo "  Starting Cloudflare tunnel..."
    > "$CF_LOG"
    nohup cloudflared tunnel --url "http://localhost:$FLASK_PORT" \
        --no-autoupdate >"$CF_LOG" 2>&1 &
    for i in $(seq 1 15); do
        sleep 2
        TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$CF_LOG" | tail -1)
        [ -n "$TUNNEL_URL" ] && break
    done
else
    TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | tail -1)
fi

[ -n "${TUNNEL_URL:-}" ] || err "Could not get tunnel URL — run ./tunnel.sh to diagnose"
ok "Tunnel live: $TUNNEL_URL"

# Update .env if URL changed
OLD_URL=$(grep '^APP_BASE_URL=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- || true)
if [ "$OLD_URL" != "$TUNNEL_URL" ]; then
    sed -i "s|^APP_BASE_URL=.*|APP_BASE_URL=$TUNNEL_URL|" "$APP_DIR/.env"
    echo -e "  ${YELLOW}⚠  Tunnel URL changed — update the OAM provider Target URL:${NC}"
    echo -e "     https://outlook.office.com/connectors/oam/publish"
    echo -e "     New URL: ${CYAN}$TUNNEL_URL${NC}"
fi

# ── 3. Send demo email ───────────────────────────────────────────────────────
hdr "Sending demo email..."

cd "$APP_DIR"
python3 - <<PYEOF
import json, os, sys
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(".env"))

import requests as _req
from notion_client import get_in_progress_tasks_by_owner
from mailer import build_owner_digest_email, _acquire_token

token   = os.environ["NOTION_TOKEN"]
db_id   = os.environ.get("NOTION_DATABASE_2_ID") or os.environ.get("NOTION_DATABASE_ID")
cfg     = json.loads(Path("config.json").read_text())
entries = json.loads(Path("databases.json").read_text())
fields  = next(e["fields"] for e in entries if e.get("env_var") and os.getenv(e["env_var"]) == db_id)
today   = date.today().strftime("%d %b %Y")

by_owner  = get_in_progress_tasks_by_owner(token, db_id, fields, "Tasks Tracker")
all_tasks = [t for info in by_owner.values() for t in info["tasks"]][:5]

if not all_tasks:
    print("No in-progress tasks found — check Notion.")
    sys.exit(1)

digest = build_owner_digest_email(
    owner_name    = "Demo",
    tasks         = all_tasks,
    app_base_url  = os.environ["APP_BASE_URL"],
    today_str     = today,
    originator_id = os.environ.get("ACTIONABLE_MSG_ORIGINATOR", ""),
)

ms_token = _acquire_token(
    os.environ["AZURE_TENANT_ID"],
    os.environ["AZURE_CLIENT_ID"],
    os.environ["AZURE_CLIENT_SECRET"],
)
resp = _req.post(
    f"https://graph.microsoft.com/v1.0/users/{cfg['sender_email']}/sendMail",
    headers={"Authorization": f"Bearer {ms_token}", "Content-Type": "application/json"},
    json={"message": {
        "subject": f"[DEMO] Daily Hours Log — {today}",
        "body": {"contentType": "HTML", "content": digest["html"]},
        "toRecipients": [{"emailAddress": {"address": cfg["sender_email"]}}],
    }},
)
resp.raise_for_status()
print(f"TASKS={len(all_tasks)}")
for t in all_tasks:
    print(f"  - {t['name']}")
PYEOF

# ── 4. Demo summary ──────────────────────────────────────────────────────────
hdr "Demo ready"
echo -e "  ${BOLD}Email sent to:${NC}   $(grep sender_email "$APP_DIR/config.json" | grep -o '"[^"]*@[^"]*"' | tr -d '"')"
echo -e "  ${BOLD}Subject:${NC}         [DEMO] Daily Hours Log — $(date '+%d %b %Y')"
echo -e "  ${BOLD}Preview page:${NC}    ${CYAN}${TUNNEL_URL}/preview-digest${NC}"
echo -e "  ${BOLD}Action endpoint:${NC} ${CYAN}${TUNNEL_URL}/log-hours-action${NC}"
echo ""
echo -e "  ${BOLD}Demo flow:${NC}"
echo -e "   1. Open ${BOLD}[DEMO] Daily Hours Log${NC} in Outlook"
echo -e "   2. Fill in hours for each task → click ${BOLD}Submit All Hours${NC}"
echo -e "   3. Confirmation email arrives within seconds"
echo -e "   4. Run ${CYAN}cat $APP_DIR/hours_log.csv${NC} to show the log live"
echo ""
