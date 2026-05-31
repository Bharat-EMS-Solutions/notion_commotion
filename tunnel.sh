#!/usr/bin/env bash
# tunnel.sh — diagnostics and relaunch for the named Cloudflare tunnel
set -euo pipefail

FLASK_PORT=5000
TUNNEL_NAME="notion-digest"
PUBLIC_URL="https://notion-digest.bharatelectrodrives.com"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
FLASK_LOG="/tmp/flask.log"
CF_LOG="/tmp/cf_tunnel.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✔${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✘${NC}  $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

flask_pid()  { pgrep -f "python3 app.py" | head -1 || true; }
tunnel_pid() { pgrep -f "cloudflared tunnel run" | head -1 || true; }

# ── diagnostics ──────────────────────────────────────────────────────────────
hdr "=== Tunnel Diagnostics ==="

# cloudflared
if command -v cloudflared &>/dev/null; then
    ok "cloudflared $(cloudflared --version 2>&1 | head -1)"
else
    err "cloudflared not found in PATH — reinstall via apt"
fi

# Flask process
fpid=$(flask_pid)
[[ -n "$fpid" ]] && ok "Flask running (PID $fpid)" || warn "Flask is NOT running"

# Flask local response
if curl -sf "http://localhost:$FLASK_PORT/" -o /dev/null 2>/dev/null; then
    ok "Flask responding on port $FLASK_PORT"
else
    warn "Flask not responding on port $FLASK_PORT"
fi

# Tunnel process
tpid=$(tunnel_pid)
[[ -n "$tpid" ]] && ok "Tunnel running (PID $tpid)" || warn "Tunnel is NOT running"

# Public reachability (skip on WSL — its DNS may not resolve the stable domain)
if curl -sf "$PUBLIC_URL/" -o /dev/null --max-time 8 2>/dev/null; then
    ok "Publicly reachable: $PUBLIC_URL"
else
    warn "Cannot reach $PUBLIC_URL from WSL (normal — test in Windows browser)"
fi

echo ""

# ── all healthy ──────────────────────────────────────────────────────────────
if [[ -n "$fpid" && -n "$tpid" ]]; then
    echo -e "${GREEN}${BOLD}Everything looks healthy.${NC}"
    echo -e "  Dashboard:   ${CYAN}$PUBLIC_URL${NC}"
    echo -e "  Preview:     ${CYAN}$PUBLIC_URL/preview-digest${NC}"
    exit 0
fi

# ── relaunch ─────────────────────────────────────────────────────────────────
echo -e "${YELLOW}One or more services are down. Relaunch?${NC}"
read -rp "  [y/N] " confirm
[[ "${confirm,,}" != "y" ]] && { echo "Aborted."; exit 0; }

# Flask
if [[ -z "$fpid" ]]; then
    pkill -f "python3 app.py" 2>/dev/null || true
    hdr "Starting Flask..."
    cd "$APP_DIR"
    nohup python3 app.py >"$FLASK_LOG" 2>&1 &
    sleep 2
    curl -sf "http://localhost:$FLASK_PORT/" -o /dev/null \
        && ok "Flask is up" \
        || { err "Flask failed — check $FLASK_LOG"; tail -20 "$FLASK_LOG"; exit 1; }
fi

# Named tunnel
if [[ -z "$tpid" ]]; then
    pkill -f "cloudflared tunnel run" 2>/dev/null || true
    hdr "Starting tunnel ($TUNNEL_NAME)..."
    nohup cloudflared tunnel run "$TUNNEL_NAME" >"$CF_LOG" 2>&1 &
    sleep 4
    if grep -q "Registered tunnel connection" "$CF_LOG" 2>/dev/null; then
        ok "Tunnel connected"
    else
        warn "Tunnel starting — check $CF_LOG if issues persist"
    fi
fi

echo ""
ok "All services running."
echo -e "  ${BOLD}URL:${NC} ${CYAN}$PUBLIC_URL${NC}"
