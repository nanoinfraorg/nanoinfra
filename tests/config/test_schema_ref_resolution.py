# tests/config/test_schema_ref_resolution.py
"""A `Config` must be usable whatever the import order -- nanoinfraorg/nanoinfra#57.

`nanoinfra/config/schema.py` resolves its tool config references eagerly, and a comment promised a
lazy rebuild for the case the eager attempt hits a cycle. No lazy rebuild existed. So a process
that imported `nanoinfra.session.manager` first held a `Config` class that raised on every
construction, for the life of that process.

The cycle, captured by replacing the silent `except ImportError: pass` for one run:

    schema.py:698   _resolve_tool_config_refs()
    schema.py:672   from nanoinfra.agent.tools.cli_apps import CliAppsToolConfig
    agent/__init__.py:3   from nanoinfra.agent.context import ContextBuilder
    agent/context.py:9    from nanoinfra.agent.memory import MemoryStore
    agent/memory.py:25    from nanoinfra.session.manager import MIN_COMPACTED_REPLAY_MESSAGES, ...
    ImportError: cannot import name ... from partially initialized module

A late resolve in that same process succeeds, which proves the failure is a timing artifact rather
than a real dependency problem.

We noticed it through the test suite: `pytest tests/session/ tests/providers/` failed with 16 errors
while `pytest tests/providers/` alone passed 966. CI stayed green because a bare run collects
`providers` before `session`. The suite order is how we noticed, and the property is about the
import order of a process, so the tests below drive a real child rather than a collection order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Each program imports something else first, and then asks for a usable Config. The names are the
# modules that reproduced the defect: every one of them reaches `session.manager` before anything
# imports `nanoinfra.agent`.
_FIRST_IMPORTS = [
    "nanoinfra.session.manager",
    "nanoinfra.session.goal_state",
    "nanoinfra.config.schema",
    "nanoinfra.agent.tools.web",
]


def _build_a_config_after_importing(first: str) -> subprocess.CompletedProcess[str]:
    """Run a child that imports *first*, then builds a Config two ways.

    A child is the only honest test here. The property is about the first import of a process, and
    this process already imported everything.
    """
    # The payload carries a `tools` section on purpose. An unresolved reference only raises when a
    # value reaches the field that holds it, so a payload without one builds even while the class
    # reports itself incomplete. That is why this defect stayed hidden: most callers never touch
    # the field.
    program = (
        f"import {first}\n"
        "from nanoinfra.config.schema import Config\n"
        "payload = {\n"
        "    'agents': {'defaults': {'provider': 'ollama', 'model': 'ollama/qwen3'}},\n"
        "    'tools': {'web': {'search': {'enabled': False}}},\n"
        "}\n"
        "Config(**payload)\n"
        "Config.model_validate(payload)\n"
        "print('both built')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("first", _FIRST_IMPORTS)
def test_a_config_builds_whatever_the_first_import_was(first: str) -> None:
    completed = _build_a_config_after_importing(first)

    assert completed.returncode == 0, completed.stderr
    assert "both built" in completed.stdout


@pytest.mark.parametrize("first", _FIRST_IMPORTS)
def test_the_class_reports_itself_complete(first: str) -> None:
    """The attribute is what the retry reads, so a test reads it too.

    A Config that builds by luck and reports itself incomplete would break the next model that
    embeds it.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {first}\n"
            "from nanoinfra.config.schema import Config, ToolsConfig\n"
            "print(Config.__pydantic_complete__, ToolsConfig.__pydantic_complete__)\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "True True"


def test_the_retry_costs_one_attribute_read_after_the_first_success() -> None:
    """A retry that resolved the references again on every Config would be a real cost.

    `_resolve_tool_config_refs` imports eight modules and rebuilds two models.
    """
    from nanoinfra.config import schema

    calls = 0
    original = schema._resolve_tool_config_refs  # pyright: ignore[reportPrivateUsage]

    def counted() -> None:
        nonlocal calls
        calls += 1
        original()

    schema._resolve_tool_config_refs = counted  # pyright: ignore[reportPrivateUsage]
    try:
        for _ in range(50):
            schema.ensure_tool_config_refs()
    finally:
        schema._resolve_tool_config_refs = original  # pyright: ignore[reportPrivateUsage]

    assert calls == 0, "a complete Config must need no resolve at all"


def test_importing_one_tool_module_pulls_in_no_agent_context() -> None:
    """The invariant that keeps the cycle away (#57).

    `nanoinfra/config/schema.py` imports a tool config class from each tool module. Importing a
    submodule of a package runs that package's ``__init__``, so an eager re-export in
    ``nanoinfra/agent/__init__.py`` made that one import pull in the agent context, then the agent
    memory, then ``nanoinfra.session.manager``. That is the cycle.

    The re-exports are lazy now. This test fails the day somebody makes one eager again, which
    reads as an obvious tidy-up and closes the cycle a second time.
    """
    program = (
        "import sys\n"
        "import nanoinfra.agent.tools.web\n"
        "pulled = [m for m in ('nanoinfra.agent.context', 'nanoinfra.agent.memory',\n"
        "                      'nanoinfra.session.manager') if m in sys.modules]\n"
        "print(','.join(pulled))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        "importing one tool module pulled in "
        f"{completed.stdout.strip()}, which is the chain that closed the cycle"
    )


def test_the_package_still_exports_every_name() -> None:
    """A lazy re-export must be invisible to a caller.

    ``__getattr__`` answers each name from ``__all__``, and ``dir()`` lists them, so a reader and
    an ``import *`` both see what they saw before.
    """
    program = (
        "import nanoinfra.agent as agent\n"
        "missing = [n for n in agent.__all__ if not hasattr(agent, n)]\n"
        "print('missing:', missing)\n"
        "print('listed:', sorted(agent.__all__) == sorted(dir(agent)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert "missing: []" in completed.stdout
    assert "listed: True" in completed.stdout


def test_an_unknown_name_still_raises_an_attribute_error() -> None:
    """``__getattr__`` must not turn a typo into an import error or a None."""
    import nanoinfra.agent as agent

    with pytest.raises(AttributeError, match="has no attribute 'NoSuchThing'"):
        _ = agent.NoSuchThing  # pyright: ignore[reportAttributeAccessIssue]


# No test drives the debug log in `schema.py` for a swallowed ImportError. The cycle that produced
# one is gone, so a test would have to build a new cycle to reach it. The log stays because the
# next cycle must name itself: a silent `except ImportError: pass` cost a bisect over seven test
# files before this issue.
