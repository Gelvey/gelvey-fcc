from __future__ import annotations

import json
from types import SimpleNamespace

from cli.adapters.base import CliParseState, CliTaskRequest
from cli.adapters.claude import CLAUDE_CLI_ADAPTER
from cli.adapters.registry import DEFAULT_CLIENT_CLI_ID, get_client_cli_adapter


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "workspace_path": "/workspace",
        "api_url": "http://127.0.0.1:8082/v1",
        "allowed_dirs": [],
        "plans_directory": None,
        "claude_bin": "claude-test",
        "auth_token": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_registry_returns_default_claude_adapter() -> None:
    assert DEFAULT_CLIENT_CLI_ID == "claude"
    assert get_client_cli_adapter() is CLAUDE_CLI_ADAPTER
    assert get_client_cli_adapter("claude") is CLAUDE_CLI_ADAPTER


def test_claude_adapter_builds_new_task_command_and_env() -> None:
    invocation = CLAUDE_CLI_ADAPTER.build_task_invocation(
        config=_config(auth_token="proxy-token"),
        request=CliTaskRequest(prompt="hello"),
        base_env={
            "KEEP_ME": "yes",
            "ANTHROPIC_API_KEY": "official-key",
            "ANTHROPIC_AUTH_TOKEN": "stale-token",
        },
    )

    assert invocation.argv == (
        "claude-test",
        "-p",
        "hello",
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--verbose",
    )
    assert invocation.env["KEEP_ME"] == "yes"
    assert invocation.env["ANTHROPIC_API_URL"] == "http://127.0.0.1:8082/v1"
    assert invocation.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8082"
    assert invocation.env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    assert invocation.env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert invocation.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert "ANTHROPIC_API_KEY" not in invocation.env
    assert invocation.trace_metadata["client_cli_id"] == "claude"


def test_claude_adapter_builds_resume_fork_command() -> None:
    invocation = CLAUDE_CLI_ADAPTER.build_task_invocation(
        config=_config(),
        request=CliTaskRequest(
            prompt="continue",
            session_id="sess_123",
            fork_session=True,
        ),
        base_env={},
    )

    assert invocation.argv[:4] == (
        "claude-test",
        "--resume",
        "sess_123",
        "--fork-session",
    )
    assert "-p" in invocation.argv
    assert "continue" in invocation.argv
    assert invocation.trace_metadata["resume_session_id"] == "sess_123"
    assert invocation.trace_metadata["fork_session"] is True


def test_claude_adapter_does_not_resume_pending_session() -> None:
    invocation = CLAUDE_CLI_ADAPTER.build_task_invocation(
        config=_config(),
        request=CliTaskRequest(prompt="new", session_id="pending_123"),
        base_env={},
    )

    assert "--resume" not in invocation.argv
    assert invocation.trace_metadata["resume_session_id"] is None


def test_claude_adapter_adds_allowed_dirs_and_plans_directory() -> None:
    invocation = CLAUDE_CLI_ADAPTER.build_task_invocation(
        config=_config(
            allowed_dirs=["/dir1", "/dir2"],
            plans_directory="./agent_workspace/plans",
        ),
        request=CliTaskRequest(prompt="hello"),
        base_env={},
    )

    assert invocation.argv.count("--add-dir") == 2
    assert "/dir1" in invocation.argv
    assert "/dir2" in invocation.argv
    settings_idx = invocation.argv.index("--settings")
    settings = json.loads(invocation.argv[settings_idx + 1])
    assert settings["plansDirectory"] == "./agent_workspace/plans"


def test_claude_adapter_launcher_env_targets_proxy() -> None:
    env = CLAUDE_CLI_ADAPTER.build_launcher_env(
        proxy_root_url="http://127.0.0.1:9191",
        auth_token=" proxy-token ",
        base_env={
            "PATH": "keep",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_API_KEY": "official-key",
            "ANTHROPIC_AUTH_TOKEN": "stale-token",
        },
    )

    assert env["PATH"] == "keep"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9191"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_adapter_extracts_supported_session_id_shapes() -> None:
    assert CLAUDE_CLI_ADAPTER.extract_session_id({"session_id": "direct"}) == "direct"
    assert CLAUDE_CLI_ADAPTER.extract_session_id({"sessionId": "camel"}) == "camel"
    assert (
        CLAUDE_CLI_ADAPTER.extract_session_id({"init": {"session_id": "nested"}})
        == "nested"
    )
    assert (
        CLAUDE_CLI_ADAPTER.extract_session_id({"result": {"sessionId": "result"}})
        == "result"
    )
    assert (
        CLAUDE_CLI_ADAPTER.extract_session_id({"conversation": {"id": "conv"}})
        == "conv"
    )
    assert CLAUDE_CLI_ADAPTER.extract_session_id({"type": "message"}) is None
    assert CLAUDE_CLI_ADAPTER.extract_session_id("not a dict") is None


def test_claude_adapter_invalid_stdout_json_becomes_raw_event() -> None:
    events = list(
        CLAUDE_CLI_ADAPTER.parse_stdout_line(
            "Not valid json",
            CliParseState(log_raw_cli_diagnostics=False),
        )
    )

    assert events == [{"type": "raw", "content": "Not valid json"}]


def test_claude_adapter_synthesizes_session_info_once() -> None:
    state = CliParseState()

    first_events = list(
        CLAUDE_CLI_ADAPTER.parse_stdout_line('{"session_id": "sess_1"}', state)
    )
    second_events = list(
        CLAUDE_CLI_ADAPTER.parse_stdout_line('{"session_id": "sess_2"}', state)
    )

    assert first_events == [
        {"type": "session_info", "session_id": "sess_1"},
        {"session_id": "sess_1"},
    ]
    assert second_events == [{"session_id": "sess_2"}]
