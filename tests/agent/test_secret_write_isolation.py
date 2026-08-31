"""No module the agent can load reaches the secret-write client (#192).

The WebUI and the agent share one process, so "the WebUI may write credentials and the agent may
not" cannot be a uid boundary here. It is a boundary in the import graph, which is the same
mechanism `test_redaction_isolation.py` uses for reading a plaintext — and the same reason: a
method one import away from a chat turn is reachable from a chat turn.

Writing matters separately from reading. A compromised agent that could write the store could
**replace** a credential — swap a refresh token, repoint a `secretRef` at a value it chose — and
every later action that resolves it would use the new one. That is why the fix for #192 was not
to make `secrets/` group-writable.
"""

from __future__ import annotations

import ast
from pathlib import Path

_AGENT_TREE = Path("nanoinfra/agent")

# The client, and the module that reaches it. Neither belongs in the agent's import graph.
_FORBIDDEN_MODULES = (
    "nanoinfra.webui.secret_write_client",
    "nanoinfra.webui.secrets_api",
)

# The names those modules export, in case a future refactor moves them without moving the rule.
_FORBIDDEN_NAMES = ("SecretWriteClient", "SecretWriteRequest", "write_record", "write_update")


def _agent_modules() -> list[Path]:
    found = sorted(_AGENT_TREE.rglob("*.py"))
    assert found, f"no modules found under {_AGENT_TREE}"
    return found


def _imports(path: Path) -> set[str]:
    """Every module this file imports, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _referenced(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def test_the_agent_tree_imports_no_secret_write_path() -> None:
    offences: list[str] = []
    for path in _agent_modules():
        for imported in _imports(path):
            if any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in _FORBIDDEN_MODULES
            ):
                offences.append(f"{path}: imports {imported}")

    assert offences == []


def test_the_agent_tree_names_no_secret_write_symbol() -> None:
    offences: list[str] = []
    for path in _agent_modules():
        referenced = _referenced(path)
        offences += [f"{path}: {name}" for name in _FORBIDDEN_NAMES if name in referenced]

    assert offences == []


def test_the_executor_client_offers_no_secret_write() -> None:
    """The client the tool tree *does* import must not grow this method.

    `ExecuteOnServerTool` holds an `ExecutorClient`, so a `secret_write` method there would be
    callable from a tool with no import at all.
    """
    from nanoinfra.gates.executor.client import ExecutorClient

    surface = {name for name in dir(ExecutorClient) if not name.startswith("__")}
    assert not {"secret_write", "create_secret", "write_secret"} & surface
