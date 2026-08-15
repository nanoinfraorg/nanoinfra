# tests/cli/test_no_helper_leak.py
"""The autouse guard in conftest must cover every helper start.

A CLI test that reaches a real start leaves a real child alive. The guard is easy to lose, because
nothing in a CLI test reads as "this starts a process". So one test asserts the guard is active and
names every helper it must cover.
"""

from __future__ import annotations

import subprocess

import pytest

from nanoinfra.cli import gateway_runtime


def test_every_helper_start_is_stubbed(helper_start_names: tuple[str, ...]) -> None:
    """Each start answers None here, which is what a failed start already answers."""
    for name in helper_start_names:
        start = getattr(gateway_runtime, name)
        assert start(object()) is None, name


def test_the_guard_names_every_helper_the_runtime_starts(
    helper_start_names: tuple[str, ...],
) -> None:
    """A fourth helper must join the guard on the day it lands, so the count is asserted."""
    starts = {
        name
        for name in dir(gateway_runtime)
        if name.startswith("_start_") and name.endswith("_for_gateway")
    }

    assert starts == set(helper_start_names)


def test_no_helper_child_survives_a_cli_test_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The property the guard exists for, measured against the process table.

    A leaked child names its module and its socket in its argv, so the check is cheap and
    specific. It counts this session's directory alone: a developer may run a real gateway beside
    the suite, and an older run may have left a child that this run did not create.
    """
    session_root = str(tmp_path_factory.getbasetemp().parent)
    listing = subprocess.run(
        ["pgrep", "-af", "python -m nanoinfra.gates"],
        capture_output=True,
        text=True,
        check=False,
    )
    leaked = [
        line
        for line in listing.stdout.splitlines()
        if session_root in line and "--socket" in line
    ]

    assert leaked == [], f"a test left a helper child alive: {leaked}"


def test_a_spawned_gateway_starts_no_helper(helper_external_env: tuple[str, ...]) -> None:
    """A CLI test may start the gateway as a subprocess, and a patch here cannot reach it.

    The child reads these variables, so the guard sets them for every CLI test. A subprocess that
    inherits them starts none of the three helpers.
    """
    import os

    for name in helper_external_env:
        assert os.environ.get(name) == "1", name
