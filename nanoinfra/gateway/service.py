"""Install and manage OS-level gateway services.

Item 15 (nanoinfraorg/nanoinfra#18) splits the agent from the executor. A single-service
deployment then needs one entry point that starts both processes, and that entry point is the
supervisor. So one unit still installs, and the unit starts the supervisor rather than the
gateway when the caller passes a supervisor command.

The supervisor owns its own flags, so this module never builds that command. The caller
passes it whole through ``GatewayServiceOptions.supervisor_command``. Only the executor's
entry point is fixed, and it reads::

    python -m nanoinfra.gates.executor --socket <path> --workspace <path>

The #18 supervisor is a Python API rather than a program, so today the gateway command is
already the one entry point that brings up both processes. The seam below waits for a
supervisor that owns a command of its own, and it keeps this module out of the business of
guessing one.

One warning about the uid split. A ``systemd --user`` unit and a LaunchAgent both run as the
account that installs them, so both processes hold one uid. One process can then ptrace the
other and read its memory. On these two managers the split is organisational, not enforced.
A kernel-enforced split needs a system unit with ``User=`` per process, or the container
layout in ``entrypoint.sh``, where a root start places the two processes on two accounts.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nanoinfra.gateway import GatewayStartOptions, build_gateway_command

ServiceManagerKind = Literal["auto", "systemd", "launchd"]


@dataclass(frozen=True)
class GatewayServiceOptions:
    """Inputs used to render one system service.

    ``supervisor_command`` is the #18 hook. An empty value keeps the gateway command, so an
    existing install renders exactly as before. A caller that supplies the supervisor command
    gets a unit that starts the supervisor, and the supervisor starts the agent and the
    executor. The command arrives whole because this module must not guess the supervisor's
    flags.
    """

    start: GatewayStartOptions
    name: str = "nanoinfra-gateway"
    manager: ServiceManagerKind = "auto"
    enable: bool = True
    start_now: bool = True
    python_executable: str = sys.executable
    supervisor_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class GatewayServiceResult:
    """Result from service install/uninstall operations."""

    ok: bool
    message: str
    manager: str
    path: Path | None
    commands: tuple[tuple[str, ...], ...] = ()
    content: str | None = None


class GatewayServiceInstaller:
    """Render and install systemd user services or macOS LaunchAgents."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        subprocess_run: Callable[..., Any] = subprocess.run,
        home: Path | None = None,
    ) -> None:
        self.platform_name = platform_name or _platform_name()
        self._subprocess_run = subprocess_run
        self.home = home or Path.home()

    def install(self, options: GatewayServiceOptions, *, dry_run: bool = False) -> GatewayServiceResult:
        manager = self._resolve_manager(options.manager)
        if manager == "systemd":
            return self._install_systemd(options, dry_run=dry_run)
        if manager == "launchd":
            return self._install_launchd(options, dry_run=dry_run)
        return GatewayServiceResult(False, f"unsupported_service_manager:{manager}", manager, None)

    def uninstall(
        self,
        *,
        name: str = "nanoinfra-gateway",
        manager: ServiceManagerKind = "auto",
        dry_run: bool = False,
    ) -> GatewayServiceResult:
        resolved = self._resolve_manager(manager)
        if resolved == "systemd":
            return self._uninstall_systemd(name=name, dry_run=dry_run)
        if resolved == "launchd":
            return self._uninstall_launchd(name=name, dry_run=dry_run)
        return GatewayServiceResult(False, f"unsupported_service_manager:{resolved}", resolved, None)

    def _install_systemd(
        self,
        options: GatewayServiceOptions,
        *,
        dry_run: bool,
    ) -> GatewayServiceResult:
        unit_name = _systemd_unit_name(options.name)
        path = self.home / ".config" / "systemd" / "user" / unit_name
        command = _entry_command(options)
        content = _systemd_unit_content(
            description=f"Nanoinfra Gateway ({options.name})",
            command=command,
            working_directory=_working_directory_text(options.start),
        )
        commands: list[tuple[str, ...]] = [("systemctl", "--user", "daemon-reload")]
        if options.enable:
            commands.append(("systemctl", "--user", "enable", unit_name))
        if options.start_now:
            commands.append(("systemctl", "--user", "restart", unit_name))
        if dry_run:
            return GatewayServiceResult(True, "service_install_dry_run", "systemd", path, tuple(commands), content)

        _working_directory(options.start).mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        for command_args in commands:
            self._subprocess_run(list(command_args), check=True)
        return GatewayServiceResult(True, "service_installed", "systemd", path, tuple(commands), content)

    def _uninstall_systemd(
        self,
        *,
        name: str,
        dry_run: bool,
    ) -> GatewayServiceResult:
        unit_name = _systemd_unit_name(name)
        path = self.home / ".config" / "systemd" / "user" / unit_name
        commands = (
            ("systemctl", "--user", "disable", "--now", unit_name),
            ("systemctl", "--user", "daemon-reload"),
        )
        if dry_run:
            return GatewayServiceResult(True, "service_uninstall_dry_run", "systemd", path, commands)

        self._run_best_effort(commands[0])
        path.unlink(missing_ok=True)
        self._subprocess_run(list(commands[1]), check=True)
        return GatewayServiceResult(True, "service_uninstalled", "systemd", path, commands)

    def _install_launchd(
        self,
        options: GatewayServiceOptions,
        *,
        dry_run: bool,
    ) -> GatewayServiceResult:
        label = _launchd_label(options.name)
        path = self.home / "Library" / "LaunchAgents" / f"{label}.plist"
        log_stem = _safe_service_name(options.name)
        stdout_path = self.home / ".nanoinfra" / "logs" / f"{log_stem}.launchd.log"
        stderr_path = self.home / ".nanoinfra" / "logs" / f"{log_stem}.launchd.err.log"
        payload = {
            "Label": label,
            "ProgramArguments": _entry_command(options),
            "WorkingDirectory": _working_directory_text(options.start),
            "RunAtLoad": bool(options.enable),
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(stdout_path),
            "StandardErrorPath": str(stderr_path),
        }
        content = plistlib.dumps(payload, sort_keys=False).decode("utf-8")
        domain = _launchd_domain()
        commands: list[tuple[str, ...]] = []
        if options.start_now:
            commands.append(("launchctl", "bootstrap", domain, str(path)))
        if options.enable:
            commands.append(("launchctl", "enable", f"{domain}/{label}"))
        if options.start_now:
            commands.append(("launchctl", "kickstart", "-k", f"{domain}/{label}"))
        if dry_run:
            return GatewayServiceResult(True, "service_install_dry_run", "launchd", path, tuple(commands), content)

        _working_directory(options.start).mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if options.start_now:
            self._run_best_effort(("launchctl", "bootout", domain, str(path)))
        for command_args in commands:
            self._subprocess_run(list(command_args), check=True)
        return GatewayServiceResult(True, "service_installed", "launchd", path, tuple(commands), content)

    def _uninstall_launchd(
        self,
        *,
        name: str,
        dry_run: bool,
    ) -> GatewayServiceResult:
        label = _launchd_label(name)
        path = self.home / "Library" / "LaunchAgents" / f"{label}.plist"
        domain = _launchd_domain()
        commands = (
            ("launchctl", "bootout", domain, str(path)),
            ("launchctl", "disable", f"{domain}/{label}"),
        )
        if dry_run:
            return GatewayServiceResult(True, "service_uninstall_dry_run", "launchd", path, commands)

        for command_args in commands:
            self._run_best_effort(command_args)
        path.unlink(missing_ok=True)
        return GatewayServiceResult(True, "service_uninstalled", "launchd", path, commands)

    def _resolve_manager(self, manager: ServiceManagerKind) -> str:
        if manager != "auto":
            return manager
        if self.platform_name == "Darwin":
            return "launchd"
        if self.platform_name == "Linux":
            return "systemd"
        return self.platform_name.lower()

    def _run_best_effort(self, command_args: tuple[str, ...]) -> None:
        self._subprocess_run(list(command_args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _entry_command(options: GatewayServiceOptions) -> list[str]:
    """Return the one command the unit starts.

    This is the #18 seam. The supervisor is the entry point for a single-service deployment,
    because one unit must bring up the agent and the executor together. Until a caller passes
    that command, the unit starts the gateway exactly as before.
    """
    if options.supervisor_command:
        return list(options.supervisor_command)
    return build_gateway_command(options.python_executable, options.start)


def _platform_name() -> str:
    if sys.platform == "darwin":
        return "Darwin"
    if sys.platform.startswith("linux"):
        return "Linux"
    if sys.platform.startswith("win"):
        return "Windows"
    return sys.platform


def _working_directory(options: GatewayStartOptions) -> Path:
    if options.workspace:
        return Path(options.workspace).expanduser()
    return Path.home()


def _working_directory_text(options: GatewayStartOptions) -> str:
    if options.workspace:
        return os.path.expanduser(options.workspace)
    return str(Path.home())


def _systemd_unit_name(name: str) -> str:
    stem = _safe_service_name(name)
    return stem if stem.endswith(".service") else f"{stem}.service"


def _launchd_label(name: str) -> str:
    if name.startswith("ai.nanoinfra."):
        return name
    suffix = _safe_service_name(name).removeprefix("nanoinfra-").replace("-", ".")
    return f"ai.nanoinfra.{suffix}"


def _safe_service_name(name: str) -> str:
    value = name.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = value.strip(".-")
    return value or "nanoinfra-gateway"


def _launchd_domain() -> str:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return "gui/current"
    return f"gui/{getuid()}"


def _systemd_unit_content(
    *,
    description: str,
    command: list[str],
    working_directory: str,
) -> str:
    quoted_command = " ".join(_systemd_quote(part) for part in command)
    return "\n".join(
        [
            "[Unit]",
            f"Description={description}",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={_systemd_quote(str(working_directory))}",
            f"ExecStart={quoted_command}",
            "Restart=always",
            "RestartSec=10",
            "Environment=PYTHONUNBUFFERED=1",
            "NoNewPrivileges=yes",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _systemd_quote(value: str) -> str:
    if value and not re.search(r"\s|['\"\\]", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
