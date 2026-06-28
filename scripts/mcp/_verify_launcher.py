#!/usr/bin/env python3
"""One-shot end-to-end launcher verifier.

Starts start_mcp.sh fully detached, polls its log for the new
"✅ self-test passed" line, then runs --self-test-only against the
just-started daemon to confirm the 4 control tools come back, then
stops everything. Prints a concise PASS/FAIL summary.
"""

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

LOG = "/tmp/launcher-verify.log"
SOCK = os.path.expanduser("~/.mcp-router/sockets/router.sock")
# Resolve companion scripts relative to this file so the verifier works for
# anybody who cloned the repo.
_SCRIPT_DIR = Path(__file__).resolve().parent
STOP = str(_SCRIPT_DIR / "stop_mcp.sh")
START = str(_SCRIPT_DIR / "start_mcp.sh")
TEST = str(_SCRIPT_DIR / "_test_e2e.py")


def cleanup():
    subprocess.run([STOP], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "mcp_router.py"], capture_output=True)
    for p in (LOG, SOCK):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(p)


def start_launcher():
    cleanup()
    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"
    proc = subprocess.Popen(
        [START],
        cwd=str(_SCRIPT_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=open(LOG, "w"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc


def wait_for_self_test_line(timeout_s: float = 150.0) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not os.path.exists(LOG):
            time.sleep(1.0)
            continue
        text = Path(LOG).read_text(errors="replace")
        for line in text.splitlines():
            if "self-test passed" in line:
                return line
            if "FATAL" in line and "self-test" in line:
                return None
        time.sleep(1.0)
    return None


def main():
    print("=== launching start_mcp.sh detached ===")
    launcher = start_launcher()
    print(f"launcher pid={launcher.pid}, waiting for self-test line...")
    success_line = wait_for_self_test_line(timeout_s=150.0)
    if not success_line:
        print("\nFAIL: did not find 'self-test passed' line within 150s")
        if os.path.exists(LOG):
            print(
                f"\n--- log tail ---\n{Path(LOG).read_text(errors='replace')[-2000:]}"
            )
        cleanup()
        sys.exit(1)
    print(f"\nFOUND: {success_line}")

    # Confirm socket is live, then run --self-test-only to verify the 4 tools.
    if not os.path.exists(SOCK):
        print(f"FAIL: socket {SOCK} missing after self-test pass")
        cleanup()
        sys.exit(1)
    print("\n=== verifying 4 control tools via --self-test-only ===")
    result = subprocess.run(
        ["uv", "run", "python", TEST, "--self-test-only", "--socket", SOCK],
        cwd=str(_SCRIPT_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(result.stdout[-1000:] if result.stdout else "(no stdout)")
    if result.returncode != 0:
        print(f"FAIL: --self-test-only exited {result.returncode}")
        print(f"stderr: {result.stderr[-500:]}")
        cleanup()
        sys.exit(1)

    print("\n=== PASS: launcher self-test line + 4 control tools verified ===")
    cleanup()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cleanup()
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        cleanup()
        sys.exit(1)
