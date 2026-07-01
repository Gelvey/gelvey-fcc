"""Cloudflare Workers AI (OpenAI-compat) adapter."""

from providers.defaults import CLOUDFLARE_AI_DEFAULT_BASE

from .client import CloudflareAiProvider

__all__ = ["CLOUDFLARE_AI_DEFAULT_BASE", "CloudflareAiProvider"]
