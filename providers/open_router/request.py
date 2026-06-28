"""Native Anthropic Messages request builder for OpenRouter."""

from __future__ import annotations

from typing import Any

from loguru import logger

from config.constants import (
    ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS as OPENROUTER_DEFAULT_MAX_TOKENS,
)
from core.anthropic.native_messages_request import (
    OpenRouterExtraBodyError,
    OpenRouterPolicySettings,
    build_openrouter_native_request_body,
    resolve_openrouter_provider_options,
)
from providers.exceptions import InvalidRequestError


def build_request_body(
    request_data: Any,
    *,
    thinking_enabled: bool,
    settings: OpenRouterPolicySettings | None = None,
) -> dict:
    """Build an Anthropic-format request body for OpenRouter's messages API.

    When ``settings`` is provided, the gateway's OpenRouter data-collection
    policy is resolved and merged into the body so a client cannot bypass it
    via ``extra_body``. The model id used for the allowlist lookup is
    ``request_data.model`` (already the resolved provider model after
    :class:`api.model_router.ModelRouter`).
    """
    provider_model = getattr(request_data, "model", None) or ""
    logger.debug(
        "OPENROUTER_REQUEST: conversion start model={} msgs={}",
        provider_model or "?",
        len(getattr(request_data, "messages", [])),
    )

    provider_options: dict[str, Any] | None = None
    if settings is not None and provider_model:
        provider_options = resolve_openrouter_provider_options(provider_model, settings)

    try:
        body = build_openrouter_native_request_body(
            request_data,
            thinking_enabled=thinking_enabled,
            default_max_tokens=OPENROUTER_DEFAULT_MAX_TOKENS,
            openrouter_provider_options=provider_options,
        )
    except OpenRouterExtraBodyError as exc:
        raise InvalidRequestError(str(exc)) from exc

    logger.debug(
        "OPENROUTER_REQUEST: conversion done model={} msgs={} tools={} data_collection={}",
        body.get("model"),
        len(body.get("messages", [])),
        len(body.get("tools", [])),
        body.get("provider", {}).get("data_collection"),
    )
    return body
