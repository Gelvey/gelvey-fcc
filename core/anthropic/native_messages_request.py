"""Native Anthropic Messages request body construction (JSON-ready dicts).

Provider adapters supply policy via parameters (defaults, OpenRouter post-steps).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel


class OpenRouterPolicySettings(Protocol):
    """Structural subset of :class:`config.settings.Settings` used by the OpenRouter data-policy helpers.

    The full ``Settings`` model is not required: only the three policy fields
    below. This lets tests and small adapter shims pass a ``SimpleNamespace``
    stub without instantiating the full Pydantic model.
    """

    open_router_data_collection: str
    open_router_free_data_collection: str
    open_router_free_model_ids: frozenset[str]


_REQUEST_FIELDS = (
    "model",
    "messages",
    "system",
    "max_tokens",
    "stop_sequences",
    "stream",
    "temperature",
    "top_p",
    "top_k",
    "metadata",
    "tools",
    "tool_choice",
    "thinking",
    "context_management",
    "output_config",
    "mcp_servers",
    "extra_body",
)

# Keys that would override routed canonical request fields if merged from ``extra_body``.
_OPENROUTER_EXTRA_BODY_FORBIDDEN_KEYS = frozenset(
    {
        "model",
        "messages",
        "system",
        "tools",
        "tool_choice",
        "stream",
        "max_tokens",
        "temperature",
        "top_p",
        "top_k",
        "metadata",
        "stop_sequences",
        "context_management",
        "output_config",
        "mcp_servers",
    }
)


class OpenRouterExtraBodyError(ValueError):
    """``extra_body`` contained reserved keys that would override canonical fields."""


def validate_openrouter_extra_body(extra: Any) -> None:
    """Reject ``extra_body`` keys that must not override routed request fields."""
    if not isinstance(extra, dict) or not extra:
        return
    bad = _OPENROUTER_EXTRA_BODY_FORBIDDEN_KEYS & extra.keys()
    if bad:
        raise OpenRouterExtraBodyError(
            f"extra_body must not override canonical request fields: {sorted(bad)}"
        )


_INTERNAL_FIELDS = {
    "thinking",
    "extra_body",
}


def _serialize_value(value: Any) -> Any:
    """Convert Pydantic models and lightweight objects into JSON-ready values."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {
            key: _serialize_value(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_serialize_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "__dict__"):
        return {
            key: _serialize_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and item is not None
        }
    return value


def _dump_request_fields(request_data: Any) -> dict[str, Any]:
    """Extract the public request fields (OpenRouter-style explicit field list)."""
    if isinstance(request_data, BaseModel):
        raw = request_data.model_dump(exclude_none=True)
        return {
            field: raw[field]
            for field in _REQUEST_FIELDS
            if field in raw and raw[field] is not None
        }

    dump = getattr(request_data, "model_dump", None)
    if callable(dump):
        raw = dump(exclude_none=True)
        if isinstance(raw, dict):
            return {
                field: raw[field]
                for field in _REQUEST_FIELDS
                if field in raw and raw[field] is not None
            }

    dumped: dict[str, Any] = {}
    for field in _REQUEST_FIELDS:
        value = getattr(request_data, field, None)
        if value is not None:
            dumped[field] = _serialize_value(value)
    return dumped


def dump_raw_messages_request(request_data: Any) -> dict[str, Any]:
    """Public JSON-ready dict of Anthropic public request fields (for native adapters)."""
    return _dump_request_fields(request_data)


def sanitize_native_messages_thinking_policy(
    messages: Any, *, thinking_enabled: bool
) -> Any:
    """Filter assistant message thinking blocks for upstream native Anthropic JSON.

    When ``thinking_enabled`` is false, remove ``thinking`` and ``redacted_thinking``
    history so disabled policy is not undermined by prior turns.

    When true, keep ``redacted_thinking`` and signed ``thinking``; remove only
    unsigned plain ``thinking`` blocks (not replayable).
    """
    if not isinstance(messages, list):
        return messages

    sanitized_messages: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            sanitized_messages.append(message)
            continue

        if message.get("role") != "assistant":
            sanitized_messages.append(message)
            continue

        content = message.get("content")
        if not isinstance(content, list):
            sanitized_messages.append(message)
            continue

        if not thinking_enabled:
            sanitized_content = [
                block
                for block in content
                if not (
                    isinstance(block, dict)
                    and block.get("type") in ("thinking", "redacted_thinking")
                )
            ]
        else:
            sanitized_content = [
                block
                for block in content
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "thinking"
                    and not isinstance(block.get("signature"), str)
                )
            ]

        sanitized_message = dict(message)
        sanitized_message["content"] = sanitized_content or ""
        sanitized_messages.append(sanitized_message)

    return sanitized_messages


def _normalize_system_prompt_for_openrouter(system: Any) -> Any:
    """Flatten Claude SDK system blocks for OpenRouter's native endpoint."""
    if not isinstance(system, list):
        return system

    text_parts: list[str] = []
    for block in system:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
    return "\n\n".join(text_parts).strip() if text_parts else system


def _apply_openrouter_reasoning_policy(body: dict[str, Any], thinking_cfg: Any) -> None:
    """Map Anthropic thinking controls onto OpenRouter reasoning controls."""
    reasoning = body.setdefault("reasoning", {"enabled": True})
    if not isinstance(reasoning, dict):
        return
    reasoning.setdefault("enabled", True)
    if not isinstance(thinking_cfg, dict):
        return
    budget_tokens = thinking_cfg.get("budget_tokens")
    if isinstance(budget_tokens, int):
        reasoning.setdefault("max_tokens", budget_tokens)


def build_base_native_anthropic_request_body(
    request: Any,
    *,
    default_max_tokens: int,
    thinking_enabled: bool,
) -> dict[str, Any]:
    """Serialize a Pydantic messages request to a generic native Anthropic body."""
    body = dump_raw_messages_request(request)

    body.pop("extra_body", None)

    if "thinking" in body:
        thinking_cfg = body.pop("thinking")
        if thinking_enabled and isinstance(thinking_cfg, dict):
            thinking_payload: dict[str, Any] = {"type": "enabled"}
            budget_tokens = thinking_cfg.get("budget_tokens")
            if isinstance(budget_tokens, int):
                thinking_payload["budget_tokens"] = budget_tokens
            body["thinking"] = thinking_payload

    if "max_tokens" not in body:
        body["max_tokens"] = default_max_tokens

    if "messages" in body:
        body["messages"] = sanitize_native_messages_thinking_policy(
            body["messages"],
            thinking_enabled=thinking_enabled,
        )

    return body


def build_openrouter_native_request_body(
    request_data: Any,
    *,
    thinking_enabled: bool,
    default_max_tokens: int,
    openrouter_provider_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an Anthropic-format request body for OpenRouter (policy hooks built-in).

    ``openrouter_provider_options`` is the result of
    :func:`resolve_openrouter_provider_options` and is deep-merged into the
    ``provider`` field of the body *after* the caller's ``extra_body`` is
    applied, so the gateway's ``data_collection`` always wins over a client
    override while other client-supplied ``provider.*`` keys (e.g. ``order``)
    are preserved.
    """
    dumped_request = _dump_request_fields(request_data)
    request_extra = dumped_request.pop("extra_body", None)
    thinking_cfg = dumped_request.get("thinking")
    body: dict[str, Any] = {
        key: value
        for key, value in dumped_request.items()
        if key not in _INTERNAL_FIELDS
    }

    if isinstance(request_extra, dict):
        validate_openrouter_extra_body(request_extra)
        body.update(request_extra)

    body["messages"] = sanitize_native_messages_thinking_policy(
        body.get("messages"),
        thinking_enabled=thinking_enabled,
    )
    if "system" in body:
        body["system"] = _normalize_system_prompt_for_openrouter(body["system"])
    body["stream"] = True
    if body.get("max_tokens") is None:
        body["max_tokens"] = default_max_tokens

    if thinking_enabled:
        _apply_openrouter_reasoning_policy(body, thinking_cfg)

    # Server-wins: deep-merge the provider policy options into body["provider"]
    # so a client cannot override the gateway's data_collection via extra_body,
    # while other provider.* keys supplied by the client survive intact.
    if openrouter_provider_options:
        new_provider = openrouter_provider_options.get("provider")
        if isinstance(new_provider, dict):
            existing = body.get("provider")
            if isinstance(existing, dict):
                merged_provider = dict(existing)
                merged_provider.update(new_provider)
                body["provider"] = merged_provider
            else:
                body["provider"] = dict(new_provider)

    return body


def resolve_openrouter_provider_options(
    provider_model: str,
    settings: OpenRouterPolicySettings,
) -> dict[str, Any]:
    """Return the ``provider`` object to inject on an OpenRouter request body.

    The result is always a dict with a single ``provider`` key whose value is a
    ``{"data_collection": "deny"|"allow"}`` dict. The chosen value comes from
    :attr:`OpenRouterPolicySettings.open_router_free_data_collection` when
    ``provider_model`` is in
    :attr:`OpenRouterPolicySettings.open_router_free_model_ids`; otherwise it
    comes from :attr:`OpenRouterPolicySettings.open_router_data_collection`.

    Server-wins precedence: callers MUST merge the returned dict into the
    request body *after* any client-supplied ``extra_body`` so a malicious or
    buggy client cannot override the operator's policy.
    """
    free_ids = settings.open_router_free_model_ids
    policy = (
        settings.open_router_free_data_collection
        if provider_model in free_ids
        else settings.open_router_data_collection
    )
    return {"provider": {"data_collection": policy}}
