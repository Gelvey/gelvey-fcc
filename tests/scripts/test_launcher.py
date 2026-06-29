import os
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _script_text() -> str:
    return (_repo_root() / "scripts" / "fcc-launcher.sh").read_text(encoding="utf-8")


def _braced_body(text: str, declaration: str) -> str:
    """Extract the braced body of a shell function."""
    start = text.index(declaration)
    brace_start = text.index("{", start)
    depth = 0

    for index, char in enumerate(text[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : index]

    raise AssertionError(f"Unclosed function body for {declaration}")


def _run_launcher(*env_overrides: str) -> subprocess.CompletedProcess[str]:
    """Run fcc-launcher.sh and return the result.

    Strips kitty/fcc-* from PATH so the script exits early (no real launch).
    """
    sh = _repo_root() / "scripts" / "fcc-launcher.sh"
    # Build a minimal PATH that excludes kitty and fcc-* binaries.
    minimal_path = "/usr/bin:/bin"
    env = {**os.environ, "PATH": minimal_path}
    for override in env_overrides:
        if "=" in override:
            key, val = override.split("=", 1)
            env[key] = val
    return subprocess.run(
        ["bash", str(sh)],
        cwd=_repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ── Static content / structure checks ──────────────────────────────────────


def test_launcher_sh_is_valid_bash() -> None:
    """fcc-launcher.sh passes bash -n syntax check."""
    script = _repo_root() / "scripts" / "fcc-launcher.sh"
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_launcher_sh_uses_home_not_hardcoded_path() -> None:
    """REPO_DIR uses $HOME, not /home/$USER."""
    text = _script_text()
    assert 'REPO_DIR="$HOME/free-claude-code"' in text
    assert 'REPO_DIR="/home/' not in text


def test_launcher_sh_has_portable_notify_function() -> None:
    """notify() function handles notify-send, osascript, and echo fallback."""
    text = _script_text()
    body = _braced_body(text, "notify()")

    assert "notify-send" in body
    assert "osascript" in body
    assert "display notification" in body
    assert "echo" in body  # fallback


def test_launcher_sh_has_portable_activate_window_function() -> None:
    """activate_window() handles wmctrl, xdotool, and osascript."""
    text = _script_text()
    body = _braced_body(text, "activate_window()")

    assert "wmctrl" in body
    assert "xdotool" in body
    assert "osascript" in body
    assert 'tell application \\"kitty\\" to activate' in body


def test_launcher_sh_checks_core_dependencies() -> None:
    """Script checks for kitty, fcc-server, fcc-claude, and git."""
    text = _script_text()
    assert "kitty" in text
    assert "fcc-server" in text
    assert "fcc-claude" in text
    assert "git" in text


def test_launcher_sh_checks_mcp_dependencies() -> None:
    """Script checks for npx, socat, jq, uv when MCP script exists."""
    text = _script_text()
    assert "npx" in text
    assert "socat" in text
    assert "jq" in text
    # uv is checked both in MCP deps and general PATH
    assert text.count("uv") >= 2


def test_launcher_sh_restores_mcp_config_from_backup() -> None:
    """Script restores mcp_config.json from backup or example on fresh clone."""
    text = _script_text()
    assert "mcp_config.json" in text
    assert "mcp_config.example.json" in text
    assert "Restored mcp_config.json from backup" in text
    assert "copied mcp_config.example.json" in text


def test_launcher_sh_kitty_listen_on_socket() -> None:
    """Script launches kitty with --listen-on unix socket."""
    text = _script_text()
    assert "--listen-on" in text
    assert "unix:" in text
    assert "--override" in text
    assert "allow_remote_control=socket-only" in text


def test_launcher_sh_spawns_three_tabs() -> None:
    """Script mentions all 3 tabs: MCP Router, FCC Server, FCC Claude."""
    text = _script_text()
    assert "MCP Router" in text
    assert "FCC Server" in text
    assert "FCC Claude" in text
    # Tab counting logic
    assert "TABS_OPENED" in text


def test_launcher_sh_warmup_for_fcc_server() -> None:
    """FCC Claude tab waits for fcc-server before launching fcc-claude."""
    text = _script_text()
    assert "FCC_CLIENT_WARMUP_S" in text
    assert "waiting" in text.lower() and "fcc-server" in text.lower()


def test_launcher_sh_git_pull_before_start() -> None:
    """Script does a git pull --ff-only before launching."""
    text = _script_text()
    assert "git pull --ff-only" in text


def test_launcher_sh_fork_url_configurable() -> None:
    """FORK_URL can be overridden via FCC_FORK_URL env var."""
    text = _script_text()
    assert "FCC_FORK_URL" in text
    assert "Gelvey/gelvey-fcc" in text


# ── Runtime exit-behaviour tests ───────────────────────────────────────────


def test_launcher_sh_exits_when_kitty_missing() -> None:
    """Script exits with non-zero when kitty is not on PATH.

    The error message may go through notify-send or osascript depending on
    what's available; in headless CI those commands silently swallow output,
    so we only check exit code, not text content.
    """
    result = _run_launcher()
    assert result.returncode != 0, (
        f"Expected non-zero exit when kitty missing, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_launcher_sh_clones_repo_when_missing() -> None:
    """Script attempts git clone when REPO_DIR does not exist."""
    # This is a static check: verify the clone logic exists.
    text = _script_text()
    assert "git clone" in text
    assert "REPO_DIR" in text
    assert 'if [ ! -d "$REPO_DIR" ]' in text
