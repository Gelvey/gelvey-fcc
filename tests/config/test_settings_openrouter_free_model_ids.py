"""Regression tests for ``Settings()`` recovering from an empty ``OPENROUTER_FREE_MODEL_IDS``.

Bug history: pydantic-settings detected ``frozenset[str]`` as a "complex"
type and JSON-decoded the dotenv/process-env value BEFORE the
``mode="before"`` field validator ran. Empty strings crashed on
``json.loads("")`` and surfaced as
``pydantic_settings.exceptions.SettingsError: error parsing value for field
"open_router_free_model_ids"`` for every Admin UI request.

The field is now marked ``Annotated[frozenset[str], NoDecode]`` so the raw
env string flows straight to the existing
``parse_openrouter_free_model_ids(mode="before")`` validator, which
already handles ``None`` / empty / frozenset pass-through / comma-list /
list / tuple / set inputs correctly.

Hermeticity: each parametrized case shifts ``cwd`` into an empty
``tmp_path`` so any host ``.env`` cannot leak, then calls
``monkeypatch.delenv(..., raising=False)`` plus ``monkeypatch.setenv(...)``
on the key under test so parent process env is also cleared. The test is
therefore independent of the user's live ``.env``, ``~/.fcc/.env``, or
process env state. ``Settings()`` is called without ``_env_file=`` because
:mod:`ty` does not currently expose underscore-prefixed kwargs in its
pydantic-settings stub (see ``tests/api/test_admin.py`` and
``tests/api/test_optimization_handlers.py`` for the established project
patterns).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "meta-llama/foo:free,deepseek/bar:free",
        "meta-llama/foo:free , deepseek/bar:free ,",
        "single-model:free",
    ],
)
def test_openrouter_free_model_ids_handles_common_shapes(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty / whitespace / comma-list shapes round-trip without raising."""

    monkeypatch.chdir(tmp_path)  # cwd has no .env that could leak
    monkeypatch.delenv("OPENROUTER_FREE_MODEL_IDS", raising=False)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL_IDS", raw)
    settings = Settings()
    parsed = settings.open_router_free_model_ids
    assert isinstance(parsed, frozenset)
    assert all(isinstance(item, str) and item.strip() for item in parsed)
    if raw.strip():
        expected = frozenset(part.strip() for part in raw.split(",") if part.strip())
        assert parsed == expected
    else:
        assert parsed == frozenset()
