"""Sandbox backends for shell command execution.

To add a new backend, implement a function with the signature:
    _wrap_<name>(command: str, workspace: str, cwd: str) -> str
and register it in _BACKENDS below.
"""

import os
import shlex
import sys
from pathlib import Path
from typing import Iterable

from nanoinfra.config.paths import get_media_dir


def _normalize_bind_paths(
    paths: Iterable[str] | None,
    *,
    workspace: Path | None = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths or []:
        value = str(raw).strip()
        if not value:
            continue
        path = Path(os.path.expandvars(value)).expanduser()
        if not path.is_absolute():
            continue
        resolved_path = path.resolve(strict=False)
        if workspace is not None:
            try:
                workspace.relative_to(resolved_path)
            except ValueError:
                pass
            else:
                # A later bind of the workspace or one of its parents could
                # cover the tmpfs that hides the config directory.
                continue
        resolved = str(resolved_path)
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _interpreter_paths() -> list[str]:
    """The prefixes that have to be reachable for `python3` to be *this* Python (#276).

    A sandbox that binds `/usr` and not these reaches a different interpreter than the one the
    application runs, so a dependency nanoinfra declares and installs reads as missing:
    `import openpyxl` fails inside the sandbox while the package sits in the venv.

    **Both prefixes, and that is the part that is easy to get wrong.** A virtualenv's
    `bin/python` is a *symlink* into `sys.base_prefix`, so binding `sys.prefix` alone leaves a
    dangling link -- not an error, just a file that is not executable, which drops it from `PATH`
    resolution and falls through to the system interpreter with a plain
    `ModuleNotFoundError`. Measured: with only the venv bound, `command -v python3` inside the
    sandbox answers `/usr/bin/python3`.

    In the published image `sys.base_prefix` is `/usr/local`, already covered by the `/usr` bind,
    which is why the venv alone looks sufficient there and is not anywhere else -- a `uv`-managed
    interpreter lives under `~/.local/share/uv/python`, pyenv under `~/.pyenv`.

    Derived rather than written down, because which directory holds the interpreter is a property
    of how the deployment was installed and never the operator's to declare.
    """
    return [sys.prefix, sys.base_prefix]


def _bwrap(
    command: str,
    workspace: str,
    cwd: str,
    *,
    sandbox_ro_binds: Iterable[str] | None = None,
    sandbox_rw_binds: Iterable[str] | None = None,
) -> str:
    """Wrap command in a bubblewrap sandbox (requires bwrap in container).

    Only the workspace is bind-mounted read-write; its parent dir (which holds
    config.json) is hidden behind a fresh tmpfs.  The media directory is
    bind-mounted read-only so exec commands can read uploaded attachments.
    """
    ws = Path(workspace).resolve()
    media = get_media_dir().resolve()

    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    required = ["/usr"]
    optional = [
        "/bin",
        "/lib",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/pki/tls/certs",
        "/etc/pki/ca-trust",
        "/etc/crypto-policies",
        "/etc/resolv.conf",
        "/etc/ld.so.cache",
    ]
    # Normalized through the same helper the operator's own binds use, so an interpreter that
    # happens to sit above the workspace is dropped rather than covering the tmpfs that hides the
    # config directory. `--ro-bind-try` throughout: a path that is not there is not an error.
    for interpreter_path in _normalize_bind_paths(_interpreter_paths(), workspace=ws):
        if interpreter_path == "/usr" or interpreter_path.startswith("/usr/"):
            continue  # already covered by the required bind, and repeating it is noise
        optional.append(interpreter_path)

    args = ["bwrap", "--new-session", "--die-with-parent", "--setenv", "HOME", str(ws)]
    # Binding the interpreter is half a fix: `python3` is resolved through `PATH`, and a gateway
    # started as `uv run nanoinfra ...` has no venv `bin` on it -- so the sandbox reached the
    # bound files and ran the *system* interpreter anyway. Measured: `import openpyxl` failed
    # inside the sandbox with openpyxl installed and bound, and a console script living in that
    # same bin was `not found`. The launch incantation is not something the agent's shell should
    # depend on, so the interpreter's own bin goes first (#276).
    #
    # `pathPrepend` still wins: `_wrap_path_export` exports it inside this shell, after this.
    interpreter_bin = Path(sys.prefix) / "bin"
    if interpreter_bin.is_dir():
        inherited = os.environ.get("PATH", "")
        args += [
            "--setenv",
            "PATH",
            f"{interpreter_bin}{os.pathsep}{inherited}" if inherited else str(interpreter_bin),
        ]
    for p in required:
        args += ["--ro-bind", p, p]
    for p in optional:
        args += ["--ro-bind-try", p, p]
    args += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),        # mask config dir
        "--dir", str(ws),                 # recreate workspace mount point
        "--bind", str(ws), str(ws),
        "--ro-bind-try", str(media), str(media),  # read-only access to media
    ]
    for p in _normalize_bind_paths(sandbox_ro_binds, workspace=ws):
        args += ["--ro-bind-try", p, p]
    for p in _normalize_bind_paths(sandbox_rw_binds, workspace=ws):
        args += ["--bind-try", p, p]
    args += ["--chdir", sandbox_cwd, "--", "sh", "-c", command]
    return shlex.join(args)


_BACKENDS = {"bwrap": _bwrap}


def wrap_command(
    sandbox: str,
    command: str,
    workspace: str,
    cwd: str,
    *,
    sandbox_ro_binds: Iterable[str] | None = None,
    sandbox_rw_binds: Iterable[str] | None = None,
) -> str:
    """Wrap *command* using the named sandbox backend."""
    if backend := _BACKENDS.get(sandbox):
        return backend(
            command,
            workspace,
            cwd,
            sandbox_ro_binds=sandbox_ro_binds,
            sandbox_rw_binds=sandbox_rw_binds,
        )
    raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {list(_BACKENDS)}")
