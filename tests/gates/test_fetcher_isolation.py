# tests/gates/test_fetcher_isolation.py
"""Item 16 (#19): what holding the fetcher gets an attacker, and what it does not.

The fetcher reads pages a stranger wrote. So the question a test has to answer is not "does the
fetch work" but "what does a compromise of this process yield". The answer must be: network reach
and a search provider key, and nothing else. No transport to a host, no host credential, and no
way to run a program.

Two of these properties are structural, and structural is what makes them checkable rather than
merely intended:

- The fetcher imports neither the credential store nor an execution backend. A module that imports
  one holds the means to read a credential or to dial a host.
- The fetcher cannot exec. A stdio MCP server is a subprocess, so #22 had to answer this property
  rather than delete it. #22 answered it with a separate MCP host process
  (``nanoinfra/gates/mcp_host/``), and the tests at the end of this module hold the fetcher's half:
  no module here reaches that package, so the fetcher gained no exec right.

The checks walk the whole syntax tree of every module the fetcher process loads. A lazy import
inside a function would satisfy a grep and fail this test.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_PACKAGE = Path("nanoinfra/gates/fetcher")

# The two files that are not part of the fetcher process. ``client.py`` runs in the agent, and
# ``supervisor.py`` runs on the supervisor's side and starts the child. The supervisor is the one
# file in this package that imports subprocess, and a test below asserts that no module the fetcher
# loads imports the supervisor. So the exec property stays true inside the process.
_NOT_IN_THE_FETCHER_PROCESS = {"client.py", "supervisor.py"}

# What the fetcher must not be able to reach. A module that imports any of these holds the means to
# dial a host or to read a host credential.
_FORBIDDEN_IMPORTS = (
    "nanoinfra.secrets.store",
    "nanoinfra.servers.execution.ssh_backend",
    "nanoinfra.servers.execution.ansible_backend",
    "nanoinfra.servers.execution.ssm_backend",
    "nanoinfra.servers.execution.api_backend",
    "nanoinfra.gates.executor.server",
    "nanoinfra.gates.executor.client",
)

# The prefix form of the same rule. A new backend, a new secret module, or a new executor module
# must not need an edit here to be refused.
#
# ``nanoinfra.gates.mcp_host`` joins the list with #22. That package holds the exec right for stdio
# MCP servers. The fetcher must reach none of it, because a fetcher that could ask the MCP host to
# start a server would hold an exec right by proxy.
_FORBIDDEN_PREFIXES = (
    "nanoinfra.secrets",
    "nanoinfra.servers",
    "nanoinfra.gates.executor",
    "nanoinfra.gates.mcp_host",
)

# Every way this package could start a program. ``subprocess`` is the obvious one. The os family is
# the one a port from another codebase brings in by habit.
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
# ``run`` and ``call`` are not on the list. ``asyncio.run`` and a method named ``run`` both carry
# the name, and a check that flags them would get muted rather than fixed. The import check above
# refuses ``subprocess`` outright, which is the only way those names reach a program from here.


def _process_modules() -> list[Path]:
    """Every file the fetcher process loads.

    The list is computed rather than written down, so a new module in this package is covered on
    the day it lands rather than the day someone remembers to add it.
    """
    found = sorted(
        path
        for path in _PACKAGE.glob("*.py")
        if path.name not in _NOT_IN_THE_FETCHER_PROCESS
    )
    assert found, f"no fetcher modules found under {_PACKAGE}"
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


# ------------------------------------------------- no credential, no transport


def test_the_fetcher_imports_no_credential_store_and_no_backend() -> None:
    """The acceptance criterion of #19, as a check rather than a promise."""
    offences: list[str] = []
    for path in _process_modules():
        imported = _imported_modules(path)
        offences += [f"{path}: {name}" for name in _FORBIDDEN_IMPORTS if name in imported]
        offences += [
            f"{path}: {name}"
            for name in sorted(imported)
            for prefix in _FORBIDDEN_PREFIXES
            if name == prefix or name.startswith(f"{prefix}.")
        ]

    assert offences == []


def test_the_fetcher_client_holds_no_credential_and_no_backend() -> None:
    """The client is agent-side code, so the same rule applies to it."""
    imported = _imported_modules(_PACKAGE / "client.py")

    assert [name for name in _FORBIDDEN_IMPORTS if name in imported] == []


def test_the_fetcher_modules_expose_no_credential_or_backend_object() -> None:
    """The runtime half of the same property."""
    import nanoinfra.gates.fetcher.fetch as fetch_module
    import nanoinfra.gates.fetcher.search as search_module
    import nanoinfra.gates.fetcher.server as server_module

    for module in (server_module, fetch_module, search_module):
        for attribute in ("SecretStore", "SSHBackend", "AnsibleRunnerBackend", "Executor"):
            assert not hasattr(module, attribute), f"{module.__name__}.{attribute}"


# ------------------------------------------------------------- cannot exec


def test_the_fetcher_imports_nothing_that_starts_a_program() -> None:
    """The property #22 had to preserve.

    Stdio MCP servers are subprocesses, so "MCP moves to the fetcher" and "the fetcher cannot exec"
    cannot both be true as written. #22 owned that contradiction, and it resolved it with the MCP
    host process. This test states the half that held before and still holds.
    """
    offences: list[str] = []
    for path in _process_modules():
        imported = _imported_modules(path)
        offences += [f"{path}: {name}" for name in _FORBIDDEN_EXEC_IMPORTS if name in imported]

    assert offences == []


def test_the_fetcher_calls_no_exec_family_function() -> None:
    """``os.system`` and the ``os.exec*`` family, by name, anywhere in the tree."""
    offences: list[str] = []
    for path in _process_modules():
        called = _called_attributes(path)
        offences += [f"{path}: {name}" for name in _FORBIDDEN_EXEC_CALLS if name in called]

    assert offences == []


def test_no_module_in_the_fetcher_process_imports_the_supervisor() -> None:
    """The supervisor spawns the child, so it must stay outside the child.

    The supervisor is the one file in this package that imports subprocess. It runs on the other
    side of the split. An import of it from a module the fetcher loads would put a spawn back
    inside the process that reads untrusted content.
    """
    offences: list[str] = []
    for path in _process_modules():
        imported = _imported_modules(path)
        if "nanoinfra.gates.fetcher.supervisor" in imported:
            offences.append(str(path))

    assert offences == []


def test_the_supervisor_source_runs_no_shell_and_no_exec() -> None:
    """The spawn that does exist accepts no command from a caller."""
    source = (_PACKAGE / "supervisor.py").read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "os.system" not in source
    assert "os.exec" not in source


# ------------------------------------------------- the compromised fetcher


def test_a_compromised_fetcher_has_no_transport_and_no_secret_store_in_memory() -> None:
    """The scenario test: hold this process, and what is in reach?

    A fresh interpreter imports the fetcher server, the way the child does, and reports which
    nanoinfra modules the import loaded. No credential store, no execution backend, and no executor
    module may be among them. An attacker inside this process therefore holds no object that reads
    a credential and no object that dials a host.

    The check names nanoinfra modules only. ``loguru`` pulls in ``multiprocessing``, which pulls in
    ``subprocess``, so CPython always has that module somewhere in reach. A kernel-level no-exec
    belongs to the deployment, and the source-level checks above are what a test can hold.
    """
    program = (
        "import json, sys\n"
        "import nanoinfra.gates.fetcher.server\n"
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


def test_a_compromised_fetcher_cannot_reach_the_executor_socket_by_import() -> None:
    """No route to a transport means no route to the process that owns one either.

    The executor holds the credential store and the four transports (#18). A fetcher that could
    ask the executor to run a command would hand a page's author a shell on a host. The import
    check above refuses that route. The socket itself is kept away by the run directory mode and by
    the two-uid deployment, because a path is not a permission.
    """
    import nanoinfra.gates.fetcher.server as server_module

    for attribute in ("ExecutorClient", "execute_on_server"):
        assert not hasattr(server_module, attribute)


def test_the_fetcher_runs_no_stdio_mcp_server() -> None:
    """Item 20 (#22): stdio MCP did not land here, and it must not.

    A stdio MCP server is a subprocess. The fetcher reads pages a stranger wrote, so an exec right
    in this process would turn one hostile page into a program start. #22 put that right in
    ``nanoinfra/gates/mcp_host/`` instead. No module of the fetcher may name the MCP SDK either,
    because a client session there would need a transport or a child.
    """
    offences: list[str] = []
    for path in [*_process_modules(), _PACKAGE / "client.py", _PACKAGE / "supervisor.py"]:
        imported = _imported_modules(path)
        offences += [
            f"{path}: {name}"
            for name in sorted(imported)
            if name == "mcp" or name.startswith("mcp.")
        ]

    assert offences == []


def test_no_file_in_the_fetcher_package_reaches_the_mcp_host() -> None:
    """The MCP host holds an exec right. The fetcher reaches none of that package.

    The check covers ``client.py`` and ``supervisor.py`` as well, because the rule is about the
    whole fetcher lane rather than only the process.
    """
    offences: list[str] = []
    for path in [*_process_modules(), _PACKAGE / "client.py", _PACKAGE / "supervisor.py"]:
        imported = _imported_modules(path)
        offences += [
            f"{path}: {name}"
            for name in sorted(imported)
            if name == "nanoinfra.gates.mcp_host"
            or name.startswith("nanoinfra.gates.mcp_host.")
        ]

    assert offences == []


def test_the_reply_carries_no_credential_field() -> None:
    """The fetcher holds the search key. The reply has nowhere to put it.

    The agent must not learn the key, because the model reads what the agent holds.
    """
    from nanoinfra.gates.fetcher.protocol import FetchResponse

    fields = set(FetchResponse.__dataclass_fields__)

    assert fields == {"ok", "body", "blocks", "is_error", "error"}
