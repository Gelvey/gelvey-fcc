#!/bin/bash
# Free Claude Code Launcher (macOS / Linux, kitty edition)
#
# Opens a kitty window with 3 tabs: MCP Router, FCC Server, FCC Claude.
# Runs a preflight kitty window for remote sync / auto-update before
# opening the main tabs. No patch overlay needed — the fork has all
# customisations merged directly.
#
# Requires: kitty, uv, git, kitten (ships with kitty)

REPO_DIR="$HOME/gelvey-fcc"
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

# ── Dependency check (kitty only — needed for preflight window) ─────────────
if ! command -v kitty &> /dev/null; then
    notify critical "FCC Launcher" "Error: 'kitty' is not installed"
    exit 1
fi

# ── Make sure the repo is cloned ──────────────────────────────────────────────
if [ ! -d "$REPO_DIR" ]; then
    git clone "$FORK_URL" "$REPO_DIR" || {
        notify critical "FCC Launcher" "Failed to clone repository"
        exit 1
    }
fi

# ── Preflight sync check (runs in its own kitty window) ──────────────────────
# Fetches origin, shows recent commits, and offers a force-pull. Runs
# interactively in a kitty window so the prompt renders correctly even
# when the launcher itself is piped or launched from a .desktop file.
PREFLIGHT_SCRIPT=$(mktemp /tmp/fcc-preflight-XXXXXX.sh)
cat > "$PREFLIGHT_SCRIPT" <<'PREFLIGHT_EOF'
#!/bin/bash
REPO_DIR="$HOME/gelvey-fcc"
cd "$REPO_DIR" || exit 1

echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║        FCC Launcher — Preflight Sync Check               ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""

if ! git remote get-url origin >/dev/null 2>&1; then
    echo "[fcc] No origin remote configured — skipping sync check"
    sleep 2
    exit 0
fi

ORIGIN_URL=$(git remote get-url origin)
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")

echo "  Remote : $ORIGIN_URL"
echo "  Branch : $CURRENT_BRANCH"
echo ""

if ! git fetch origin --quiet 2>/dev/null; then
    echo "  ⚠ Could not reach remote — continuing with local checkout"
    sleep 2
    exit 0
fi

LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null)
REMOTE_HEAD=$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null || git rev-parse origin/main 2>/dev/null)

if [ -n "$LOCAL_HEAD" ] && [ -n "$REMOTE_HEAD" ] && [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
    NEW_COUNT=$(git rev-list --count "$LOCAL_HEAD..$REMOTE_HEAD" 2>/dev/null || echo "?")
    echo "  ⚠ $NEW_COUNT new commit(s) available on remote"
    echo ""

    # Show the last 10 commits on origin
    git log "origin/$CURRENT_BRANCH" --oneline --decorate=short \
        -10 --format="    %C(yellow)%h%C(reset) %C(dim)%ar%C(reset) %s" 2>/dev/null \
        || git log origin/main --oneline --decorate=short \
            -10 --format="    %C(yellow)%h%C(reset) %C(dim)%ar%C(reset) %s" 2>/dev/null
    echo ""

    echo "  ─────────────────────────────────────────────────────────"
    echo "  ⚠  WARNING: Force-pull will DISCARD all local changes"
    echo "     and reset to the remote state."
    echo ""
    printf "  Pull latest state of gelvey-fcc? [y/N] "
    read -r REPLY
    echo ""

    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        echo "[fcc] Force-pulling latest state from origin..."
        if git reset --hard "origin/$CURRENT_BRANCH" 2>/dev/null \
                || git reset --hard origin/main 2>/dev/null; then
            echo "[fcc] ✓ Local checkout reset to $(git rev-parse --short HEAD)"
        else
            echo "[fcc] ERROR: Force-pull failed"
        fi
    else
        echo "[fcc] Skipping pull — continuing with local checkout"
    fi
else
    echo "  ✓ Local checkout is already up to date"
fi

echo ""
echo "[fcc] Preflight complete — launching FCC..."
sleep 1
PREFLIGHT_EOF
chmod +x "$PREFLIGHT_SCRIPT"

kitty \
    --listen-on "unix:$SOCKET" \
    --override "allow_remote_control=socket-only" \
    --title "FCC — Preflight Sync" \
    --override "initial_window_width=680" \
    --override "initial_window_height=420" \
    bash "$PREFLIGHT_SCRIPT" \
    && rm -f "$PREFLIGHT_SCRIPT" \
    || { rm -f "$PREFLIGHT_SCRIPT"; echo "[fcc] Pre-flight kitty exited with error"; }

# ── Dependency check (remaining deps — after preflight) ──────────────────────
# fcc-server and fcc-claude are launched via `uv run` from the repo so the
# latest source is always used — only `uv` and `git` need to be on PATH.
for cmd in uv git; do
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

cd "$REPO_DIR" || {
    notify critical "FCC Launcher" "Cannot cd to $REPO_DIR"
    exit 1
}

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
    bash -c "echo '=== FCC Claude (waiting ${FCC_CLIENT_WARMUP_S:-5}s for fcc-server) ===' && sleep ${FCC_CLIENT_WARMUP_S:-5} && uv run fcc-claude; exec bash" &

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

# Tab 2: FCC Server (run from repo via uv so the latest source is always used)
spawn_tab "FCC Server" bash -c "echo '=== FCC Server ===' && uv run fcc-server; exec bash"

# The first kitty window already has the FCC Claude tab.

TABS_OPENED=2  # FCC Claude + FCC Server always open
if [ -x "$MCP_SCRIPT" ] && command -v npx >/dev/null 2>&1 \
        && command -v socat >/dev/null 2>&1 \
        && command -v jq >/dev/null 2>&1 \
        && command -v uv >/dev/null 2>&1; then
    TABS_OPENED=$((TABS_OPENED + 1))
fi

notify normal "FCC Launcher" \
    "$TABS_OPENED FCC tab(s) opened (MCP / Server / Claude)" \
    2>/dev/null || true

activate_window "FCC" || true

echo "FCC tabs opened (MCP / Server / Claude)."
exit 0
