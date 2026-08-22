# tests/gates/test_fetcher_frozen_settings.py
"""A config the fetcher can no longer read is reported once, not per request.

A confined helper's Landlock rule binds to the config file's inode. `save_config` replaces the
file atomically, so the moment an operator saves a setting the rule stops covering it and every
later reload fails with EACCES -- while the file is perfectly readable to the account. Retrying
wrote a traceback per request, and three subagents searching in parallel produced three per turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.config.errors import ConfigLoadError
from nanoinfra.gates.fetcher.server import Fetcher, WebSettings, _is_unreadable_config


def _unreadable_config_error() -> ConfigLoadError:
    """The exact chain the loader raises: its own error, caused by a PermissionError."""
    cause = PermissionError(13, "Permission denied")
    error = ConfigLoadError(
        Path("/home/nanoinfra/.nanoinfra/config.json"),
        kind="unreadable",
        summary="Unable to read the file: Permission denied.",
    )
    error.__cause__ = cause
    return error


def test_the_cause_chain_is_walked_rather_than_the_top_exception() -> None:
    assert _is_unreadable_config(_unreadable_config_error())
    # A malformed config is fixable while the process runs, so it must not freeze anything.
    assert not _is_unreadable_config(ValueError("bad json"))


def test_an_unreadable_config_stops_the_reload_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []

    def loader() -> WebSettings:
        calls.append(1)
        raise _unreadable_config_error()

    fetcher = Fetcher(settings_loader=loader)
    for _ in range(4):
        fetcher._settings()  # pyright: ignore[reportPrivateUsage]

    # One attempt, not four: none of the retries could have succeeded.
    assert len(calls) == 1


def test_the_settings_that_loaded_before_the_replace_stay_in_force() -> None:
    loaded = WebSettings(provider="searxng", base_url="http://searxng:8080/")
    state: dict[str, Any] = {"fail": False}

    def loader() -> WebSettings:
        if state["fail"]:
            raise _unreadable_config_error()
        return loaded

    fetcher = Fetcher(settings_loader=loader)
    assert fetcher._settings().provider == "searxng"  # pyright: ignore[reportPrivateUsage]

    state["fail"] = True
    settings = fetcher._settings()  # pyright: ignore[reportPrivateUsage]

    # The operator's own last saved intent, rather than a default that drops their provider.
    assert settings.provider == "searxng"
    assert settings.base_url == "http://searxng:8080/"


def test_a_fixable_config_fault_keeps_retrying() -> None:
    """A broken file can be repaired while this process runs, so it must not latch."""
    calls: list[int] = []

    def loader() -> WebSettings:
        calls.append(1)
        raise ValueError("bad json")

    fetcher = Fetcher(settings_loader=loader)
    for _ in range(3):
        fetcher._settings()  # pyright: ignore[reportPrivateUsage]

    assert len(calls) == 3
