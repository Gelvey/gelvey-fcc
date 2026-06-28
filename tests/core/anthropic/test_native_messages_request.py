"""Tests for the OpenRouter provider-options resolver and body-builder wiring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.anthropic.native_messages_request import (
    build_openrouter_native_request_body,
    resolve_openrouter_provider_options,
)
from providers.exceptions import InvalidRequestError
from providers.open_router.request import build_request_body


def _settings(
    *,
    data_collection: str = "deny",
    free_data_collection: str = "allow",
    free_model_ids: frozenset[str] | str = frozenset(),
) -> SimpleNamespace:
    """Lightweight stand-in for :class:`config.settings.Settings`.

    Satisfies the structural :class:`OpenRouterPolicySettings` Protocol that the
    resolver and body builder accept, so we don't have to instantiate the full
    Pydantic Settings model (which reads ``.env`` files and has many required
    fields with non-trivial defaults).
    """
    if isinstance(free_model_ids, str):
        free_model_ids = frozenset(
            part.strip() for part in free_model_ids.split(",") if part.strip()
        )
    return SimpleNamespace(
        open_router_data_collection=data_collection,
        open_router_free_data_collection=free_data_collection,
        open_router_free_model_ids=free_model_ids,
    )


class _FakeRequest:
    """Lightweight stand-in for an Anthropic Messages request."""

    extra_body: Any

    def __init__(self, **fields: Any) -> None:
        defaults: dict[str, Any] = {
            "model": "anthropic/claude-3.5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 64,
            "system": None,
            "tools": None,
            "tool_choice": None,
            "metadata": None,
            "stop_sequences": None,
            "temperature": None,
            "top_p": None,
            "top_k": None,
            "thinking": None,
            "stream": True,
            "extra_body": None,
        }
        defaults.update(fields)
        for key, value in defaults.items():
            setattr(self, key, value)


def _request(**fields: Any) -> _FakeRequest:
    return _FakeRequest(**fields)


# ---------- resolve_openrouter_provider_options ----------


def test_resolver_default_deny_for_paid_model() -> None:
    settings = _settings()
    out = resolve_openrouter_provider_options("anthropic/claude-3.5-sonnet", settings)
    assert out == {"provider": {"data_collection": "deny"}}


def test_resolver_allow_for_allowlisted_free_model() -> None:
    settings = _settings(
        free_model_ids="meta-llama/llama-3.3-70b-instruct:free,deepseek/deepseek-chat:free"
    )
    out = resolve_openrouter_provider_options(
        "meta-llama/llama-3.3-70b-instruct:free", settings
    )
    assert out == {"provider": {"data_collection": "allow"}}


def test_resolver_falls_through_to_default_when_model_not_allowlisted() -> None:
    settings = _settings(data_collection="allow", free_data_collection="allow")
    out = resolve_openrouter_provider_options("anthropic/claude-3.5-sonnet", settings)
    assert out == {"provider": {"data_collection": "allow"}}


def test_resolver_respects_explicit_free_data_collection_deny() -> None:
    settings = _settings(
        free_data_collection="deny",
        free_model_ids="meta-llama/llama-3.3-70b-instruct:free",
    )
    out = resolve_openrouter_provider_options(
        "meta-llama/llama-3.3-70b-instruct:free", settings
    )
    # Operator explicitly chose deny for free models -> deny wins even on the allowlist.
    assert out == {"provider": {"data_collection": "deny"}}


def test_resolver_empty_allowlist_falls_through_to_default() -> None:
    settings = _settings(free_model_ids=frozenset())
    out = resolve_openrouter_provider_options("anything:free", settings)
    assert out == {"provider": {"data_collection": "deny"}}


# ---------- build_openrouter_native_request_body: provider-options kw ----------


def test_body_includes_provider_data_collection_default_deny() -> None:
    settings = _settings()
    body = build_openrouter_native_request_body(
        _request(),
        thinking_enabled=False,
        default_max_tokens=1024,
        openrouter_provider_options=resolve_openrouter_provider_options(
            "anthropic/claude-3.5-sonnet", settings
        ),
    )
    assert body["provider"] == {"data_collection": "deny"}


def test_body_includes_provider_data_collection_allow_for_free_model() -> None:
    settings = _settings(
        free_model_ids="meta-llama/llama-3.3-70b-instruct:free",
    )
    body = build_openrouter_native_request_body(
        _request(model="meta-llama/llama-3.3-70b-instruct:free"),
        thinking_enabled=False,
        default_max_tokens=1024,
        openrouter_provider_options=resolve_openrouter_provider_options(
            "meta-llama/llama-3.3-70b-instruct:free", settings
        ),
    )
    assert body["provider"] == {"data_collection": "allow"}


def test_body_server_wins_over_extra_body_provider() -> None:
    """A client's extra_body.provider must NOT override the gateway's data_collection.

    Other client-supplied provider.* keys (e.g. ``order``) must survive the
    deep-merge so legitimate per-request options are preserved.
    """
    settings = _settings()
    req = _request()
    req.extra_body = {"provider": {"data_collection": "allow", "order": ["anthropic"]}}
    body = build_openrouter_native_request_body(
        req,
        thinking_enabled=False,
        default_max_tokens=1024,
        openrouter_provider_options=resolve_openrouter_provider_options(
            "anthropic/claude-3.5-sonnet", settings
        ),
    )
    # Server-wins: data_collection is deny even though extra_body asked for allow.
    assert body["provider"]["data_collection"] == "deny"
    # Non-conflicting keys from extra_body survive the deep-merge at the top level.
    assert body["provider"]["order"] == ["anthropic"]


def test_body_omits_provider_when_options_omitted() -> None:
    """Existing call sites that don't pass openrouter_provider_options are unchanged."""
    body = build_openrouter_native_request_body(
        _request(),
        thinking_enabled=False,
        default_max_tokens=1024,
    )
    assert "provider" not in body


# ---------- providers/open_router/request.py build_request_body ----------


def test_build_request_body_injects_provider_policy_from_settings() -> None:
    settings = _settings(
        free_model_ids="meta-llama/llama-3.3-70b-instruct:free",
    )
    req = _request(model="meta-llama/llama-3.3-70b-instruct:free")
    body = build_request_body(req, thinking_enabled=False, settings=settings)
    assert body["provider"] == {"data_collection": "allow"}


def test_build_request_body_injects_deny_for_paid_model() -> None:
    settings = _settings()
    req = _request(model="anthropic/claude-3.5-sonnet")
    body = build_request_body(req, thinking_enabled=False, settings=settings)
    assert body["provider"] == {"data_collection": "deny"}


def test_build_request_body_without_settings_omits_provider() -> None:
    """Backward-compat: existing tests that call build_request_body without settings still work."""
    body = build_request_body(_request(), thinking_enabled=False, settings=None)
    assert "provider" not in body


def test_build_request_body_still_rejects_reserved_extra_body_keys() -> None:
    settings = _settings()
    req = _request(model="anthropic/claude-3.5-sonnet")
    req.extra_body = {"model": "hijack"}
    with pytest.raises(InvalidRequestError, match="model"):
        build_request_body(req, thinking_enabled=False, settings=settings)
