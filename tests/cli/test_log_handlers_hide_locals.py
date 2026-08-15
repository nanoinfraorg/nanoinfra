# tests/cli/test_log_handlers_hide_locals.py
"""A diagnosed traceback prints every frame local, and a local can hold a credential.

loguru's `diagnose` option is on by default, and `logger.exception` then writes the value of every
local in every frame. The executor resolves a plaintext credential into a local, and it builds a
resolved command that routinely embeds one, so a diagnosed traceback writes both into a log file
that lives for the life of the deployment.

#41 fixed the executor child. Its parent handlers kept the default, so a gateway-side traceback
could still print the same values. `backtrace` stays on, because a traceback that names the file and
the line of each frame costs nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_FILES = (
    Path("nanoinfra/cli/commands.py"),
    Path("nanoinfra/cli/gateway.py"),
    Path("nanoinfra/gates/executor/__main__.py"),
)


def _logger_add_calls(path: Path) -> list[ast.Call]:
    """Every `logger.add(...)` call in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add":
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "logger":
            calls.append(node)
    return calls


@pytest.mark.parametrize("path", _FILES, ids=lambda p: str(p))
def test_every_handler_turns_diagnose_off(path: Path) -> None:
    calls = _logger_add_calls(path)

    assert calls, f"{path} adds no handler, so this test guards nothing"
    for call in calls:
        names = {kw.arg for kw in call.keywords}
        assert "diagnose" in names, f"{path}:{call.lineno} adds a handler with diagnose left on"
        for kw in call.keywords:
            if kw.arg == "diagnose":
                assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (
                    f"{path}:{call.lineno} must pass diagnose=False"
                )
