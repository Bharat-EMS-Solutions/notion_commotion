#!/usr/bin/env bash
# tunnel.sh — diagnostics, relaunch, and .env update for the Cloudflare tunnel
set -euo pipefail

FLASK_PORT=5000
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$APP_DIR/.env"
CF_LOG="/tmp/cf_tunnel.log"
FLASK_LOG="/tmp/flask.log"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✔${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✘${NC}  $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# ── helpers ─────────────────────────────────────────────────────────────────
flask_pid()  { pgrep -f "python3 app.py" | head -1 || true; }
tunnel_pid() { pgrep -f "cloudflared tunnel" | head -1 || true; }

current_url() {
    grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | tail -1 || true
}

env_url() {
    grep '^APP_BASE_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true
}

update_env_url() {
    local new_url="$1"
    if grep -q '^APP_BASE_URL=' "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^APP_BASE_URL=.*|APP_BASE_URL=$new_url|" "$ENV_FILE"
    else
        echo "APP_BASE_URL=$new_url" >> "$ENV_FILE"
    fi
}

# ── diagnostics ─────────────────────────────────────────────────────────────
run_diagnostics() {
    hdr "=== Tunnel Diagnostics ==="

    # 1. cloudflared binary
    if command -v cloudflared &>/dev/null; then
        ok "cloudflared $(cloudflared --version 2>&1 | head -1)"
    else
        err "cloudflared not found in PATH"
    fi

    # 2. Flask process
    local fpid; fpid=$(flask_pid)
    if [[ -n "$fpid" ]]; then
        ok "Flask running (PID $fpid)"
    else
        warn "Flask is NOT running"
    fi

    # 3. Flask reachable locally
    if curl -sf "http://localhost:$FLASK_PORT/" -o /dev/null 2>/dev/null; then
        ok "Flask responding on port $FLASK_PORT"
    else
        warn "Flask not responding on port $FLASK_PORT"
    fi

    # 4. Tunnel process
    local tpid; tpid=$(tunnel_pid)
    if [[ -n "$tpid" ]]; then
        ok "cloudflared tunnel running (PID $tpid)"
    else
        warn "Tunnel is NOT running"
    fi

    # 5. Current tunnel URL
    local url; url=$(current_url)
    if [[ -n "$url" ]]; then
        ok "Tunnel URL: $url"
        # 6. Tunnel reachable from internet
        if curl -sf "$url/" -o /dev/null --max-time 15 --retry 2 --retry-delay 3 2>/dev/null; then
            ok "Tunnel publicly reachable"
        else
            warn "Tunnel URL exists but not responding publicly (Cloudflare may still be warming up)"
        fi
    else
        warn "No tunnel URL found in $CF_LOG"
    fi

    # 7. .env APP_BASE_URL vs live URL
    local saved; saved=$(env_url)
    if [[ -z "$saved" ]]; then
        warn "APP_BASE_URL not set in .env"
    elif [[ -n "$url" && "$saved" != "$url" ]]; then
        warn ".env APP_BASE_URL ($saved) differs from live URL ($url)"
    elif [[ -n "$saved" ]]; then
        ok ".env APP_BASE_URL matches live URL"
    fi

    echo ""
}

# ── start Flask ──────────────────────────────────────────────────────────────
start_flask() {
    hdr "Starting Flask..."
    cd "$APP_DIR"
    nohup python3 app.py >"$FLASK_LOG" 2>&1 &
    local fpid=$!
    echo -e "  Flask PID: $fpid"
    sleep 2
    if curl -sf "http://localhost:$FLASK_PORT/" -o /dev/null 2>/dev/null; then
        ok "Flask is up"
    else
        err "Flask failed to start — check $FLASK_LOG"
        tail -20 "$FLASK_LOG"
        exit 1
    fi
}

# ── start tunnel ─────────────────────────────────────────────────────────────
start_tunnel() {
    hdr "Starting Cloudflare tunnel..."
    > "$CF_LOG"
    nohup cloudflared tunnel --url "http://localhost:$FLASK_PORT" --no-autoupdate \
        >"$CF_LOG" 2>&1 &
    echo -e "  Waiting for URL assignment..."

    local url=""
    for i in $(seq 1 20); do
        sleep 2
        url=$(current_url)
        [[ -n "$url" ]] && break
        echo -ne "  ...${i}s\r"
    done

    if [[ -z "$url" ]]; then
        err "Tunnel started but no URL appeared after 40s"
        err "Last log lines:"
        tail -10 "$CF_LOG"
        exit 1
    fi

    ok "Tunnel live: $url"

    # Update .env
    local old_url; old_url=$(env_url)
    update_env_url "$url"
    ok "Updated APP_BASE_URL in .env"

    # Warn if URL changed (provider registration needs updating)
    if [[ -n "$old_url" && "$old_url" != "$url" ]]; then
        echo ""
        warn "${BOLD}URL has changed from the last session!${NC}"
        warn "You must update the Target URL in the Actionable Messages provider:"
        warn "  1. Go to https://outlook.office.com/connectors/oam/publish"
        warn "  2. Edit 'Notion Hours Digest'"
        warn "  3. Replace Target URL with: ${CYAN}$url${NC}"
        warn "  4. Save — propagation takes up to 1 hour"
        echo ""
    fi

    echo -e "\n${BOLD}Preview URL:${NC} ${CYAN}$url/preview-digest${NC}"
    echo -e "${BOLD}Action endpoint:${NC} ${CYAN}$url/log-hours-action${NC}"
}

# ── main ────────────────────────────────────────────────────────────────────
run_diagnostics

fpid=$(flask_pid)
tpid=$(tunnel_pid)

if [[ -n "$fpid" && -n "$tpid" ]]; then
    url=$(current_url)
    echo -e "${GREEN}${BOLD}Everything looks healthy.${NC}"
    [[ -n "$url" ]] && echo -e "Live at: ${CYAN}$url${NC}"
    exit 0
fi

# Something is down — ask before relaunching
echo -e "${YELLOW}One or more services are down. Relaunch?${NC}"
read -rp "  [y/N] " confirm
[[ "${confirm,,}" != "y" ]] && { echo "Aborted."; exit 0; }

# Kill any partial survivors first
[[ -n "$fpid" ]] || { warn "Stopping stale Flask...";  pkill -f "python3 app.py" 2>/dev/null || true; sleep 1; }
[[ -n "$tpid" ]] || { warn "Stopping stale tunnel..."; pkill -f "cloudflared tunnel" 2>/dev/null || true; sleep 1; }

[[ -z "$fpid" ]] && start_flask
[[ -z "$tpid" ]] && start_tunnel

echo ""
ok "All services running."
