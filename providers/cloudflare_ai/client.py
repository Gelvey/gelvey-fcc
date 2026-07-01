"""Cloudflare Workers AI provider (OpenAI-compatible chat completions).

The OpenAI-compat endpoint lives at
``https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1`` and exposes
``/chat/completions`` with the standard OpenAI request shape. There is no
corresponding OpenAI-compat ``/models`` endpoint, so we advertise a curated list
of the free coding/reasoning models that ship on the Workers AI free tier.
"""

from __future__ import annotations

from typing import Any

from providers.base import ProviderConfig
from providers.transports.openai_chat import OpenAIChatTransport

from .request import build_request_body

# Curated list of popular Cloudflare Workers AI free-tier chat models. Kept in
# one place so tests and discovery stay in lockstep. New entries should be added
# only after the model is generally available on the Workers AI free tier.
_DEFAULT_MODEL_IDS: frozenset[str] = frozenset(
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

    async def list_model_ids(self) -> frozenset[str]:
        """Return curated free-tier Cloudflare Workers AI model ids.

        Cloudflare's OpenAI-compat layer does not expose ``GET /v1/models``. The
        native model-list endpoint requires a separate authentication model,
        so we ship a curated set of the free models useful for coding/agent
        workloads instead.
        """
        return _DEFAULT_MODEL_IDS
