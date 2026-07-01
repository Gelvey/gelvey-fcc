"""Provider model-list response parsing helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from providers.exceptions import ModelListResponseError


@dataclass(frozen=True, slots=True)
class ProviderModelInfo:
    """Internal provider model metadata used for gateway model-list shaping."""

    model_id: str
    supports_thinking: bool | None = None


def model_infos_from_ids(
    model_ids: Iterable[str], *, supports_thinking: bool | None = None
) -> frozenset[ProviderModelInfo]:
    """Build unknown-capability model metadata from plain provider model ids."""
    return frozenset(
        ProviderModelInfo(model_id=model_id, supports_thinking=supports_thinking)
        for model_id in model_ids
        if model_id.strip()
    )


def extract_openai_model_ids(payload: Any, *, provider_name: str) -> frozenset[str]:
    """Extract model ids from an OpenAI-compatible ``/models`` response."""
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_ids: set[str] = set()
    for item in data:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(provider_name, "expected every data item to include id")
        model_ids.add(model_id)

    if not model_ids:
        raise _malformed(provider_name, "response did not include any model ids")
    return frozenset(model_ids)


def extract_openrouter_tool_model_ids(
    payload: Any, *, provider_name: str
) -> frozenset[str]:
    """Extract OpenRouter model ids that advertise tool-use support."""
    return frozenset(
        info.model_id
        for info in extract_openrouter_tool_model_infos(
            payload, provider_name=provider_name
        )
    )


def extract_openrouter_tool_model_infos(
    payload: Any, *, provider_name: str
) -> frozenset[ProviderModelInfo]:
    """Extract OpenRouter tool-capable model ids with thinking capability metadata."""
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_infos: set[ProviderModelInfo] = set()
    for item in data:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(provider_name, "expected every data item to include id")

        supported_parameters = _field(item, "supported_parameters")
        if not _is_sequence(supported_parameters):
            continue
        supported_parameter_names = {
            param for param in supported_parameters if isinstance(param, str)
        }
        if supported_parameter_names.isdisjoint({"tools", "tool_choice"}):
            continue
        model_infos.add(
            ProviderModelInfo(
                model_id=model_id,
                supports_thinking="reasoning" in supported_parameter_names,
            )
        )

    return frozenset(model_infos)


def extract_cloudflare_ai_model_ids(
    payload: Any, *, provider_name: str
) -> frozenset[str]:
    """Extract text-generation model ids from a Cloudflare Workers AI ``/ai/models`` response.

    Only text-generation models are included; embeddings, image-generation, audio,
    and other task types are filtered out because they cannot be used for chat
    completions.

    Handles multiple response formats:
    - ``/ai/models``: ``{"id": "@cf/...", "task": {"id": "text-generation"}}``
    - ``/ai/models/search``: ``{"id": "<uuid>", "name": "@cf/...", "task": ...}``
      or other variants where the model name is in ``name``, ``model``,
      or ``model_id`` fields.
    """
    result = _field(payload, "result")
    if not _is_sequence(result):
        raise _malformed(provider_name, "expected top-level result array")

    model_ids: set[str] = set()
    for item in result:
        model_id = _extract_model_id(item)
        if not model_id:
            continue
        if _is_text_generation_model(item):
            model_ids.add(model_id)

    if not model_ids:
        raise _malformed(
            provider_name, "response did not include any text-generation model ids"
        )
    return frozenset(model_ids)


def _extract_model_id(item: Any) -> str | None:
    """Extract the usable model id from a Cloudflare AI model item.

    The ``/ai/models/search`` endpoint returns UUIDs in ``id`` and the actual
    model name (e.g. ``@cf/meta/llama-3.3-70b-instruct-fp8-fast``) in ``name``,
    ``model``, or ``model_id``.  Prefer the human-readable name when available.
    """
    # Check alternative name fields first (preferred for /ai/models/search)
    for field_name in ("name", "model", "model_id"):
        value = _field(item, field_name)
        if isinstance(value, str) and value.strip() and value.startswith("@"):
            return value
    # Fall back to id field
    raw_id = _field(item, "id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id
    return None


def _is_text_generation_model(item: Any) -> bool:
    """Check if a model item represents a text-generation model.

    Supports multiple response formats from different Cloudflare AI endpoints:
    - Dict with ``task.id`` or ``task.name``
    - Object-style (``SimpleNamespace``) with ``task.id`` attribute
    - ``task`` as a plain string
    - ``type`` field instead of ``task``
    """
    task = _field(item, "task")
    # Check task as Mapping (dict) or object with attributes
    task_id = _field(task, "id") if task is not None else None
    if task_id == "text-generation":
        return True
    # Also check task.name for "Text Generation" (case-insensitive)
    task_name = _field(task, "name") if task is not None else None
    if isinstance(task_name, str) and task_name.lower() == "text generation":
        return True
    # Format: task as string (/ai/models/search variant)
    if isinstance(task, str) and task == "text-generation":
        return True
    # Format: type field instead of task
    item_type = _field(item, "type")
    return isinstance(item_type, str) and item_type == "text-generation"


def extract_ollama_model_ids(payload: Any, *, provider_name: str) -> frozenset[str]:
    """Extract model ids from Ollama's native ``/api/tags`` response."""
    models = _field(payload, "models")
    if not _is_sequence(models):
        raise _malformed(provider_name, "expected top-level models array")

    model_ids: set[str] = set()
    for item in models:
        item_ids: list[str] = []
        for key in ("model", "name"):
            value = _field(item, key)
            if isinstance(value, str) and value.strip():
                item_ids.append(value)
        if not item_ids:
            raise _malformed(
                provider_name,
                "expected every models item to include model or name",
            )
        model_ids.update(item_ids)

    if not model_ids:
        raise _malformed(provider_name, "response did not include any model ids")
    return frozenset(model_ids)


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def _malformed(provider_name: str, reason: str) -> ModelListResponseError:
    return ModelListResponseError(
        f"{provider_name} model-list response is malformed: {reason}"
    )
