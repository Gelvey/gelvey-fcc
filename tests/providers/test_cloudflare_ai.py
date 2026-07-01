"""Tests for Cloudflare Workers AI (OpenAI-compatible) provider."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from providers.base import ProviderConfig
from providers.cloudflare_ai import CLOUDFLARE_AI_DEFAULT_BASE, CloudflareAiProvider
from providers.exceptions import AuthenticationError


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


@pytest.mark.asyncio
async def test_list_model_ids_returns_curated_set(cloudflare_provider):
    model_ids = await cloudflare_provider.list_model_ids()
    assert "@cf/meta/llama-3.3-70b-instruct-fp8-fast" in model_ids
    assert "@cf/qwen/qwen2.5-coder-32b-instruct" in model_ids
    assert all(model.startswith("@cf/") for model in model_ids)


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
