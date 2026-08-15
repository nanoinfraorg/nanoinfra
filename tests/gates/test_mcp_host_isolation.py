# tests/gates/test_mcp_host_isolation.py
"""Item 20 (#22): where the exec right sits after the split, and where it does not.

A stdio MCP server is a subprocess. #19 states that the fetcher cannot exec. #22 keeps both
statements true by moving that one exec right into the MCP host process.

So three processes make three claims, and each claim is a test here:

- The MCP host may exec. It must hold nothing else: no credential store, and no HTTP transport.
  A compromise there yields the right to start a configured MCP server, and nothing more.
- The agent may not exec an MCP server. ``nanoinfra/agent/tools/mcp.py`` imports the host's client
  and its protocol only. It names neither ``stdio_client`` nor ``StdioServerParameters``.
- The fetcher may not exec at all. ``tests/gates/test_fetcher_isolation.py`` holds that half, and
  it now also refuses every import of this package.

The checks walk whole syntax trees. A lazy import inside a function would satisfy a grep and fail
these tests.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_HOST_PACKAGE = Path("nanoinfra/gates/mcp_host")
_AGENT_TOOL = Path("nanoinfra/agent/tools/mcp.py")

# The two files that are not part of the MCP host process. ``client.py`` runs in the agent, and
# ``supervisor.py`` runs on the supervisor's side and starts the child.
_NOT_IN_THE_HOST_PROCESS = {"client.py", "supervisor.py"}

# What the host must not be able to reach. A module that imports any of these holds the means to
# read a host credential or to dial a host.
_FORBIDDEN_PREFIXES = (
    "nanoinfra.secrets",
    "nanoinfra.servers",
    "nanoinfra.gates.executor",
    "nanoinfra.gates.fetcher",
)

# The host starts programs. It must not also reach the network, because those two rights together
# turn one compromised MCP server into an egress path with an exec right behind it.
_FORBIDDEN_TRANSPORT_IMPORTS = (
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "http.client",
    "socket",
)

# Every way a module could start a program. ``subprocess`` is the obvious one. The os family is the
# one a port from another codebase brings in by habit.
_FORBIDDEN_EXEC_IMPORTS = ("subprocess", "multiprocessing", "pty", "popen2", "commands")
_FORBIDDEN_EXEC_CALLS = (
    "system",
    "popen",
    "execl",
    "execle",
    "execlp",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnv",
    "spawnve",
    "posix_spawn",
    "posix_spawnp",
    "fork",
    "forkpty",
    "Popen",
)

# The two SDK names that start a stdio MCP server. Neither may appear in the agent's tool module.
_STDIO_STARTER_NAMES = ("stdio_client", "StdioServerParameters")


def _host_process_modules() -> list[Path]:
    """Every file the MCP host process loads.

    The list is computed rather than written down, so a new module in this package is covered on
    the day it lands rather than the day someone remembers to add it.
    """
    found = sorted(
        path for path in _HOST_PACKAGE.glob("*.py") if path.name not in _NOT_IN_THE_HOST_PROCESS
    )
    assert found, f"no MCP host modules found under {_HOST_PACKAGE}"
    return found


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


def _imported_names(path: Path) -> set[str]:
    """Every name the file imports, such as the ``stdio_client`` in ``from x import stdio_client``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _called_attributes(path: Path) -> set[str]:
    """Every attribute name the file calls, such as the ``system`` in ``os.system(...)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            called.add(target.attr)
        elif isinstance(target, ast.Name):
            called.add(target.id)
    return called


def _source_names(path: Path) -> set[str]:
    """Every identifier the file names anywhere, as a name or as an attribute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


# ------------------------------------------- the host holds no credential


def test_the_host_imports_no_credential_store_and_no_backend() -> None:
    """A compromise of the host must yield no host credential and no transport to a host."""
    offences: list[str] = []
    for path in _host_process_modules():
        imported = _imported_modules(path)
        offences += [
            f"{path}: {name}"
            for name in sorted(imported)
            for prefix in _FORBIDDEN_PREFIXES
            if name == prefix or name.startswith(f"{prefix}.")
        ]

    assert offences == []


def test_the_host_client_holds_no_credential_and_no_backend() -> None:
    """The client is agent-side code, so the same rule applies to it."""
    imported = _imported_modules(_HOST_PACKAGE / "client.py")

    assert [
        name
        for name in sorted(imported)
        for prefix in _FORBIDDEN_PREFIXES
        if name == prefix or name.startswith(f"{prefix}.")
    ] == []


def test_the_host_reaches_no_transport() -> None:
    """The host may start a program. It may not also dial one.

    HTTP and SSE MCP transports stay in the agent behind the SSRF guards of
    ``.agent/security.md``. ``load_stdio_settings`` refuses a server that is not stdio, and this
    test holds the import half of the same rule.
    """
    offences: list[str] = []
    for path in _host_process_modules():
        imported = _imported_modules(path)
        offences += [
            f"{path}: {name}" for name in _FORBIDDEN_TRANSPORT_IMPORTS if name in imported
        ]

    assert offences == []


def test_no_module_in_the_host_process_imports_the_supervisor() -> None:
    """The supervisor spawns the host, so it must stay outside the host.

    The supervisor is the one file in this package that imports subprocess. It runs on the other
    side of the split.
    """
    offences: list[str] = []
    for path in _host_process_modules():
        if "nanoinfra.gates.mcp_host.supervisor" in _imported_modules(path):
            offences.append(str(path))

    assert offences == []


def test_the_supervisor_source_runs_no_shell_and_no_exec() -> None:
    """The spawn that does exist accepts no command from a caller."""
    source = (_HOST_PACKAGE / "supervisor.py").read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "os.system" not in source
    assert "os.exec" not in source


def test_a_compromised_host_holds_no_secret_store_in_memory() -> None:
    """The scenario test: hold this process, and what is in reach?

    A fresh interpreter imports the host server, the way the child does, and reports which
    nanoinfra modules the import loaded. No credential store, no execution backend, and no
    executor or fetcher module may be among them.
    """
    program = (
        "import json, sys\n"
        "import nanoinfra.gates.mcp_host.server\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('nanoinfra'))\n"
        "print(json.dumps(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    loaded: list[str] = json.loads(result.stdout)
    reachable = [
        name
        for name in loaded
        for prefix in _FORBIDDEN_PREFIXES
        if name == prefix or name.startswith(f"{prefix}.")
    ]
    assert reachable == []


# ------------------------------------------- the agent starts no stdio child


def test_the_agent_tool_imports_nothing_that_starts_a_program() -> None:
    """The property the split exists for.

    Before #22 this module entered ``stdio_client`` and the agent process held the MCP server as
    its own child. The host holds it now.
    """
    imported = _imported_modules(_AGENT_TOOL)

    assert [name for name in _FORBIDDEN_EXEC_IMPORTS if name in imported] == []


def test_the_agent_tool_calls_no_exec_family_function() -> None:
    called = _called_attributes(_AGENT_TOOL)

    assert [name for name in _FORBIDDEN_EXEC_CALLS if name in called] == []


def test_the_agent_tool_names_no_stdio_starter() -> None:
    """Neither SDK name may appear anywhere in the agent's tool module.

    ``stdio_client`` starts the child. ``StdioServerParameters`` describes the command it runs. A
    module that names either one holds the means to start an MCP server in the agent process.
    """
    names = _source_names(_AGENT_TOOL)

    assert [name for name in _STDIO_STARTER_NAMES if name in names] == []


def test_the_agent_tool_imports_the_host_client_and_not_the_host_server() -> None:
    """The import direction is the enforcement.

    The agent reads ``client`` and ``protocol``. ``server`` holds the exec right, and
    ``supervisor`` holds the spawn. An import of either from the agent would undo the split.
    """
    imported = _imported_modules(_AGENT_TOOL)

    assert "nanoinfra.gates.mcp_host.client" in imported
    assert "nanoinfra.gates.mcp_host.server" not in imported
    assert "nanoinfra.gates.mcp_host.supervisor" not in imported


def test_the_host_client_starts_no_program() -> None:
    """The client runs in the agent, so it carries the agent's rule."""
    client = _HOST_PACKAGE / "client.py"
    imported = _imported_modules(client)
    called = _called_attributes(client)
    names = _imported_names(client)

    assert [name for name in _FORBIDDEN_EXEC_IMPORTS if name in imported] == []
    assert [name for name in _FORBIDDEN_EXEC_CALLS if name in called] == []
    assert [name for name in _STDIO_STARTER_NAMES if name in names] == []


def test_the_agent_process_starts_no_stdio_child_on_import() -> None:
    """A fresh interpreter imports the agent's MCP tool and holds no stdio starter.

    The import check above reads the source. This one reads the loaded module, so a re-export from
    another module would fail it too.
    """
    program = (
        "import json\n"
        "import nanoinfra.agent.tools.mcp as tool\n"
        "print(json.dumps([name for name in ('stdio_client', 'StdioServerParameters')"
        " if hasattr(tool, name)]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
