# tests/cli/conftest.py
"""No CLI test starts a real helper process.

`_run_gateway` starts three children now: the executor (#40), the fetcher (#19), and the MCP host
(#22). Each one is a real interpreter that binds a socket, and a test that reaches the real start
leaves that child alive after the test ends. Ten such orphans came out of one session, and two of
them held the operator's own `~/.nanoinfra/run` sockets, which would collide with the next real
gateway start.

One test file already stubbed the three starts, and one test that never used that helper leaked
anyway. So the stub belongs here, where it covers the whole package and needs no test to remember
it. `tests/gates` covers each start on its own, and those tests keep their children under a
`tmp_path`.
"""

from __future__ import annotations

from typing import Any

import pytest

_HELPER_STARTS = (
    "_start_executor_for_gateway",
    "_start_fetcher_for_gateway",
    "_start_mcp_host_for_gateway",
)

# A CLI test may start the gateway as a real subprocess, and a monkeypatch in this process cannot
# reach that child. These are the variables a deployment sets when another supervisor already runs
# the helpers, so the child starts none of the three and needs no patch at all.
_EXTERNAL_ENV = (
    "NANOINFRA_EXECUTOR_EXTERNAL",
    "NANOINFRA_FETCHER_EXTERNAL",
    "NANOINFRA_MCP_HOST_EXTERNAL",
)


@pytest.fixture(autouse=True)
def no_real_helper_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace each helper start with one that answers None.

    None is what a failed start already returns, and every caller handles it, so the gateway path
    under test behaves as it does on a host where a helper cannot start.
    """
    for helper in _HELPER_STARTS:
        monkeypatch.setattr(
            f"nanoinfra.cli.gateway_runtime.{helper}",
            lambda _config: None,
            raising=True,
        )
    for name in _EXTERNAL_ENV:
        monkeypatch.setenv(name, "1")


def _helper_names() -> tuple[str, ...]:
    return _HELPER_STARTS


@pytest.fixture
def helper_start_names() -> tuple[str, ...]:
    """The three names, for a test that asserts the fixture covers all of them."""
    return _helper_names()


@pytest.fixture
def helper_external_env() -> tuple[str, ...]:
    """The three variables a spawned gateway reads, for the same reason."""
    return _EXTERNAL_ENV


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "real_helper_process: this test starts a real helper child and cleans it up itself",
    )
