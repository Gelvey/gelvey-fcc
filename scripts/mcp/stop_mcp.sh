#!/usr/bin/env bash
# Stop the MCP meta-router stack started by start_mcp.sh
# Idempotent — safe to run when nothing is running.

set -u

QUIET=0
if [ "${1:-}" = "--quiet" ]; then
    QUIET=1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/mcp_config.json"
STATE_DIR="$HOME/.mcp-router"
RUN_DIR="$STATE_DIR/run"

log() {
    [ $QUIET -eq 1 ] || echo "[stop] $*"
}

if ! command -v jq >/dev/null 2>&1; then
    log "jq not available; skipping config-driven shutdown"
    exit 0
fi
if [ -f "$CONFIG" ]; then
    SERVERS_JSON=$(jq -c '.servers' "$CONFIG")
    SOCKET_PATH=$(jq -r '.router_socket' "$CONFIG")

    # Stop supergateways
    for name in $(echo "$SERVERS_JSON" | jq -r 'keys[]'); do
        pidfile="$RUN_DIR/${name}.pid"
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile" 2>/dev/null || true)
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                log "killing supergateway $name (pid $pid)"
                kill "$pid" 2>/dev/null || true
                # npx may have spawned a child; reap
                pkill -P "$pid" 2>/dev/null || true
            fi
            rm -f "$pidfile"
        fi
    done

    # Stop socat
    socat_pidfile="$RUN_DIR/socat.pid"
    if [ -f "$socat_pidfile" ]; then
        pid=$(cat "$socat_pidfile" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log "killing socat (pid $pid)"
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$socat_pidfile"
    fi

    # Clean up the socket and any orphan meta-router processes
    if [ -n "${SOCKET_PATH:-}" ] && [ -S "$SOCKET_PATH" ]; then
        rm -f "$SOCKET_PATH"
    fi
fi

# Belt and suspenders: kill any stragglers by name
pkill -f "mcp_router.py" 2>/dev/null || true
pkill -f "supergateway"  2>/dev/null || true

log "done."
