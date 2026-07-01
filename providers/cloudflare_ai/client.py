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
from providers.model_listing import extract_cloudflare_ai_model_ids
from providers.transports.openai_chat import OpenAIChatTransport

from .request import build_request_body


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
        """Derive the native Cloudflare /ai/models/search URL from the OpenAI-compat base URL."""
        base = self._base_url
        if base.endswith("/ai/v1"):
            return base[: -len("/ai/v1")] + "/ai/models/search"
        # Custom base URL (proxy, self-hosted gateway): try appending /models/search
        # relative to the AI root.
        return base.rstrip("/") + "/models/search"

    async def list_model_ids(self) -> frozenset[str]:
        """Return text-generation model ids from the Cloudflare Workers AI API.

        Raises on failure so the caller surfaces the real error to the user.
        """
        url = self._models_endpoint()
        logger.debug(
            "CLOUDFLARE_AI_MODEL_LIST: fetching models from url={}",
            url,
        )
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=15.0,
            )
            response.raise_for_status()
        payload = response.json()
        # Log first result item structure for debugging format changes
        result = payload.get("result") if isinstance(payload, dict) else None
        first_item = result[0] if isinstance(result, list) and result else None
        logger.debug(
            "CLOUDFLARE_AI_MODEL_LIST: response keys={} success={} result_count={} first_item_keys={}",
            list(payload.keys())
            if isinstance(payload, dict)
            else type(payload).__name__,
            payload.get("success") if isinstance(payload, dict) else None,
            len(result) if isinstance(result, list) else None,
            list(first_item.keys())
            if isinstance(first_item, dict)
            else type(first_item).__name__
            if first_item is not None
            else None,
        )
        if isinstance(first_item, dict):
            logger.debug(
                "CLOUDFLARE_AI_MODEL_LIST: first_item={}",
                first_item,
            )
        return extract_cloudflare_ai_model_ids(
            payload, provider_name=self._provider_name
        )
