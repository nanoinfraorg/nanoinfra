# tests/gates/test_import_order.py
"""Every gates module must import first, on its own, in a fresh interpreter.

`nanoinfra/gates/latch.py` needs `ToolResult` at class-definition time, because TerminalDenial
subclasses it. Importing anything under `nanoinfra.agent` runs `nanoinfra/agent/__init__.py`,
which imports AgentLoop and therefore the runner. So a top-level `from nanoinfra.gates.latch
import ...` in the runner closes a cycle.

The cycle only appears in one import order. A test that imports inside this process cannot see
it, because the agent package is already loaded by the time the test runs. Each check
therefore uses a subprocess with a clean interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_MODULES = [
    "approvals",
    "audit",
    "executor.client",
    "executor.operator_socket",
    "executor.protocol",
    "latch",
    "pending",
    "policy",
    "prompt",
    "startup",
    "tokens",
]

_REPO_ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("module", _MODULES)
def test_the_module_imports_first_in_a_clean_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import nanoinfra.gates.{module}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]


def test_the_gates_package_imports_first_in_a_clean_interpreter() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import nanoinfra.gates"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
