#!/bin/bash
# Free Claude Code Launcher (macOS / Linux, kitty edition)
#
# Opens a kitty window with 3 tabs: MCP Router, FCC Server, FCC Claude.
# Does a quick git pull on the fork before starting so the local checkout
# stays current. No patch overlay or preflight tab needed — the fork has
# all customisations merged directly.
#
# Requires: kitty, fcc-server, fcc-claude, git, kitten (ships with kitty)

REPO_DIR="$HOME/free-claude-code"
# Override with FCC_FORK_URL=<url> to track a different remote.
# Default points to the publicly-published gelvey-fcc fork.
FORK_URL="${FCC_FORK_URL:-https://github.com/Gelvey/gelvey-fcc}"
SOCKET="${XDG_RUNTIME_DIR:-/tmp}/fcc-kitty-$$-$(date +%s%N 2>/dev/null || date +%s).sock"

# ── Portable helpers (macOS + Linux) ─────────────────────────────────────────
# Desktop notification: Linux uses notify-send, macOS uses osascript.
# pgrep -x Finder guards against headless macOS (CI, servers) where
# osascript hangs without a window server.
notify() {
    local urgency="$1" title="$2" body="$3"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -u "$urgency" "$title" "$body"
    elif command -v osascript >/dev/null 2>&1 && pgrep -x Finder >/dev/null; then
        # Escape double-quotes and backslashes for AppleScript string literals.
        local escaped_title escaped_body
        escaped_title=$(printf '%s' "$title" | sed 's/\\/\\\\/g; s/"/\\"/g')
        escaped_body=$(printf '%s' "$body" | sed 's/\\/\\\\/g; s/"/\\"/g')
        osascript -e "display notification \"${escaped_body}\" with title \"${escaped_title}\""
    else
        echo "[$title] $body" >&2
    fi
}

# Bring a window to the front by title.
activate_window() {
    if command -v wmctrl >/dev/null 2>&1; then
        wmctrl -a "$1" 2>/dev/null
    elif command -v xdotool >/dev/null 2>&1; then
        xdotool search --name "$1" windowactivate 2>/dev/null
    elif command -v osascript >/dev/null 2>&1; then
        osascript -e "tell application \"kitty\" to activate" 2>/dev/null
    fi
}

# ── Dependency check ──────────────────────────────────────────────────────────
for cmd in kitty fcc-server fcc-claude git; do
    if ! command -v "$cmd" &> /dev/null; then
        notify critical "FCC Launcher" "Error: '$cmd' is not installed"
        exit 1
    fi
done

# MCP stack dependencies (only required if the user is using the meta-router)
MCP_SCRIPT="$REPO_DIR/scripts/mcp/start_mcp.sh"
if [ -x "$MCP_SCRIPT" ]; then
    for cmd in npx socat jq uv; do
        if ! command -v "$cmd" &> /dev/null; then
            notify critical "FCC Launcher" "MCP stack enabled but '$cmd' is not installed"
            exit 1
        fi
    done
fi

# ── Make sure the repo is cloned ──────────────────────────────────────────────
if [ ! -d "$REPO_DIR" ]; then
    git clone "$FORK_URL" "$REPO_DIR" || {
        notify critical "FCC Launcher" "Failed to clone repository"
        exit 1
    }
fi

# ── Quick git pull to stay current (non-blocking — continues on failure) ─────
cd "$REPO_DIR" || {
    notify critical "FCC Launcher" "Cannot cd to $REPO_DIR"
    exit 1
}
git pull --ff-only 2>&1 || echo "[fcc] WARNING: git pull failed, continuing with local checkout"

# ── Restore MCP config if missing (e.g. after a fresh clone) ──────────────────
MCP_CONFIG_REAL="$REPO_DIR/scripts/mcp/mcp_config.json"
MCP_CONFIG_BACKUP="$HOME/.fcc/mcp_config.json"
MCP_CONFIG_EXAMPLE="$REPO_DIR/scripts/mcp/mcp_config.example.json"
if [ ! -f "$MCP_CONFIG_REAL" ]; then
    if [ -f "$MCP_CONFIG_BACKUP" ]; then
        cp "$MCP_CONFIG_BACKUP" "$MCP_CONFIG_REAL"
        chmod 600 "$MCP_CONFIG_REAL"
        echo "[fcc] Restored mcp_config.json from backup"
    elif [ -f "$MCP_CONFIG_EXAMPLE" ]; then
        cp "$MCP_CONFIG_EXAMPLE" "$MCP_CONFIG_REAL"
        chmod 600 "$MCP_CONFIG_REAL"
        echo "[fcc] WARNING: copied mcp_config.example.json — edit with real secrets"
    fi
fi

# ── Open kitty with the 3 FCC tabs ────────────────────────────────────────────
kitty \
    --listen-on "unix:$SOCKET" \
    --override "allow_remote_control=socket-only" \
    --title "FCC" \
    bash -c "echo '=== FCC Claude (waiting ${FCC_CLIENT_WARMUP_S:-5}s for fcc-server) ===' && sleep ${FCC_CLIENT_WARMUP_S:-5} && fcc-claude; exec bash" &

KITTY_PID=$!
sleep 1
if ! kill -0 "$KITTY_PID" 2>/dev/null; then
    notify critical "FCC Launcher" "kitty failed to start"
    exit 1
fi

# Spawn the other 2 tabs into the same kitty window
spawn_tab() {
    local title="$1"; shift
    local _err
    _err=$(mktemp 2>/dev/null) || return 1
    if kitten @ --to "unix:$SOCKET" launch --type=tab --tab-title="$title" -- "$@" 2>"$_err"; then
        rm -f "$_err"
    else
        echo "[fcc] WARN: failed to spawn $title tab:"; cat "$_err"; rm -f "$_err"
    fi
}

# Tab 1: MCP Router (only if start_mcp.sh exists and deps are present)
if [ -x "$MCP_SCRIPT" ] && command -v npx >/dev/null 2>&1 \
        && command -v socat >/dev/null 2>&1 \
        && command -v jq >/dev/null 2>&1 \
        && command -v uv >/dev/null 2>&1; then
    spawn_tab "MCP Router" bash -c "
        echo '=== MCP Router ==='
        $MCP_SCRIPT
        rc=\$?
        echo
        echo \"--- start_mcp.sh exited with code \$rc ---\"
        if [ \$rc -ne 0 ]; then
            echo 'ERROR: start_mcp.sh failed. Check ~/.mcp-router/logs/ for details.'
        fi
        exec bash
    "
else
    echo "[fcc] MCP Router tab skipped (start_mcp.sh missing or deps not on PATH)"
fi

# Tab 2: FCC Server
if command -v fcc-server >/dev/null 2>&1; then
    spawn_tab "FCC Server" bash -c "echo '=== FCC Server ===' && fcc-server; exec bash"
else
    echo "[fcc] fcc-server not on PATH; skipping server tab"
fi

# The first kitty window already has the FCC Claude tab.

TABS_OPENED=1  # FCC Claude tab always opens first
if [ -x "$MCP_SCRIPT" ] && command -v npx >/dev/null 2>&1 \
        && command -v socat >/dev/null 2>&1 \
        && command -v jq >/dev/null 2>&1 \
        && command -v uv >/dev/null 2>&1; then
    TABS_OPENED=$((TABS_OPENED + 1))
fi
if command -v fcc-server >/dev/null 2>&1; then
    TABS_OPENED=$((TABS_OPENED + 1))
fi

notify normal "FCC Launcher" \
    "$TABS_OPENED FCC tab(s) opened (MCP / Server / Claude)" \
    2>/dev/null || true

activate_window "FCC" || true

echo "FCC tabs opened (MCP / Server / Claude)."
exit 0
