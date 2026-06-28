"""Regression for ``api.app.general_error_handler`` settings-reload guard.

Bug history: ``general_error_handler`` calls ``get_settings()`` to read
``log_api_error_tracebacks``. When ``Settings()`` itself raised mid-handler
(e.g.:class:`pydantic_settings.exceptions.SettingsError` from a broken
dotenv value), uvicorn produced a recursive 500 in the access log because
every 500 attempt re-triggered the same exception.

The 0004 patch wraps the ``get_settings()`` call in a narrow
``(... SettingsError, ValidationError, OSError, UnicodeDecodeError)``
handler. This test forces ``get_settings()`` to raise during a request
whose route handler has already triggered the FastAPI generic Exception
handler, and asserts the response is still the project-standard Anthropic
500 envelope.

The success-path 500 envelope is already covered by
``tests/api/test_app_lifespan_and_errors.py:test_create_app_general_exception_handler_returns_500``
and the no-leak guarantee by
``...::test_create_app_general_exception_default_logs_exclude_exception_message``,
so this file intentionally contains only the failure-path test.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic_settings.exceptions import SettingsError

import api.app as api_app_mod
from providers.registry import ProviderRegistry


def test_general_error_handler_emits_clean_500_when_settings_reload_raises(
    tmp_path: Path,
) -> None:
    """``SettingsError`` raised by ``get_settings()`` does NOT re-trigger the handler."""

    # Phase 1 stub: ``create_app`` reads only ``settings.log_raw_api_payloads``
    # during its log-config call. ``lifespan_enabled=False`` keeps the app
    # from registering startup callbacks, so ``TestClient.__enter__`` cannot
    # trigger any post-create ``get_settings()`` call (which would clobber
    # the phase-2 raise-patch).
    create_app_settings = SimpleNamespace(log_raw_api_payloads=False)

    with patch.object(api_app_mod, "get_settings", return_value=create_app_settings):
        from api.app import create_app

        app = create_app(lifespan_enabled=False)

    @app.get("/raise_runtime")
    async def _boom() -> None:
        raise RuntimeError("original handler exception")

    # Phase 2: during the request, ``get_settings`` raises SettingsError so the
    # hardened try/except inside ``general_error_handler`` fires.
    # ``raise_server_exceptions=False`` ensures TestClient surfaces the
    # handler-emitted JSON envelope rather than re-raising.
    with (
        patch.object(
            api_app_mod,
            "get_settings",
            side_effect=SettingsError("forced for test"),
        ),
        patch.object(ProviderRegistry, "cleanup", new=AsyncMock()),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/raise_runtime")

    assert response.status_code == 500, response.text
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": "An unexpected error occurred.",
        },
    }
