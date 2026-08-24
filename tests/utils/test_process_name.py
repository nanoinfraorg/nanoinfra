"""The four processes of a split deployment answer to four different names."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nanoinfra.utils.process_name import (
    CLI_NAME,
    COMM_MAX_BYTES,
    EXECUTOR_NAME,
    FETCHER_NAME,
    GATEWAY_NAME,
    MCP_HOST_NAME,
    set_process_name,
)

_SHIPPED = (CLI_NAME, GATEWAY_NAME, EXECUTOR_NAME, FETCHER_NAME, MCP_HOST_NAME)

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="prctl is a Linux call")


def test_every_shipped_name_fits_the_kernel_limit() -> None:
    """A name the kernel truncates names the wrong thing, so the limit is checked here."""
    for name in _SHIPPED:
        assert len(name.encode()) <= COMM_MAX_BYTES, name
    assert len(set(_SHIPPED)) == len(_SHIPPED), "two processes cannot share one name"
    # The prefixed form is what did not fit, and this records why the names are bare.
    assert len(b"nanoinfra-gateway") > COMM_MAX_BYTES


@linux_only
def test_the_name_reaches_proc(tmp_path: Path) -> None:
    """A child process, so the test runner keeps its own name."""
    script = tmp_path / "named.py"
    script.write_text(
        "from nanoinfra.utils.process_name import set_process_name\n"
        "import pathlib\n"
        "print(set_process_name('exec'))\n"
        "print(pathlib.Path('/proc/self/comm').read_text().strip())\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == ["True", "exec"]


@linux_only
def test_a_name_over_the_limit_is_refused_rather_than_cut() -> None:
    assert set_process_name("nanoinfra-gateway") is False


@linux_only
def test_an_empty_name_is_refused() -> None:
    assert set_process_name("") is False


def test_each_helper_entry_point_names_itself() -> None:
    """The name has to be set after the exec `confinement.main` performs, which is the guard."""
    for module, expected in (
        ("nanoinfra/gates/executor/__main__.py", "EXECUTOR_NAME"),
        ("nanoinfra/gates/fetcher/__main__.py", "FETCHER_NAME"),
        ("nanoinfra/gates/mcp_host/__main__.py", "MCP_HOST_NAME"),
    ):
        source = Path(module).read_text(encoding="utf-8")
        guard = source.index('if __name__ == "__main__":')
        assert f"set_process_name({expected})" in source[guard:], module
        assert f"set_process_name({expected})" not in source[:guard], (
            f"{module} renames on import, which would rename anything that imports it"
        )
