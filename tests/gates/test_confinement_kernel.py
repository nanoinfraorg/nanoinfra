# tests/gates/test_confinement_kernel.py
"""The kernel enforces the confinement, and the privilege split still works (#20).

These tests ask the running kernel to apply a real ruleset. The policy says one thing, and the
kernel does another, so only a kernel test tells the truth about a sandbox.

Every test here skips on a kernel without Landlock support. A skip states that reason. A test that
passes on such a kernel would report a control that nothing applies.

The probe runs in a child process. A ruleset restricts the caller for the life of the process, so
an in-process test would confine the whole pytest session.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from nanoinfra.gates import confinement

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    confinement.landlock_abi() is None,
    reason="this kernel reports no landlock support, so no ruleset applies here",
)

_ABI = confinement.landlock_abi() or 0

_PROBE = r'''
import json
import os
import socket
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from nanoinfra.gates import confinement

spec = json.loads(sys.argv[1])
answers = {}


def check(name, action):
    try:
        action()
        answers[name] = "ok"
    except OSError as exc:
        answers[name] = "errno %d" % (exc.errno or 0)
    except BaseException as exc:  # noqa: BLE001 - the probe reports every fault
        answers[name] = "%s: %s" % (type(exc).__name__, exc)


def try_exec(path, argv):
    pid = os.fork()
    if pid == 0:
        try:
            os.execv(path, argv)
        except OSError as exc:
            os._exit(min(exc.errno or 1, 120))
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        raise OSError(code, "the exec was refused")


run_dir = Path(spec["run_dir"])
outside = Path(spec["outside"])
temp_root = Path(tempfile.mkdtemp())
written = temp_root / "written.sh"
written.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
written.chmod(0o755)

# The plan grants /tmp, and pytest builds its own paths under /tmp. So the grant would cover the
# run dir, the workspace, and the outside dir of this probe, and every check below would pass for
# the wrong reason. One private temp dir keeps the shape of the policy and isolates the role rules.
confinement._temp_dirs = lambda: [temp_root]

decision = confinement.plan_child(
    spec["role"], run_dir=run_dir, workspace=spec["workspace"]
)
plan = decision.plan
if spec.get("allowed_port"):
    plan = replace(plan, connect_ports=(spec["allowed_port"],), restrict_connect=True)
confinement.apply_plan(plan, abi=decision.abi)

check("bind_in_run_dir", lambda: socket.socket(socket.AF_UNIX).bind(str(run_dir / "probe.sock")))
check("bind_outside_run_dir", lambda: socket.socket(socket.AF_UNIX).bind(str(outside / "p.sock")))
check("connect_unix_outside", lambda: socket.socket(socket.AF_UNIX).connect(spec["listener"]))
check("read_credential_store", lambda: open(spec["secret"], "rb").read(1))
check("make_file_in_run_dir", lambda: open(run_dir / "note.txt", "w").close())
check("read_outside_dir", lambda: os.listdir(str(outside)))


def nested_socket():
    nested = run_dir / "operator"
    nested.mkdir()
    socket.socket(socket.AF_UNIX).bind(str(nested / "op.sock"))


check("nested_socket_dir_in_run_dir", nested_socket)
check("resolve_localhost", lambda: socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM))
check("tcp_listen", lambda: socket.socket().bind(("127.0.0.1", 0)))
check("exec_interpreter", lambda: try_exec(sys.executable, [sys.executable, "-c", ""]))
check("exec_written_program", lambda: try_exec(str(written), [str(written)]))
if spec.get("allowed_port"):
    check(
        "tcp_connect_allowed",
        lambda: socket.create_connection(("127.0.0.1", spec["allowed_port"]), timeout=5).close(),
    )
    check(
        "tcp_connect_denied",
        lambda: socket.create_connection(("127.0.0.1", spec["denied_port"]), timeout=5).close(),
    )

sys.stdout.write(json.dumps(answers))
sys.stdout.flush()
'''


def _listener(family: int, address: object) -> socket.socket:
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.bind(address)  # pyright: ignore[reportArgumentType]
    listener.listen(4)
    return listener


def _probe(role: str, tmp_path: Path, *, with_ports: bool = False) -> dict[str, str]:
    """Run the probe under the real rules of *role* and return its answers."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    secret = workspace / "secrets" / "cred.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("{}", encoding="utf-8")

    unix_listener = _listener(socket.AF_UNIX, str(outside / "peer.sock"))
    spec: dict[str, object] = {
        "role": role,
        "run_dir": str(run_dir),
        "outside": str(outside),
        "workspace": str(workspace),
        "secret": str(secret),
        "listener": str(outside / "peer.sock"),
    }
    tcp_listeners: list[socket.socket] = []
    if with_ports:
        allowed = _listener(socket.AF_INET, ("127.0.0.1", 0))
        denied = _listener(socket.AF_INET, ("127.0.0.1", 0))
        tcp_listeners = [allowed, denied]
        spec["allowed_port"] = allowed.getsockname()[1]
        spec["denied_port"] = denied.getsockname()[1]
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE, json.dumps(spec)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        unix_listener.close()
        for listener in tcp_listeners:
            listener.close()
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# ------------------------------------------------- the split keeps its sockets


@pytest.mark.parametrize(
    "role", [confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE]
)
def test_a_confined_child_binds_its_socket_in_the_run_dir(role: str, tmp_path: Path) -> None:
    """The privilege split needs this bind. A confinement that breaks it breaks the split."""
    answers = _probe(role, tmp_path)

    assert answers["bind_in_run_dir"] == "ok"
    assert answers["bind_outside_run_dir"] != "ok"


@pytest.mark.parametrize(
    "role", [confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE]
)
def test_a_confined_child_still_connects_to_a_unix_socket(role: str, tmp_path: Path) -> None:
    """Landlock governs a bind and governs no connect, so a confined child reaches its peers.

    The agent connects to the helper, and each helper answers. A rule set that broke this would
    make every gated action fail.
    """
    answers = _probe(role, tmp_path)

    assert answers["connect_unix_outside"] == "ok"


@pytest.mark.parametrize(
    "role", [confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE]
)
def test_a_confined_child_starts_the_interpreter_and_resolves_a_name(
    role: str, tmp_path: Path
) -> None:
    """Two things break first when a read root is missing.

    The exec of the entry point needs the interpreter and the loader. Name resolution needs four
    files in /etc that no manual page lists together.
    """
    answers = _probe(role, tmp_path)

    assert answers["exec_interpreter"] == "ok"
    assert answers["resolve_localhost"] == "ok"


# ------------------------------------------------------- the confinement bites


def test_the_fetcher_reads_no_credential_store(tmp_path: Path) -> None:
    """The process untrusted web content enters holds no path to the credentials."""
    answers = _probe(confinement.FETCHER_ROLE, tmp_path)

    assert answers["read_credential_store"] == "errno 13"
    assert answers["read_outside_dir"] == "errno 13"


def test_the_mcp_host_reads_no_credential_store(tmp_path: Path) -> None:
    """The host holds the exec right, so the credential store stays out of its reach."""
    answers = _probe(confinement.MCP_HOST_ROLE, tmp_path)

    assert answers["read_credential_store"] == "errno 13"


def test_the_executor_creates_its_operator_socket_dir(tmp_path: Path) -> None:
    """The executor binds a second socket in a subdirectory of the run dir.

    ``bind_operator_socket`` creates that subdirectory on the first start, so the run dir needs
    MAKE_DIR. The container puts the run dir under /run, and no other grant covers it there.
    """
    answers = _probe(confinement.EXECUTOR_ROLE, tmp_path)

    assert answers["nested_socket_dir_in_run_dir"] == "ok"


@pytest.mark.parametrize(
    "role", [confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE]
)
def test_a_confined_child_writes_no_file_in_the_run_dir(role: str, tmp_path: Path) -> None:
    """The run dir takes the rights a bind needs and no more.

    A pip install puts all three sockets in one dir, so a helper that could write a file there
    could plant one for another helper to read.
    """
    answers = _probe(role, tmp_path)

    assert answers["make_file_in_run_dir"] == "errno 13"


@pytest.mark.parametrize(
    "role", [confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE]
)
@pytest.mark.skipif(_ABI < 4, reason="tcp rules need landlock abi 4, and this kernel is older")
def test_a_confined_child_opens_no_tcp_listener(role: str, tmp_path: Path) -> None:
    """No helper listens on TCP. A listener would put a helper on the network."""
    answers = _probe(role, tmp_path)

    assert answers["tcp_listen"] == "errno 13"


@pytest.mark.parametrize(
    "role", [confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE]
)
def test_a_confined_child_runs_no_program_that_it_wrote(role: str, tmp_path: Path) -> None:
    """The exec surface holds no temp dir, so a script in one never runs."""
    answers = _probe(role, tmp_path)

    assert answers["exec_written_program"] == "errno 13"


@pytest.mark.skipif(_ABI < 4, reason="tcp rules need landlock abi 4, and this kernel is older")
def test_a_port_allowlist_refuses_every_other_port(tmp_path: Path) -> None:
    """The fetcher's egress policy, measured against two live listeners on one host."""
    answers = _probe(confinement.FETCHER_ROLE, tmp_path, with_ports=True)

    assert answers["tcp_connect_allowed"] == "ok"
    assert answers["tcp_connect_denied"] == "errno 13"


# ------------------------------------------------------- the real helper starts


def test_the_real_fetcher_starts_confined_and_answers_on_its_socket(tmp_path: Path) -> None:
    """The whole path, with the real entry point and the real rules.

    The start waits for one successful connect, so a handle proves the socket answers. The request
    that follows proves the confined process serves as well: it loads the config, it imports its
    HTTP client, and it reports the refusal of the SSRF guard.
    """
    from nanoinfra.gates.fetcher.client import FetcherClient
    from nanoinfra.gates.fetcher.supervisor import start_fetcher

    handle = start_fetcher(
        socket_path=tmp_path / "run" / "f.sock", workspace=tmp_path, timeout_s=60.0
    )
    try:
        assert handle.is_running() is True
        answer = FetcherClient(handle.socket_path).fetch(url="http://127.0.0.1/")
        # The SSRF guard refuses a loopback target, so this answer needs no network at all.
        assert answer.ok is True
        assert "validation failed" in answer.body
    finally:
        assert handle.stop(timeout_s=10) is True


def test_the_real_executor_starts_confined_and_answers_on_its_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor writes the job store and the audit log while it is confined.

    HOME moves to a temp dir, so this test writes no byte in a live data dir. The plan reads the
    data dir from the same HOME, so the child and the plan name one directory.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import nanoinfra.gates.executor.server"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if probe.returncode != 0:
        pytest.skip(f"the executor server does not import in this tree: {probe.stderr.strip()}")

    home = tmp_path / "home"
    (home / ".nanoinfra" / "gates").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from nanoinfra.gates.executor.supervisor import start_executor

    handle = start_executor(
        socket_path=tmp_path / "run" / "e.sock", workspace=workspace, timeout_s=60.0
    )
    try:
        assert handle.is_running() is True
        assert os.path.exists(handle.socket_path)
    finally:
        assert handle.stop(timeout_s=10) is True


def test_the_launcher_confines_and_then_starts_the_helper() -> None:
    """The container path, end to end, with the real kernel.

    The socket path stays short. A Unix socket path holds 108 bytes on Linux, and a pytest temp
    path plus a socket name passes that limit.
    """
    import shutil
    import tempfile
    import time

    root = Path(tempfile.mkdtemp(prefix="nlc", dir="/tmp"))
    run_dir = root / "run"
    run_dir.mkdir()
    socket_path = run_dir / "f.sock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nanoinfra.gates.confinement",
            "--role",
            "fetcher",
            "--socket",
            str(socket_path),
            "--workspace",
            str(root),
        ],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not socket_path.exists():
            assert child.poll() is None, "the launcher exited before it opened the socket"
            time.sleep(0.05)
        assert socket_path.exists()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.settimeout(5)
        peer.connect(str(socket_path))
        peer.close()
    finally:
        child.terminate()
        output = child.communicate(timeout=30)[0]
        shutil.rmtree(root, ignore_errors=True)

    assert "landlock abi" in output
