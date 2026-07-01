"""Cloudflare Workers AI provider (OpenAI-compatible chat completions).

The OpenAI-compat endpoint lives at
``https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1`` and exposes
``/chat/completions`` with the standard OpenAI request shape. Model discovery
uses the native Cloudflare REST API at ``/ai/models`` to fetch all available
text-generation models dynamically.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from providers.base import ProviderConfig
from providers.exceptions import ModelListResponseError
from providers.model_listing import extract_cloudflare_ai_model_ids
from providers.transports.openai_chat import OpenAIChatTransport

from .request import build_request_body

# Curated fallback set used when the live /ai/models endpoint is unreachable.
_FALLBACK_MODEL_IDS: frozenset[str] = frozenset(
    {
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "@cf/meta/llama-3.1-8b-instruct",
        "@cf/qwen/qwen2.5-coder-32b-instruct",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
    }
)


class CloudflareAiProvider(OpenAIChatTransport):
    """Cloudflare Workers AI (OpenAI-compatible chat completions)."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="CLOUDFLARE_AI",
            base_url=config.base_url or "",
            api_key=config.api_key,
        )

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict:
        return build_request_body(
            request,
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )

    def _models_endpoint(self) -> str:
        """Derive the native Cloudflare /ai/models URL from the OpenAI-compat base URL."""
        base = self._base_url
        if base.endswith("/ai/v1"):
            return base[: -len("/ai/v1")] + "/ai/models"
        # Custom base URL (proxy, self-hosted gateway): try appending /models
        # relative to the AI root.
        return base.rstrip("/") + "/models"

    async def list_model_ids(self) -> frozenset[str]:
        """Return text-generation model ids from the Cloudflare Workers AI API.

        Falls back to a curated set of free-tier models if the native API is
        unreachable (network error, auth change, rate limit).
        """
        url = self._models_endpoint()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=15.0,
                )
                response.raise_for_status()
            payload = response.json()
            return extract_cloudflare_ai_model_ids(
                payload, provider_name=self._provider_name
            )
        except (httpx.HTTPError, ModelListResponseError, ValueError) as exc:
            logger.warning(
                "CLOUDFLARE_AI_MODEL_LIST: live fetch failed, using fallback: "
                "url={} exc_type={} detail={}",
                url,
                type(exc).__name__,
                exc,
            )
            return _FALLBACK_MODEL_IDS
