# tests/gates/test_confinement.py
"""The confinement layer each helper process starts under (#20).

These tests read the plan and the decision. They need no Landlock support, because a plan is a
value. ``test_confinement_kernel.py`` holds the tests that ask the kernel to enforce the plan.

Two properties matter more than the rule lists. The first: a failure to confine is loud. The
second: confinement never replaces the privilege split, so the sockets the split needs stay
reachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates import confinement
from nanoinfra.gates.executor import supervisor as executor_supervisor
from nanoinfra.gates.fetcher import supervisor as fetcher_supervisor
from nanoinfra.gates.mcp_host import supervisor as mcp_host_supervisor
from nanoinfra.gates.startup import policy_summary

ROLES = (confinement.EXECUTOR_ROLE, confinement.FETCHER_ROLE, confinement.MCP_HOST_ROLE)


def _plan(role: str, tmp_path: Path) -> confinement.ConfinementPlan:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    (workspace / "secrets").mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}", encoding="utf-8")
    return confinement.plan_child(
        role,
        run_dir=run_dir,
        workspace=workspace,
        config_path=config_path,
        data_dir=config_path.parent,
    ).plan


def _granted(plan: confinement.ConfinementPlan, path: Path) -> int:
    """The access bits the plan grants on *path*, or 0 when no rule names it."""
    for rule in plan.rules:
        if rule.path == path:
            return rule.access
    return 0


def _covers(plan: confinement.ConfinementPlan, path: Path) -> bool:
    """Report whether one rule names *path* or an ancestor of it."""
    return any(path == rule.path or path.is_relative_to(rule.path) for rule in plan.rules)


# ------------------------------------------------------------------ kernel support


def test_the_module_reports_a_landlock_abi_or_none() -> None:
    """The probe answers with a version or with None. It never raises."""
    abi = confinement.landlock_abi()

    assert abi is None or abi >= 1


def test_a_kernel_without_landlock_degrades_and_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kernel with no Landlock support is a legitimate host, so the start continues."""
    monkeypatch.setattr(confinement, "landlock_abi", lambda: None)

    decision = confinement.plan_child(confinement.FETCHER_ROLE, run_dir=tmp_path)

    assert decision.layer == confinement.LAYER_NONE
    assert decision.preexec() is None
    assert decision.reason is not None
    assert "landlock" in decision.summary().lower()
    assert "not applied" in decision.summary().lower()


def test_a_kernel_with_landlock_applies_the_landlock_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 4)

    decision = confinement.plan_child(confinement.FETCHER_ROLE, run_dir=tmp_path)

    assert decision.layer == confinement.LAYER_LANDLOCK
    assert decision.abi == 4
    assert decision.preexec() is not None
    assert "abi 4" in decision.summary().lower()


def test_an_old_abi_reports_the_controls_it_cannot_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TCP rules need ABI 4. An operator must read which control the kernel lacks."""
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 1)

    summary = confinement.plan_child(confinement.FETCHER_ROLE, run_dir=tmp_path).summary()

    assert "tcp" in summary.lower()


# ---------------------------------------------------------- the split stays reachable


@pytest.mark.parametrize("role", ROLES)
def test_every_plan_lets_the_child_create_its_socket(role: str, tmp_path: Path) -> None:
    """A bind creates a socket node, so the run dir needs MAKE_SOCK and REMOVE_FILE.

    Without these two the child cannot bind, and a confinement that breaks the socket breaks the
    privilege split it is meant to complement.
    """
    plan = _plan(role, tmp_path)

    access = _granted(plan, tmp_path / "run")
    assert access & confinement.FS_MAKE_SOCK
    assert access & confinement.FS_REMOVE_FILE
    assert access & confinement.FS_WRITE_FILE
    assert access & confinement.FS_READ_DIR


@pytest.mark.parametrize("role", ROLES)
def test_every_plan_requires_the_run_dir(role: str, tmp_path: Path) -> None:
    """An absent run dir refuses the start, because the child cannot serve without it."""
    plan = _plan(role, tmp_path)

    required = [str(rule.path) for rule in plan.rules if rule.required]
    assert str(tmp_path / "run") in required


@pytest.mark.parametrize("role", ROLES)
def test_every_plan_denies_a_tcp_listener(role: str, tmp_path: Path) -> None:
    """No helper listens on TCP. Each one answers on a Unix socket."""
    plan = _plan(role, tmp_path)

    assert plan.restrict_bind is True
    assert plan.bind_ports == ()


@pytest.mark.parametrize("role", ROLES)
def test_every_plan_lets_the_child_exec_the_interpreter(role: str, tmp_path: Path) -> None:
    """The rules apply before the exec of the entry point, so the interpreter needs EXECUTE.

    The ELF loader needs it too. The kernel opens the loader with exec intent, and a missing
    grant there fails the exec of every dynamically linked program.
    """
    plan = _plan(role, tmp_path)
    interpreter = Path(os.path.realpath(sys.executable))

    assert _granted(plan, interpreter) & confinement.FS_EXECUTE
    loaders = [
        rule
        for rule in plan.rules
        if rule.access & confinement.FS_EXECUTE and rule.path.name.startswith("ld-")
    ]
    assert loaders != []


# ------------------------------------------------------------------- the fetcher


def test_the_fetcher_plan_allows_the_web_ports_only(tmp_path: Path) -> None:
    plan = _plan(confinement.FETCHER_ROLE, tmp_path)

    assert plan.restrict_connect is True
    assert set(plan.connect_ports) == {53, 80, 443}


def test_the_fetcher_plan_adds_a_proxy_port_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment behind a proxy on port 3128 must keep working."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")

    plan = _plan(confinement.FETCHER_ROLE, tmp_path)

    assert 3128 in plan.connect_ports


def test_the_fetcher_plan_reaches_no_inventory_host_and_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#20 states it plainly: the fetcher reads no inventory host.

    The workspace holds the server inventory and the credential store, so no rule may name the
    workspace or an ancestor of it.

    The temp dir grant drops out of this test. pytest builds its own paths under /tmp, and a real
    workspace lives under the home dir rather than under the temp dir.
    """
    monkeypatch.setattr(confinement, "_temp_dirs", lambda: [])
    plan = _plan(confinement.FETCHER_ROLE, tmp_path)

    assert not _covers(plan, tmp_path / "workspace" / "secrets")
    assert not _covers(plan, tmp_path / "workspace" / "servers")


def test_the_fetcher_plan_reads_the_config_file_and_not_the_data_dir(tmp_path: Path) -> None:
    """The fetcher loads the search provider and the proxy from config.json, and nothing else."""
    plan = _plan(confinement.FETCHER_ROLE, tmp_path)

    assert _granted(plan, tmp_path / "data" / "config.json") & confinement.FS_READ_FILE
    assert _granted(plan, tmp_path / "data") == 0


# ------------------------------------------------------------------ the executor


def test_the_executor_plan_restricts_no_outbound_port(tmp_path: Path) -> None:
    """Contact with inventory hosts is its purpose, so a port allowlist would restrict nothing."""
    plan = _plan(confinement.EXECUTOR_ROLE, tmp_path)

    assert plan.restrict_connect is False
    assert plan.connect_ports == ()


def test_the_executor_plan_writes_the_workspace_and_the_data_dir(tmp_path: Path) -> None:
    """The job store lives under the workspace. The gate audit log lives under the data dir."""
    plan = _plan(confinement.EXECUTOR_ROLE, tmp_path)

    assert _granted(plan, tmp_path / "workspace") & confinement.FS_MAKE_REG
    assert _granted(plan, tmp_path / "data") & confinement.FS_MAKE_REG


def test_the_executor_plan_writes_the_working_dir(tmp_path: Path) -> None:
    """ansible-runner writes its artifacts in the working dir when no projectPath names one."""
    plan = _plan(confinement.EXECUTOR_ROLE, tmp_path)

    assert _granted(plan, Path.cwd()) & confinement.FS_MAKE_REG


def test_the_executor_plan_bounds_the_exec_surface(tmp_path: Path) -> None:
    """The executor runs ansible and ssh, so system bin dirs stay in the exec surface.

    The workspace, the data dir, and the temp dir stay out of it. An agent writes files in those
    three, and a program the agent wrote must never run here.
    """
    plan = _plan(confinement.EXECUTOR_ROLE, tmp_path)

    for path in (tmp_path / "workspace", tmp_path / "data", Path("/tmp")):
        assert not _granted(plan, path) & confinement.FS_EXECUTE


# ------------------------------------------------------------------ the MCP host


def test_the_mcp_host_plan_keeps_the_credential_store_out_of_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host starts a program that a config in the agent's reach names.

    So the host is the process where confinement matters most. It reads no workspace at all: its
    own entry point uses the workspace for one log line.

    The temp dir grant drops out of this test, for the reason the fetcher test states.
    """
    monkeypatch.setattr(confinement, "_temp_dirs", lambda: [])
    plan = _plan(confinement.MCP_HOST_ROLE, tmp_path)

    assert not _covers(plan, tmp_path / "workspace" / "secrets")


def test_the_mcp_host_plan_bounds_the_exec_surface(tmp_path: Path) -> None:
    """A stdio server comes from a package manager cache or from a system bin dir."""
    plan = _plan(confinement.MCP_HOST_ROLE, tmp_path)

    exec_paths = {str(rule.path) for rule in plan.rules if rule.access & confinement.FS_EXECUTE}
    assert any(path.endswith("bin") for path in exec_paths)
    assert not _granted(plan, tmp_path / "run") & confinement.FS_EXECUTE


# --------------------------------------------------------------- the startup echo


def test_the_startup_echo_reports_the_confinement() -> None:
    """The security checklist an operator reads must name the sandbox as a control."""
    summary = policy_summary(GatesConfig())

    assert "confinement" in summary.lower()
    assert "landlock" in summary.lower()


def test_the_startup_echo_still_names_a_shipped_policy_as_such() -> None:
    """The confinement clause must not disturb the claim #8 makes about written policy."""
    written = GatesConfig.model_validate({"unattended": {"mutate.remote": {"host": "grant"}}})

    assert "default" not in policy_summary(written).lower()


def test_a_kernel_without_landlock_says_so_in_the_startup_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confinement, "landlock_abi", lambda: None)

    summary = policy_summary(GatesConfig())

    assert "not applied" in summary.lower()


# ------------------------------------------------------------ the three supervisors


@pytest.mark.parametrize(
    ("module", "runtime_name"),
    [
        (executor_supervisor, "ExecutorRuntime"),
        (fetcher_supervisor, "FetcherRuntime"),
        (mcp_host_supervisor, "MCPHostRuntime"),
    ],
)
def test_each_supervisor_confines_its_child(
    module: Any, runtime_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shared module, three supervisors. Three copies of a policy would drift apart."""
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 4)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runtime = getattr(module, runtime_name)(
        socket_path=run_dir / "s.sock", workspace=tmp_path / "workspace"
    )

    kwargs = runtime._popen_platform_kwargs()

    assert callable(kwargs["preexec_fn"])


@pytest.mark.parametrize(
    ("module", "runtime_name"),
    [
        (executor_supervisor, "ExecutorRuntime"),
        (fetcher_supervisor, "FetcherRuntime"),
        (mcp_host_supervisor, "MCPHostRuntime"),
    ],
)
def test_a_kernel_without_landlock_starts_the_child_anyway(
    module: Any, runtime_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard refusal here would make the release unusable on a kernel with no Landlock."""
    monkeypatch.setattr(confinement, "landlock_abi", lambda: None)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runtime = getattr(module, runtime_name)(
        socket_path=run_dir / "s.sock", workspace=tmp_path / "workspace"
    )

    kwargs = runtime._popen_platform_kwargs()

    assert "preexec_fn" not in kwargs


def test_a_refused_ruleset_stops_the_fetcher_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kernel that supports Landlock and then rejects the ruleset refuses the start.

    A silent failure is worse than no sandbox. So the start raises rather than serves.
    """
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 4)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise confinement.ConfinementError("the kernel rejected the ruleset")

    monkeypatch.setattr(confinement, "apply_plan", _refuse)

    with pytest.raises(fetcher_supervisor.FetcherStartError) as caught:
        fetcher_supervisor.start_fetcher(
            socket_path=tmp_path / "run" / "f.sock", workspace=tmp_path, timeout_s=5.0
        )

    assert "confin" in str(caught.value).lower()


# ------------------------------------------------------- the agent keeps its wrapper


def test_the_bwrap_wrapper_still_applies_to_the_local_shell(tmp_path: Path) -> None:
    """#20 leaves the agent's own wrapper in place. This test states that it is still there."""
    from nanoinfra.agent.tools.sandbox import wrap_command
    from nanoinfra.agent.tools.shell import ExecTool

    wrapped = wrap_command("bwrap", "echo hi", str(tmp_path), str(tmp_path))

    assert wrapped.startswith("bwrap ")
    assert "--die-with-parent" in wrapped
    assert ExecTool.capability_class == "mutate.local"


# ------------------------------------------------------------------- the launcher


def test_the_launcher_refuses_the_start_when_the_kernel_rejects_the_ruleset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 78 tells the container entry point to stop the retries.

    The launcher never reaches the exec on this path, so no helper serves unconfined.
    """
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 4)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise confinement.ConfinementError("the kernel rejected the ruleset")

    monkeypatch.setattr(confinement, "apply_plan", _refuse)

    status = confinement.main(
        ["--role", "fetcher", "--socket", str(tmp_path / "f.sock"), "--workspace", str(tmp_path)]
    )

    assert status == 78


def test_the_launcher_accepts_no_command_and_no_argv() -> None:
    """The launcher reads a role. A caller that could name a program would hold an exec right."""
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-m", "nanoinfra.gates.confinement", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0
    for option in ("--role", "--socket", "--workspace"):
        assert option in completed.stdout
    for forbidden in ("--command", "--argv", "--exec", "--program", "--shell"):
        assert forbidden not in completed.stdout


def test_the_launcher_names_one_fixed_entry_point_per_role() -> None:
    """Three roles, three module names, and no other program."""
    assert confinement.ROLE_MODULES == {
        "executor": "nanoinfra.gates.executor",
        "fetcher": "nanoinfra.gates.fetcher",
        "mcp-host": "nanoinfra.gates.mcp_host",
    }
