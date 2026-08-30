# tests/agent/test_redaction_isolation.py
"""Item 39 (#41): the agent process builds no redaction sentinel.

#18 moved the credential store behind the executor. ``workspace_secret_sentinels`` then
decrypted every secret of the workspace inside the agent process, on every turn that
persisted a transcript. A prompt injection reached no plaintext through a tool. An arbitrary
read of that process memory reached every one of them.

The checks below walk the syntax tree of every module the agent process can load. A lazy
import inside a function satisfies a grep and fails these tests.

Two names carry the rule. ``resolve_plaintext`` is the one seam that decrypts a secret.
``workspace_secret_sentinels`` is the function that calls it for every secret at once.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE = Path("nanoinfra")

# The two names no module in the agent process may reach.
_FORBIDDEN_NAMES = ("resolve_plaintext", "workspace_secret_sentinels")

# The module that holds the sentinel build after #41. The agent must not import it either,
# because an import of it puts the whole credential store back in reach.
_SCRUB_MODULE = "nanoinfra.gates.executor.scrub"

# What the agent process does not load.
#
# ``nanoinfra/secrets`` defines the decryption seam, and the Secrets REST lane owns it.
# ``tests/secrets/test_no_plaintext_leak_invariant.py`` holds that half.
#
# The executor files run in the executor child. ``server.py`` resolves one credential for one
# action (#18). ``scrub.py`` builds the sentinels (#41). ``connector_credentials.py`` exchanges
# a connector's refresh token for a short-lived access token, and ``connector_action.py`` is the
# chain that spends it -- a connector call is performed in this process for the same reason a
# command is, so the agent holds no token at all. Each is an address space that is allowed to
# hold a plaintext.
_NOT_IN_THE_AGENT_PROCESS = (
    Path("nanoinfra/secrets"),
    Path("nanoinfra/gates/executor/server.py"),
    Path("nanoinfra/gates/executor/scrub.py"),
    Path("nanoinfra/gates/executor/connector_credentials.py"),
    Path("nanoinfra/gates/executor/connector_action.py"),
)


def _agent_process_modules() -> list[Path]:
    """Every module the agent process can load.

    The list is computed rather than written down. So a new module is covered on the day it
    lands rather than the day somebody remembers to add it.
    """
    found = sorted(
        path
        for path in _PACKAGE.rglob("*.py")
        if not any(_is_under(path, excluded) for excluded in _NOT_IN_THE_AGENT_PROCESS)
    )
    assert found, f"no modules found under {_PACKAGE}"
    return found


def _is_under(path: Path, excluded: Path) -> bool:
    return path == excluded or excluded in path.parents


def _referenced_names(path: Path) -> set[str]:
    """Every identifier and attribute name the file names, at any depth.

    A docstring that quotes a name does not count. That is the reason this walks the tree
    rather than the text: several modules explain the rule in prose, and prose is not a call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _imported_modules(path: Path) -> set[str]:
    """Every module name the file imports, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_module_in_the_agent_process_reaches_a_secret_plaintext() -> None:
    """The acceptance criterion of #41, as a check rather than a promise."""
    offences: list[str] = []
    for path in _agent_process_modules():
        referenced = _referenced_names(path)
        offences += [f"{path}: {name}" for name in _FORBIDDEN_NAMES if name in referenced]

    assert offences == []


def test_no_module_in_the_agent_process_imports_the_scrub_service() -> None:
    """The sentinel build lives in one module. An import of it undoes the split."""
    offences: list[str] = []
    for path in _agent_process_modules():
        imported = _imported_modules(path)
        if any(
            name == _SCRUB_MODULE or name.startswith(f"{_SCRUB_MODULE}.") for name in imported
        ):
            offences.append(str(path))

    assert offences == []


def test_the_agent_redaction_module_exposes_no_sentinel_builder() -> None:
    """The runtime half of the same property."""
    import nanoinfra.agent.redaction as redaction

    for name in _FORBIDDEN_NAMES:
        assert not hasattr(redaction, name), f"nanoinfra.agent.redaction.{name}"
    assert not hasattr(redaction, "SecretStore")


def test_the_check_is_not_vacuous() -> None:
    """The scrub service does name the sentinel builder, so a blind check fails here.

    Without this test a rename of either forbidden name would leave every check above
    passing for the wrong reason.
    """
    referenced = _referenced_names(Path("nanoinfra/gates/executor/scrub.py"))

    assert "workspace_secret_sentinels" in referenced
    assert "resolve_plaintext" in referenced
