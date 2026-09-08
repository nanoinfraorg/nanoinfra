"""A managed child's `print()` output has to reach the log file (upstream PR 5412).

`_start_background` redirects the child's stdout to a log file, and with output redirected
`sys.stdout.line_buffering` is False. loguru writes to stderr and survives that, but the plain
`print()` calls in `cli/gateway_runtime.py` sit in a 8 KB block buffer -- so a child that hangs or
is killed leaves an empty log, exactly when somebody is reading it to find out why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.gateway import GatewayRuntime, GatewayRuntimePaths, GatewayStartOptions


class _FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


def test_the_background_child_runs_unbuffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_popen(_command: list[str], **kwargs: Any) -> _FakeProcess:
        calls.append(kwargs)
        return _FakeProcess()

    runtime = GatewayRuntime(
        paths=GatewayRuntimePaths.for_instance(data_dir=tmp_path),
        platform_name="Linux",
        python_executable="/python",
        popen=fake_popen,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(runtime, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(runtime, "_process_identity", lambda _pid: 4242)
    monkeypatch.setenv("NANOINFRA_TEST_INHERITED", "kept")

    result = runtime.start_background(GatewayStartOptions(port=18790))

    assert result.ok is True
    env = calls[0]["env"]
    assert env["PYTHONUNBUFFERED"] == "1"
    # The child still gets everything else it was started with -- provider keys included.
    assert env["NANOINFRA_TEST_INHERITED"] == "kept"
