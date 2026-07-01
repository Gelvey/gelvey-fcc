"""Tests for Cloudflare Workers AI (OpenAI-compatible) provider."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config.settings import Settings
from providers.base import ProviderConfig
from providers.cloudflare_ai import CLOUDFLARE_AI_DEFAULT_BASE, CloudflareAiProvider
from providers.exceptions import AuthenticationError, ModelListResponseError


class MockMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class MockRequest:
    def __init__(self, **kwargs):
        self.model = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        self.messages = [MockMessage("user", "Hello")]
        self.max_tokens = 100
        self.temperature = 0.5
        self.top_p = 0.9
        self.system = "System prompt"
        self.stop_sequences = None
        self.tools = []
        self.thinking = MagicMock()
        self.thinking.enabled = True
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture
def cloudflare_config():
    return ProviderConfig(
        api_key="test_cloudflare_token",
        base_url=("https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"),
        rate_limit=10,
        rate_window=60,
        enable_thinking=True,
    )


@pytest.fixture(autouse=True)
def mock_rate_limiter():
    """Mock the global rate limiter to prevent waiting."""

    @asynccontextmanager
    async def _slot():
        yield

    with patch("providers.transports.openai_chat.transport.GlobalRateLimiter") as mock:
        instance = mock.get_scoped_instance.return_value

        async def _passthrough(fn, *args, **kwargs):
            return await fn(*args, **kwargs)

        instance.execute_with_retry = AsyncMock(side_effect=_passthrough)
        instance.concurrency_slot.side_effect = _slot
        yield instance


@pytest.fixture
def cloudflare_provider(cloudflare_config):
    return CloudflareAiProvider(cloudflare_config)


def _cf_models_payload(*model_ids: str) -> dict:
    """Build a minimal Cloudflare /ai/models response payload."""
    return {
        "success": True,
        "result": [
            {
                "id": mid,
                "task": {"id": "text-generation", "name": "Text Generation"},
                "name": mid.split("/")[-1],
            }
            for mid in model_ids
        ],
    }


def _cf_models_response(*model_ids: str, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response wrapping a Cloudflare models payload."""
    return httpx.Response(
        status_code=status_code,
        json=_cf_models_payload(*model_ids),
        request=httpx.Request(
            "GET",
            "https://api.cloudflare.com/client/v4/accounts/abc123/ai/models/search",
        ),
    )


def test_default_base_url_constant_contains_placeholder():
    assert (
        CLOUDFLARE_AI_DEFAULT_BASE
        == "https://api.cloudflare.com/client/v4/accounts/CLOUDFLARE_AI_ACCOUNT_ID/ai/v1"
    )


def test_init(cloudflare_config):
    with patch("providers.transports.openai_chat.transport.AsyncOpenAI") as mock_openai:
        provider = CloudflareAiProvider(cloudflare_config)
        assert provider._api_key == "test_cloudflare_token"
        assert provider._base_url == (
            "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
        )
        mock_openai.assert_called_once()


def test_models_endpoint_standard_url(cloudflare_provider):
    """Standard /ai/v1 base URL is rewritten to /ai/models/search."""
    assert cloudflare_provider._models_endpoint() == (
        "https://api.cloudflare.com/client/v4/accounts/abc123/ai/models/search"
    )


def test_models_endpoint_custom_url():
    """Custom base URLs get /models/search appended."""
    provider = CloudflareAiProvider(
        ProviderConfig(
            api_key="tok",
            base_url="https://my-proxy.example.com/cf-ai",
            rate_limit=1,
            rate_window=60,
        )
    )
    assert (
        provider._models_endpoint()
        == "https://my-proxy.example.com/cf-ai/models/search"
    )


@pytest.mark.asyncio
async def test_list_model_ids_fetches_from_api(cloudflare_provider):
    """Live /ai/models response is parsed and text-generation models returned."""
    payload = {
        "success": True,
        "result": [
            {
                "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "task": {"id": "text-generation", "name": "Text Generation"},
            },
            {
                "id": "@cf/qwen/qwen2.5-coder-32b-instruct",
                "task": {"id": "text-generation", "name": "Text Generation"},
            },
            {
                "id": "@cf/meta/llama-3.1-8b-instruct",
                "task": {"id": "text-generation", "name": "Text Generation"},
            },
            {
                "id": "@cf/mistralai/mistral-small-3.1-24b-instruct",
                "task": {"id": "text-generation", "name": "Text Generation"},
            },
            {
                "id": "@cf/stabilityai/stable-diffusion-xl-base-1.0",
                "task": {"id": "image-generation", "name": "Image Generation"},
            },
            {
                "id": "@cf/baai/bge-base-en-v1.5",
                "task": {"id": "text-embeddings", "name": "Text Embeddings"},
            },
        ],
    }
    mock_response = httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://api.cloudflare.com/"),
    )

    with patch("providers.cloudflare_ai.client.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.get.return_value = mock_response

        model_ids = await cloudflare_provider.list_model_ids()

    assert "@cf/meta/llama-3.3-70b-instruct-fp8-fast" in model_ids
    assert "@cf/qwen/qwen2.5-coder-32b-instruct" in model_ids
    assert all(mid.startswith("@cf/") for mid in model_ids)
    # Non-text-generation models (image-generation, embeddings) are excluded.
    assert "@cf/stabilityai/stable-diffusion-xl-base-1.0" not in model_ids
    assert "@cf/baai/bge-base-en-v1.5" not in model_ids
    assert len(model_ids) == 4


@pytest.mark.asyncio
async def test_list_model_ids_raises_on_http_error(cloudflare_provider):
    """Network errors propagate to the caller instead of silently falling back."""
    with patch("providers.cloudflare_ai.client.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        with pytest.raises(httpx.ConnectError, match="connection refused"):
            await cloudflare_provider.list_model_ids()


@pytest.mark.asyncio
async def test_list_model_ids_raises_on_malformed_response(cloudflare_provider):
    """Malformed JSON (no text-generation models) raises instead of silently falling back."""
    with patch("providers.cloudflare_ai.client.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        bad_response = httpx.Response(
            status_code=200,
            json={"success": True, "result": []},
            request=httpx.Request("GET", "https://api.cloudflare.com/"),
        )
        mock_client.get.return_value = bad_response

        with pytest.raises(ModelListResponseError, match="text-generation"):
            await cloudflare_provider.list_model_ids()


@pytest.mark.asyncio
async def test_list_model_ids_raises_on_http_status_error(cloudflare_provider):
    """HTTP status errors (401, 403, 429, etc.) propagate to the caller."""
    with patch("providers.cloudflare_ai.client.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        error_response = httpx.Response(
            status_code=401,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
            },
            request=httpx.Request("GET", "https://api.cloudflare.com/"),
        )
        mock_client.get.return_value = error_response

        with pytest.raises(httpx.HTTPStatusError):
            await cloudflare_provider.list_model_ids()


def test_build_request_body_basic(cloudflare_provider):
    """Basic request body conversion attaches system message from Anthropic request."""
    req = MockRequest()
    body = cloudflare_provider._build_request_body(req)

    assert body["model"] == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert body["messages"][0]["role"] == "system"


def test_build_request_body_global_disable_blocks_reasoning_mapping():
    provider = CloudflareAiProvider(
        ProviderConfig(
            api_key="test_cloudflare_token",
            base_url=("https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"),
            rate_limit=10,
            rate_window=60,
            enable_thinking=False,
        )
    )
    req = MockRequest()
    body = provider._build_request_body(req)

    roles = [m.get("role") for m in body.get("messages", [])]
    assert "assistant_reasoning_content" not in roles


def test_build_request_body_preserves_caller_extra_body(cloudflare_provider):
    req = MockRequest(extra_body={"guidance": {"length": 4096}})

    body = cloudflare_provider._build_request_body(req)

    assert body.get("extra_body") == {"guidance": {"length": 4096}}


def test_build_request_body_handles_missing_extra_body(cloudflare_provider):
    body = cloudflare_provider._build_request_body(MockRequest())
    assert "extra_body" not in body


@pytest.mark.asyncio
async def test_stream_response_text(cloudflare_provider):
    """Text content deltas are emitted as text blocks."""
    mock_chunk = MagicMock()
    mock_chunk.choices = [
        MagicMock(
            delta=MagicMock(
                content="Cloudflare reply",
                reasoning_content=None,
                tool_calls=None,
            ),
            finish_reason="stop",
        )
    ]
    mock_chunk.usage = MagicMock(completion_tokens=5, prompt_tokens=10)

    async def mock_stream():
        yield mock_chunk

    with patch.object(
        cloudflare_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_stream()

        events = [
            event async for event in cloudflare_provider.stream_response(MockRequest())
        ]

    assert any(
        '"text_delta"' in event and "Cloudflare reply" in event for event in events
    )


@pytest.mark.asyncio
async def test_stream_response_reasoning_content(cloudflare_provider):
    """reasoning_content deltas are emitted as thinking blocks."""
    mock_chunk = MagicMock()
    mock_chunk.choices = [
        MagicMock(
            delta=MagicMock(
                content=None,
                reasoning_content="Thinking...",
                tool_calls=None,
            ),
            finish_reason="stop",
        )
    ]
    mock_chunk.usage = MagicMock(completion_tokens=2, prompt_tokens=10)

    async def mock_stream():
        yield mock_chunk

    with patch.object(
        cloudflare_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_stream()

        events = [
            event async for event in cloudflare_provider.stream_response(MockRequest())
        ]

    assert any(
        '"thinking_delta"' in event and "Thinking..." in "".join(events)
        for event in events
    )


@pytest.mark.asyncio
async def test_cleanup(cloudflare_provider):
    cloudflare_provider._client = AsyncMock()

    await cloudflare_provider.cleanup()

    cloudflare_provider._client.close.assert_called_once()


def test_factory_raises_when_account_id_missing():
    """Setting only the token without the account id must raise AuthenticationError."""
    from providers.registry import _create_cloudflare_ai

    settings = Settings.model_construct(
        cloudflare_ai_api_key="tok",
        cloudflare_ai_account_id="",
        cloudflare_ai_base_url="",
    )

    config = ProviderConfig(
        api_key="tok",
        base_url=CLOUDFLARE_AI_DEFAULT_BASE,
        rate_limit=1,
        rate_window=60,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        _create_cloudflare_ai(config, settings)

    assert "CLOUDFLARE_AI_ACCOUNT_ID" in str(exc_info.value)


# ---------------------------------------------------------------------------
# extract_cloudflare_ai_model_ids unit tests
# ---------------------------------------------------------------------------


class TestExtractCloudflareAiModelIds:
    """Unit tests for the Cloudflare /ai/models response parser."""

    def test_extracts_text_generation_models(self):
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = _cf_models_payload(
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "@cf/qwen/qwen2.5-coder-32b-instruct",
        )
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset(
            {
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "@cf/qwen/qwen2.5-coder-32b-instruct",
            }
        )

    def test_filters_out_non_text_generation_models(self):
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {
                    "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "task": {"id": "text-generation", "name": "Text Generation"},
                },
                {
                    "id": "@cf/baai/bge-base-en-v1.5",
                    "task": {"id": "text-embeddings", "name": "Text Embeddings"},
                },
                {
                    "id": "@cf/stabilityai/stable-diffusion-xl-base-1.0",
                    "task": {"id": "image-generation", "name": "Image Generation"},
                },
            ],
        }
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})

    def test_raises_on_missing_result_field(self):
        from providers.exceptions import ModelListResponseError
        from providers.model_listing import extract_cloudflare_ai_model_ids

        with pytest.raises(ModelListResponseError, match="result array"):
            extract_cloudflare_ai_model_ids(
                {"success": True}, provider_name="CLOUDFLARE_AI"
            )

    def test_raises_on_empty_result(self):
        from providers.exceptions import ModelListResponseError
        from providers.model_listing import extract_cloudflare_ai_model_ids

        with pytest.raises(ModelListResponseError, match="text-generation"):
            extract_cloudflare_ai_model_ids(
                {"success": True, "result": []}, provider_name="CLOUDFLARE_AI"
            )

    def test_raises_when_no_text_generation_models(self):
        from providers.exceptions import ModelListResponseError
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {
                    "id": "@cf/baai/bge-base-en-v1.5",
                    "task": {"id": "text-embeddings", "name": "Text Embeddings"},
                },
            ],
        }
        with pytest.raises(ModelListResponseError, match="text-generation"):
            extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")

    def test_skips_items_without_id(self):
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {"task": {"id": "text-generation"}},
                {
                    "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "task": {"id": "text-generation", "name": "Text Generation"},
                },
            ],
        }
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})

    def test_handles_result_as_object_with_result_attr(self):
        """Supports both dict and object-style payloads (like OpenAI SDK objects)."""
        from types import SimpleNamespace

        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = SimpleNamespace(
            result=[
                SimpleNamespace(
                    id="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    task=SimpleNamespace(id="text-generation"),
                ),
            ]
        )
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})

    def test_handles_task_as_string(self):
        """Supports /ai/models/search format where task is a plain string."""
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {
                    "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "task": "text-generation",
                },
                {
                    "id": "@cf/baai/bge-base-en-v1.5",
                    "task": "text-embeddings",
                },
            ],
        }
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})

    def test_handles_type_field_instead_of_task(self):
        """Supports response format where type field is used instead of task."""
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {
                    "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "type": "text-generation",
                },
                {
                    "id": "@cf/stabilityai/stable-diffusion-xl-base-1.0",
                    "type": "image-generation",
                },
            ],
        }
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})

    def test_handles_task_name_text_generation(self):
        """Supports response format where task.name is 'Text Generation'."""
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {
                    "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "task": {"name": "Text Generation"},
                },
                {
                    "id": "@cf/baai/bge-base-en-v1.5",
                    "task": {"name": "Text Embeddings"},
                },
            ],
        }
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})

    def test_handles_uuid_id_with_name_field(self):
        """Supports /ai/models/search format where id is UUID and name is the model name."""
        from providers.model_listing import extract_cloudflare_ai_model_ids

        payload = {
            "success": True,
            "result": [
                {
                    "id": "02c16efa-29f5-4304-8e6c-3d188889f875",
                    "name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "task": {"id": "text-generation", "name": "Text Generation"},
                },
                {
                    "id": "06455e78-19f7-487b-93cd-c05a3dd07813",
                    "name": "@cf/baai/bge-base-en-v1.5",
                    "task": {"id": "text-embeddings", "name": "Text Embeddings"},
                },
            ],
        }
        result = extract_cloudflare_ai_model_ids(payload, provider_name="CLOUDFLARE_AI")
        assert result == frozenset({"@cf/meta/llama-3.3-70b-instruct-fp8-fast"})
