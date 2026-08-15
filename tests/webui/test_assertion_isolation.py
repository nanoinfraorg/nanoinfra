# tests/webui/test_assertion_isolation.py
"""Item 3 of M1 (#60): no tool module reaches the JWKS client, and the cost is recorded.

#59 put an outbound HTTP client in the gateway process, and that weakens a boundary #19
built. The statement to preserve is that the agent process holds no transport.

The closure below is the third of the four things that carry it. It walks the whole first-party
import graph from every module under ``nanoinfra/agent/tools/``, so a two-hop path is caught as
well as a direct import, and a lazy import inside a function is caught as well as one at the
top of a file. The same technique holds the approval answer surface (#43,
``tests/command/test_approval_commands.py``) and the redaction split (#41,
``tests/agent/test_redaction_isolation.py``).

The fourth thing is not a check and cannot be one: **a compromised agent that runs arbitrary
code inside the gateway defeats this closure.** It then holds one HTTP client aimed at one
host. The last test in this file asserts that ``.agent/security.md`` still records that, beside
the other accepted risks.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

_PACKAGE = Path("nanoinfra")
_TOOLS = Path("nanoinfra/agent/tools")
_SECURITY_RECORD = Path(".agent/security.md")

# The module that holds the outbound client. A tool that cannot import it cannot fetch.
_JWKS_CLIENT = "nanoinfra.webui.assertion_jwks"

# The two address guards, and the rule about which one this path uses. The narrow guard allows
# RFC1918, because a homelab identity provider lives there. The wide guard blocks RFC1918 and
# would refuse a private provider outright, so an import of it here would be a regression that
# looks like a hardening.
_NARROW_GUARD = "nanoinfra.servers.network_guard"
_WIDE_GUARD = "nanoinfra.security.network"


def _imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, at any depth, including inside a function."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _first_party_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in _PACKAGE.rglob("*.py"):
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _import_closure(*seed_prefixes: str) -> set[str]:
    """Every first-party module the seeds reach, transitively.

    A one-level check passes while a two-hop path stays open, so this walks the whole graph.
    """
    modules = _first_party_modules()
    graph = {
        name: {edge for edge in _imported_modules(path) if edge in modules}
        for name, path in modules.items()
    }
    seeds = [
        name
        for name in modules
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in seed_prefixes)
    ]
    assert seeds, f"no modules found for {seed_prefixes}"
    seen = set(seeds)
    queue = collections.deque(seeds)
    while queue:
        for edge in graph.get(queue.popleft(), ()):
            if edge not in seen:
                seen.add(edge)
                queue.append(edge)
    return seen


def test_no_tool_module_reaches_the_jwks_client() -> None:
    """The acceptance criterion of #60, as a check rather than a promise."""
    closure = _import_closure("nanoinfra.agent.tools")

    assert _JWKS_CLIENT not in closure


def test_no_tool_module_imports_the_jwks_client_directly() -> None:
    """The one-hop half, named separately so a failure says which file to look at."""
    offenders = [
        str(path)
        for path in _TOOLS.rglob("*.py")
        if _JWKS_CLIENT in _imported_modules(path)
    ]

    assert offenders == []


def test_the_closure_is_not_vacuous() -> None:
    """The gateway does reach the client, so a rename cannot leave the checks above passing.

    Without this test, a move of ``assertion_jwks.py`` would make every assertion above true
    for the wrong reason, and the closure would guard a module that no longer exists.
    """
    closure = _import_closure("nanoinfra.channels.websocket.runtime")

    assert _JWKS_CLIENT in closure


def test_the_jwks_client_uses_the_narrow_address_guard() -> None:
    """#59 rule 2, as a structural check beside the behavioural one.

    ``tests/webui/test_assertion_jwks.py`` asserts that RFC1918 is allowed and that the
    metadata address is blocked. This test names the module that must supply that verdict, so
    a swap to the wide guard fails here even if somebody also changed the expectations there.
    """
    imported = _imported_modules(Path("nanoinfra/webui/assertion_jwks.py"))

    assert _NARROW_GUARD in imported
    assert _WIDE_GUARD not in imported


def test_the_widened_egress_surface_is_recorded() -> None:
    """The fourth carrier of the boundary is a written record, so a check keeps it written.

    A closure with an undocumented defeat is worse than no closure, because a reader concludes
    the boundary is absolute. This asserts the record still names the module and still names
    what defeats it.
    """
    record = _SECURITY_RECORD.read_text(encoding="utf-8")

    assert "assertion_jwks" in record
    assert "defeats" in record
    assert "agent/tools/" in record
