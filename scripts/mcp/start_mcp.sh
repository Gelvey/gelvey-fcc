#!/usr/bin/env bash
# Start the MCP meta-router stack:
#   - one supergateway per stdio backend (stdio -> http://127.0.0.1:PORT/sse)
#   - socat bridging a Unix socket to the meta-router's stdio
#
# Lifecycle: this script is meant to be the *first command* in a dedicated
# kitty tab opened by fcc-launcher.sh. When that tab (or the whole kitty
# window) closes, the tab's process group is killed and this script + all
# children die with it. Nothing is enabled at boot; nothing persists.
#
# Implementation note: supergateway uses `child_process.spawn(stdioCmd,
# { shell: true })` internally — it hands the --stdio value to /bin/sh.
# Passing a multi-word command string here causes shell-mangling on some
# setups (the command ends up being prefixed with `uv `). We side-step
# that by writing a one-line wrapper script per backend that exports
# the env vars and `exec`s the real command, and pass the wrapper's path
# as --stdio. The shell then runs a single, unambiguous filename.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/mcp_config.json"
STOP_SCRIPT="$SCRIPT_DIR/stop_mcp.sh"

STATE_DIR="$HOME/.mcp-router"
RUN_DIR="$STATE_DIR/run"
LOG_DIR="$STATE_DIR/logs"
SOCK_DIR="$STATE_DIR/sockets"
mkdir -p "$RUN_DIR" "$LOG_DIR" "$SOCK_DIR"

export PATH="$HOME/.local/bin:$PATH"

# -- stop any previous run cleanly ----------------------------------------
bash "$STOP_SCRIPT" --quiet || true

# -- sanity checks --------------------------------------------------------
for cmd in npx socat jq uv; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "FATAL: $cmd not found in PATH" >&2
        exit 1
    fi
done
[ -f "$CONFIG" ] || { echo "FATAL: $CONFIG not found" >&2; exit 1; }

# -- read config ---------------------------------------------------------
# Portable read of JSON array keys (bash 3.2+ / zsh compatible).
# mapfile requires bash 4+ (not available on macOS stock bash 3.2).
SERVER_NAMES=()
while IFS= read -r line; do
    SERVER_NAMES+=("$line")
done < <(jq -r '.servers | keys[]' "$CONFIG")
SOCKET_PATH=$(jq -r '.router_socket' "$CONFIG")
ROUTER_PIDFILE=$(jq -r '.router_pidfile' "$CONFIG")
ROUTER_LOG=$(jq -r '.router_log' "$CONFIG")
HEALTH_TIMEOUT_S=$(jq -r '.health_timeout_s' "$CONFIG")

# Expand ~/ and literal $HOME in config values. mcp_config.example.json uses
# ~/... so users can copy it as-is, and advanced users sometimes write
# $HOME/... — bash does not perform either expansion inside the quoted
# strings we read from jq, so do it here. Pure string substitution (no
# eval) so a hostile config file can't execute code.
expand_path() {
    case "$1" in
        "~"|"~/"*) printf '%s' "$HOME${1#~}" ;;
        *) printf '%s' "${1//\$HOME/$HOME}" ;;
    esac
}
SOCKET_PATH=$(expand_path "$SOCKET_PATH")
ROUTER_PIDFILE=$(expand_path "$ROUTER_PIDFILE")
ROUTER_LOG=$(expand_path "$ROUTER_LOG")

# Portable timeout: macOS lacks GNU timeout from coreutils.
# Runs a command with a deadline in seconds. Returns the command's exit
# code, or 124 (matching GNU timeout convention) on deadline expiry.
_timeout() {
    local seconds="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
        return $?
    fi
    # Fallback: run in background, kill after deadline.
    "$@" &
    local pid=$!
    (sleep "$seconds" && kill "$pid" 2>/dev/null) &
    local killer=$!
    wait "$pid" 2>/dev/null
    local rc=$?
    if kill -0 "$killer" 2>/dev/null; then
        # Killer still alive → command finished before deadline.
        kill "$killer" 2>/dev/null
        wait "$killer" 2>/dev/null
        return $rc
    fi
    # Killer already dead → deadline expired and killed the command.
    wait "$killer" 2>/dev/null
    return 124
}

# -- health-check helpers -----------------------------------------------
wait_for_health() {
    local name="$1" port="$2"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
    while [ $SECONDS -lt $deadline ]; do
        if curl --silent --fail --max-time 1 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# -- start one supergateway per stdio backend -----------------------------
echo "[mcp] configuring supergateways for ${#SERVER_NAMES[@]} backend(s)..."
for name in "${SERVER_NAMES[@]}"; do
    type=$(jq -r ".servers[\"$name\"].type" "$CONFIG")
    port=$(jq -r ".servers[\"$name\"].port" "$CONFIG")
    logfile="$LOG_DIR/${name}.log"
    pidfile="$RUN_DIR/${name}.pid"

    if [ "$type" = "sse" ]; then
        echo "[mcp] $name: type=sse, will connect directly to remote $(jq -r ".servers[\"$name\"].url" "$CONFIG")"
        # No supergateway needed for remote SSE backends.
        host=$(jq -r ".servers[\"$name\"].url" "$CONFIG" | sed -E 's|^https?://||; s|/.*$||; s|:[0-9]+$||')
        if ! _timeout 3 bash -c "echo > /dev/tcp/$host/$port" 2>/dev/null; then
            echo "[mcp]   warn: cannot reach $host:$port (will fail at activation time)"
        fi
        continue
    fi

    cmd=$(jq -r ".servers[\"$name\"].command" "$CONFIG")
    # Portable read of JSON arrays (bash 3.2+ / zsh compatible).
    ARGS=()
    while IFS= read -r line; do
        ARGS+=("$line")
    done < <(jq -r ".servers[\"$name\"].args[]" "$CONFIG")
    ENV_KV=()
    while IFS= read -r line; do
        ENV_KV+=("$line")
    done < <(jq -r ".servers[\"$name\"].env // {} | to_entries[] | \"\(.key)=\\(.value)\"" "$CONFIG")

    # Write env vars to a separate file (no shell-quoting needed) and a
    # wrapper script that sources it then execs the real command. The
    # wrapper is a single, unambiguous path — supergateway's shell
    # invocation never has to parse a multi-word command.
    envfile="$RUN_DIR/${name}.env"
    wrapper="$RUN_DIR/${name}.sh"
    {
        echo "# Auto-generated env file for $name"
        printf 'export %q\n' "${ENV_KV[@]}"
    } > "$envfile"
    chmod 600 "$envfile"
    {
        echo "#!/usr/bin/env bash"
        echo "# Auto-generated by start_mcp.sh — do not edit."
        echo "source '${envfile}'"
        printf 'exec %q' "$cmd"
        for arg in "${ARGS[@]}"; do
            printf ' %q' "$arg"
        done
        echo
    } > "$wrapper"
    chmod 700 "$wrapper"

    echo "[mcp] $name: spawning supergateway on port $port (env: ${#ENV_KV[@]} var(s))"
    (
        cd "$HOME"
        npx -y supergateway \
            --stdio "$wrapper" \
            --port "$port" \
            --baseUrl "http://127.0.0.1:$port" \
            --logLevel info \
            --healthEndpoint "/healthz" \
            >"$logfile" 2>&1 &
        echo $! > "$pidfile"
    )
done

# -- health-check the stdio supergateways ---------------------------------
echo "[mcp] waiting up to ${HEALTH_TIMEOUT_S}s for supergateways to be healthy..."
fails=()
for name in "${SERVER_NAMES[@]}"; do
    type=$(jq -r ".servers[\"$name\"].type" "$CONFIG")
    if [ "$type" = "sse" ]; then continue; fi
    port=$(jq -r ".servers[\"$name\"].port" "$CONFIG")
    if ! wait_for_health "$name" "$port"; then
        fails+=("$name:$port")
    fi
done
if [ ${#fails[@]} -ne 0 ]; then
    echo "[mcp] FATAL: supergateways not healthy after ${HEALTH_TIMEOUT_S}s: ${fails[*]}" >&2
    echo "[mcp] check logs: $LOG_DIR/<name>.log" >&2
    for name in "${SERVER_NAMES[@]}"; do
        type=$(jq -r ".servers[\"$name\"].type" "$CONFIG")
        if [ "$type" = "sse" ]; then continue; fi
        echo "[mcp]   --- $name log tail ---" >&2
        tail -n 20 "$LOG_DIR/${name}.log" >&2 || true
    done
    bash "$STOP_SCRIPT" --quiet || true
    exit 1
fi

# -- start socat bridge to meta-router -----------------------------------
rm -f "$SOCKET_PATH"

echo "[mcp] starting meta-router daemon at $SOCKET_PATH"
# The meta-router is now a persistent Unix-socket daemon. One process
# handles all client connections — no per-connection fork, no per-
# connection uv-run / import overhead. start_mcp.sh stays in `wait`
# below so the tab stays alive; the tab's process group is killed when
# it closes, which terminates the meta-router and the supergateways.
cd "$SCRIPT_DIR"
uv run --directory "$SCRIPT_DIR" \
    python mcp_router.py \
    --config "$CONFIG" \
    --socket "$SOCKET_PATH" \
    --log "$ROUTER_LOG" \
    >"$LOG_DIR/router-stdout.log" 2>&1 &
ROUTER_PID=$!
echo "$ROUTER_PID" > "$RUN_DIR/router.pid"
cd "$HOME"

# Wait for the meta-router to bind the socket (it imports MCP SDK + creates
# listeners, so allow a few seconds).
for ((i = 1; i <= 30; i++)); do
    if [ -S "$SOCKET_PATH" ] && kill -0 "$ROUTER_PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done
if [ ! -S "$SOCKET_PATH" ]; then
    echo "[mcp] FATAL: meta-router did not create $SOCKET_PATH" >&2
    tail -n 30 "$LOG_DIR/router-stdout.log" >&2 || true
    bash "$STOP_SCRIPT" --quiet || true
    exit 1
fi

echo
echo "[mcp] ✅ ready."
echo "[mcp]    meta-router socket:  $SOCKET_PATH"
echo "[mcp]    supergateways:        $(printf '%s, ' "${SERVER_NAMES[@]}" | sed 's/, $//')"
echo "[mcp]    logs:                $LOG_DIR"
echo "[mcp]    stop with:           $STOP_SCRIPT"
echo "[mcp]    (or just close this kitty tab)"
echo

# -- post-startup self-test ------------------------------------------------
# Run the end-to-end test against the just-started daemon to catch SDK or
# router regressions that would otherwise silently surface as "tools fetch
# failed" in fcc-claude. The test connects, sends initialize + tools/list
# (the same path mcp-proxy-tool uses), and asserts all 4 control tools
# come back. Failure here tears the stack down so the launcher exits
# loudly instead of handing the user a broken MCP.
echo "[mcp] running post-startup self-test..."
if ! uv run --directory "$SCRIPT_DIR" \
        python _test_e2e.py --self-test-only --socket "$SOCKET_PATH"; then
    echo "[mcp] FATAL: post-startup self-test failed; tearing down." >&2
    echo "[mcp] --- last 40 lines of router log ---" >&2
    tail -n 40 "$ROUTER_LOG" >&2 || true
    bash "$STOP_SCRIPT" --quiet || true
    exit 1
fi
echo "[mcp] ✅ self-test passed at $(date -u '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date -u '+%Y-%m-%dT%H:%M:%S') (socket=$SOCKET_PATH)."
echo

# Stay in foreground so the tab stays alive. We block on the meta-
# router's PID (a direct child of this shell). When the meta-router
# dies — or when the tab closes and SIGHUPs us — this script exits,
# the tab's process group is killed, and all supergateways die with it.
wait "$ROUTER_PID"
