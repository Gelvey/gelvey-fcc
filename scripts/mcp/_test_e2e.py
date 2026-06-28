#!/usr/bin/env python3
"""End-to-end test for the MCP router.

Two modes:
  1. Default (no args): start a fresh daemon on a test socket, run the
     self-test, assert all 4 control tools come back, kill the daemon,
     clean up. Use for interactive/manual regression testing.
  2. ``--self-test-only --socket <path>``: connect to an already-running
     daemon at <path> and run the self-test only. Use as a post-startup
     self-test from start_mcp.sh so any SDK or router regression fails
     the launcher loudly.

The self-test exercises the same path fcc-claude uses via mcp-proxy-tool:
send ``initialize`` (the SDK now runs in stateless mode, so the client is
not required to send ``notifications/initialized``), then ``tools/list``,
and assert the response contains all 4 control tools. A 4th check
activates a real supergateway via ``use_server`` to catch supergateway
regressions (bad port, broken wrapper, env not exported) that the
control-tool checks would miss.
"""

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TEST_SOCK = os.path.expanduser("~/.mcp-router/sockets/router-test-e2e.sock")
TEST_LOG = "/tmp/router-test-e2e.log"
# Resolve relative to this file so the tester works from any clone of the repo.
SCRIPT_DIR = str(Path(__file__).resolve().parent)

EXPECTED_TOOLS = (
    "list_servers",
    "use_server",
    "list_active_servers",
    "deactivate_server",
)
# Cheap stdio backend used for the 4th self-test check (use_server +
# tools/list). Has only 1 env var, so supergateway startup is fast.
GATEWAY_PROBE_BACKEND = "resend"
SOCKET_TIMEOUT_S = 15.0  # cold-start of `uv run` + SDK import can take 5-10s
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0


def _check(cond: bool, msg: str) -> None:
    """Assert helper that survives ``python -O`` (bare ``assert`` is stripped)."""
    if not cond:
        raise AssertionError(msg)


def _send_messages(s: socket.socket, messages: list[dict]) -> None:
    for m in messages:
        s.sendall((json.dumps(m) + "\n").encode())
        time.sleep(0.2)


def _recv_lines(s: socket.socket, want_newlines: int, settle_s: float = 1.0) -> str:
    """Read from the socket until we have at least ``want_newlines`` lines or it times out."""
    time.sleep(settle_s)
    buf = b""
    try:
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
            if buf.count(b"\n") >= want_newlines:
                break
    except TimeoutError:
        pass
    return buf.decode("utf-8", errors="replace")


def _connect_and_handshake(
    sock_path: str,
    with_initialized: bool,
    extra_requests: list[dict] | None = None,
) -> str:
    """Connect to the router, send initialize + tools/list (+ optional extras), return the raw response.

    Raises a distinct exception per failure mode so start_mcp.sh's router-log
    dump correlates with the right root cause:
      * ``ConnectionRefusedError`` — daemon not listening / wrong path
      * ``socket.timeout`` — daemon accepting but not responding (wedged)
      * ``AssertionError`` — daemon responded but payload is wrong
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(SOCKET_TIMEOUT_S)
        s.connect(sock_path)
    except ConnectionRefusedError as exc:
        raise ConnectionRefusedError(
            f"daemon not listening on {sock_path}: {exc}"
        ) from exc
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"socket path does not exist: {sock_path}") from exc
    try:
        messages: list[dict] = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "self-test", "version": "0"},
                },
            },
        ]
        if with_initialized:
            messages.append({"jsonrpc": "2.0", "method": "notifications/initialized"})
        messages.append({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        if extra_requests:
            messages.extend(extra_requests)
        try:
            _send_messages(s, messages)
            want_newlines = 2 + (2 * len(extra_requests or []))
            return _recv_lines(s, want_newlines=want_newlines, settle_s=1.5)
        except TimeoutError as exc:
            raise TimeoutError(
                f"daemon accepted connection on {sock_path} but did not respond "
                f"within {SOCKET_TIMEOUT_S}s — event loop may be wedged"
            ) from exc
    finally:
        with contextlib.suppress(Exception):
            s.close()


def _call_tool(sock_path: str, tool_name: str, arguments: dict | None = None) -> str:
    """Send initialize + notifications/initialized + tools/call, return the raw response."""
    return _connect_and_handshake(
        sock_path,
        with_initialized=True,
        extra_requests=[
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            }
        ],
    )


def _parse_tools_call_result(resp: str, call_id: int = 99) -> dict | None:
    """Extract the parsed ``result.content[0].text`` payload from a tools/call response.

    Walks newline-delimited JSON-RPC messages in ``resp``, finds the one
    whose ``id`` matches ``call_id``, and parses the first text content as
    JSON. Returns ``None`` if the response is missing, malformed, or the
    text content is not valid JSON. This is more robust than substring
    matching (e.g. ``'"ok": true' in resp``) because it survives cosmetic
    changes in the router's response format.
    """
    for line in resp.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") != call_id or "result" not in msg:
            continue
        content = msg["result"].get("content") or []
        if not content or not isinstance(content, list):
            return None
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if not isinstance(text, str):
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def self_test(sock_path: str) -> None:
    """Run the self-test against a daemon already listening on ``sock_path``.

    Runs 4 checks, each wrapped in a 3-attempt retry loop (1s backoff) to
    survive transient startup races. Raises ``AssertionError`` with a
    human-readable message on failure. Raises ``ConnectionRefusedError``,
    ``socket.timeout``, or ``FileNotFoundError`` for distinct connection
    failure modes (survives ``python -O`` — does not use bare ``assert``).
    """

    # --- Check 1: initialize only (verify the daemon responds at all).
    def _check1() -> str:
        resp = _connect_and_handshake(sock_path, with_initialized=False)
        _check('"result"' in resp, f"no result in initialize response:\n{resp}")
        _check(
            '"protocolVersion"' in resp,
            f"no protocolVersion in initialize response:\n{resp}",
        )
        return resp

    _retry(_check1, "check 1 (initialize)")

    # --- Check 2: initialize + tools/list (the path fcc-claude uses).
    def _check2() -> str:
        resp = _connect_and_handshake(sock_path, with_initialized=False)
        _check(
            "before initialization" not in resp.lower(),
            f"SDK rejected request as uninitialized — stateless mode broken?\n{resp}",
        )
        missing = [t for t in EXPECTED_TOOLS if t not in resp]
        _check(
            not missing,
            f"control tool(s) missing from tools/list response: {missing}\n{resp}",
        )
        return resp

    _retry(_check2, "check 2 (tools/list, no init)")

    # --- Check 3: full proper handshake (defends against a future SDK
    # that re-introduces init requirements for some clients).
    def _check3() -> str:
        resp = _connect_and_handshake(sock_path, with_initialized=True)
        missing = [t for t in EXPECTED_TOOLS if t not in resp]
        _check(
            not missing,
            f"control tool(s) missing with proper handshake: {missing}\n{resp}",
        )
        return resp

    _retry(_check3, "check 3 (full handshake)")

    # --- Check 4 (best-effort): activate a real supergateway via
    # use_server, then tools/list, and assert the response includes at
    # least one tool prefixed with ``<backend>__``. Catches supergateway
    # regressions (bad port, broken wrapper, env not exported) that the
    # control-tool checks would miss. This check is best-effort: a
    # failure is logged as a WARNING (visible in the launcher output)
    # but does NOT fail the self-test, because a single broken
    # supergateway shouldn't tear down the whole launcher and force the
    # user to re-launch fcc-claude just to get the 4 control tools back.
    prefix = f"{GATEWAY_PROBE_BACKEND}__"

    def _check4() -> None:
        activate_resp = _call_tool(
            sock_path, "use_server", {"name": GATEWAY_PROBE_BACKEND}
        )
        # Parse the TextContent payload properly instead of substring
        # matching on the raw response (which is fragile to formatting
        # changes in the router).
        result = _parse_tools_call_result(activate_resp)
        _check(
            result is not None and result.get("ok") is True,
            f"use_server({GATEWAY_PROBE_BACKEND!r}) did not succeed:\n"
            f"  parsed result: {result}\n"
            f"  raw response: {activate_resp}",
        )
        # A second use_server call (already active) is a no-op success
        # — that's fine, but the tools/list below is the real check that
        # the activated backend's tools are actually registered.
        post_resp = _connect_and_handshake(sock_path, with_initialized=True)
        _check(
            prefix in post_resp,
            f"no tools prefixed with {prefix!r} after use_server; "
            f"supergateway for {GATEWAY_PROBE_BACKEND!r} may be broken.\n{post_resp}",
        )

    try:
        _retry(
            _check4, f"check 4 (use_server + tools/list for {GATEWAY_PROBE_BACKEND!r})"
        )
    except (
        TimeoutError,
        AssertionError,
        ConnectionRefusedError,
        FileNotFoundError,
    ) as exc:
        # Best-effort: log a warning and continue. The self-test still
        # returns success so the launcher doesn't tear the stack down.
        print(
            f"WARNING: supergateway probe for {GATEWAY_PROBE_BACKEND!r} failed: {exc}\n"
            f"  -> The 4 control tools are available, but activating the "
            f"{GATEWAY_PROBE_BACKEND!r} backend may not work until the issue is fixed.\n"
            f"  -> See ~/.mcp-router/logs/{GATEWAY_PROBE_BACKEND}.log for details.",
            file=sys.stderr,
        )


def _retry(fn, label: str) -> None:
    """Run ``fn()`` up to RETRY_ATTEMPTS times with RETRY_BACKOFF_S between attempts.

    Retries only on transient connection/timeout failures; assertion failures
    (i.e. the daemon responded but with a wrong payload) bubble up immediately
    so a real regression isn't papered over by retries.
    """
    transient = (ConnectionRefusedError, socket.timeout, FileNotFoundError)
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            fn()
            return
        except AssertionError:
            raise  # don't retry real assertion failures
        except transient as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S)
                continue
            break
    # Exhausted retries on a transient error — re-raise the last one.
    assert last_exc is not None  # for type-checkers
    raise last_exc


# ---------------------------------------------------------------------------
# Standalone mode: start a fresh daemon, run the test, clean up.
# ---------------------------------------------------------------------------


def _cleanup_test_artifacts():
    for p in (TEST_SOCK, TEST_LOG):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(p)
    subprocess.run(["pkill", "-f", "mcp_router.py.*router-test-e2e"], check=False)


def _start_test_daemon() -> subprocess.Popen:
    _cleanup_test_artifacts()
    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "python",
            "mcp_router.py",
            "--config",
            "mcp_config.json",
            "--socket",
            TEST_SOCK,
            "--log",
            TEST_LOG,
        ],
        cwd=SCRIPT_DIR,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(40):
        if os.path.exists(TEST_SOCK):
            time.sleep(0.5)
            return proc
        time.sleep(0.2)
    raise RuntimeError(
        f"daemon did not create socket. log:\n{Path(TEST_LOG).read_text()}"
    )


def _run_standalone() -> int:
    print("=== starting daemon ===")
    proc = _start_test_daemon()
    print(f"daemon pid={proc.pid}, socket exists={os.path.exists(TEST_SOCK)}")
    try:
        # Delegate to self_test() — it already prints, retries, and raises
        # distinct exceptions. We just translate exceptions into exit codes.
        self_test(TEST_SOCK)
        print(
            f"\n=== ALL TESTS PASSED at {datetime.now().isoformat(timespec='seconds')} ==="
        )
        return 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.5)
        _cleanup_test_artifacts()


def main() -> int:
    _doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=_doc.splitlines()[0] if _doc else "")
    parser.add_argument(
        "--self-test-only",
        action="store_true",
        help="Skip daemon startup; run the self-test against "
        "--socket. Used by start_mcp.sh.",
    )
    parser.add_argument(
        "--socket",
        help="Path to the router's Unix socket (required with --self-test-only).",
    )
    args = parser.parse_args()

    if args.self_test_only:
        if not args.socket:
            print("--self-test-only requires --socket <path>", file=sys.stderr)
            return 2
        try:
            self_test(args.socket)
        except AssertionError as e:
            print(f"SELF-TEST FAILED: {e}", file=sys.stderr)
            return 1
        except (ConnectionRefusedError, FileNotFoundError) as e:
            print(f"SELF-TEST ERROR (connection): {e}", file=sys.stderr)
            return 1
        except TimeoutError as e:
            print(f"SELF-TEST ERROR (daemon wedged): {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"SELF-TEST ERROR: {e}", file=sys.stderr)
            return 1
        return 0

    try:
        return _run_standalone()
    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        if os.path.exists(TEST_LOG):
            print(f"\n--- daemon log ---\n{Path(TEST_LOG).read_text()}")
        _cleanup_test_artifacts()
        return 1
    except (TimeoutError, ConnectionRefusedError, FileNotFoundError) as e:
        print(f"\nTEST ERROR: {e}")
        if os.path.exists(TEST_LOG):
            print(f"\n--- daemon log ---\n{Path(TEST_LOG).read_text()}")
        _cleanup_test_artifacts()
        return 1
    except Exception as e:
        print(f"\nERROR: {e}")
        if os.path.exists(TEST_LOG):
            print(f"\n--- daemon log ---\n{Path(TEST_LOG).read_text()}")
        _cleanup_test_artifacts()
        return 1


if __name__ == "__main__":
    sys.exit(main())
