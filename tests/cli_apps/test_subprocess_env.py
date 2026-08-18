"""CLI app subprocesses must not inherit API keys from the parent environ.

Two call sites carry the risk: ``_run_argv`` runs the installer (pip/npm/brew/uv)
and ``run`` runs the installed app itself with agent-supplied arguments. Both
must get the same minimal allowlist the shell tool already applies in
``nanoinfra/agent/tools/shell.py:_build_env``. See nanoinfraorg/nanoinfra#133.
"""

from __future__ import annotations

import subprocess

from nanoinfra.apps.cli.service import CliAppManager

_LEAKY_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")


def _manager(tmp_path):
    return CliAppManager(workspace=tmp_path, data_dir=tmp_path / "cli-apps")


def test_subprocess_env_excludes_api_keys(monkeypatch, tmp_path) -> None:
    for key in _LEAKY_KEYS:
        monkeypatch.setenv(key, f"sk-{key.lower()}-leak")

    env = _manager(tmp_path)._subprocess_env()

    for key in _LEAKY_KEYS:
        assert key not in env
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "PATH" in env


def test_subprocess_env_excludes_api_keys_on_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("nanoinfra.apps.cli.service.sys.platform", "win32")
    for key in _LEAKY_KEYS:
        monkeypatch.setenv(key, f"sk-{key.lower()}-leak")

    env = _manager(tmp_path)._subprocess_env()

    for key in _LEAKY_KEYS:
        assert key not in env
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["SYSTEMROOT"]
    assert all(isinstance(value, str) for value in env.values())


def test_management_subprocesses_use_filtered_env(monkeypatch, tmp_path) -> None:
    """The installer path: ``_run_argv`` passed no env at all, so it inherited."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr("nanoinfra.apps.cli.service.subprocess.run", fake_run)

    _manager(tmp_path)._run_argv(["example-cli", "--help"], timeout=5)

    env = captured.get("env")
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_passes_filtered_env(monkeypatch, tmp_path) -> None:
    """The execution path: ``run`` passed an explicit ``os.environ.copy()``."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    manager = _manager(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr("nanoinfra.apps.cli.service.subprocess.run", fake_run)
    monkeypatch.setattr(manager, "get_app", lambda name: {"name": name, "entry_point": "echo"})
    monkeypatch.setattr(manager, "_load_installed", lambda: {"echo": {"entry_point": "echo"}})
    monkeypatch.setattr("nanoinfra.apps.cli.service.shutil.which", lambda entry: "/bin/echo")
    monkeypatch.setattr(manager, "_resolve_cwd", lambda *a, **k: tmp_path)
    monkeypatch.setattr(manager, "_artifact_snapshot", lambda cwd: {})
    monkeypatch.setattr(manager, "_changed_artifacts", lambda cwd, snap: [])

    manager.run("echo", ["hi"])

    env = captured.get("env")
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
