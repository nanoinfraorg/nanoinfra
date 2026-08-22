"""One confinement layer per helper process -- nanoinfraorg/nanoinfra#20.

A sandbox complements the privilege split. A sandbox never replaces it. The split must hold after
a sandbox layer fails, so nothing here weakens a boundary that #18, #19, or #22 already keeps.

Three helper processes use this module. The executor (#18) decrypts credentials and reaches the
inventory hosts. The fetcher (#19) reads untrusted web content. The MCP host (#22) starts a program
that a config in the agent's reach names, so it holds the exec right and it needs this most.

WHY LANDLOCK. Landlock needs no root, no namespace, and no helper program. ``bwrap`` may be absent
on a plain ``pip install`` host, so a bwrap-only design fails there. The kernel syscalls need no
new third-party package. The agent keeps its own bwrap wrapper for local shell commands
(``nanoinfra/agent/tools/sandbox.py``), and #20 changes nothing about that.

WHAT EACH ROLE GETS.

- Fetcher: a TCP port allowlist, plus a filesystem policy that names no workspace path. So the
  process that reads a page cannot read the credential store and cannot read the inventory.
- Executor: a filesystem policy and a bounded exec surface. A port allowlist would equal the
  inventory, and an allowlist that equals the inventory restricts nothing.
- MCP host: a bounded exec surface, a bounded write surface, and no workspace path at all.
- Every role: no TCP listener, and no signal or abstract socket outside its own process tree.

LOUD FAILURE, AND WHICH FAILURE DOES WHAT. A silent failure is worse than no sandbox.

- The version probe fails, so this kernel offers no Landlock: DEGRADE with a warning. A kernel
  without Landlock support is a legitimate host. A refusal there makes the release unusable.
  Docker profiles that predate Landlock answer this probe with EPERM, and that answer lands here
  as well.
- The kernel supports Landlock and then rejects the ruleset: REFUSE the start. The child raises
  before the exec, and the supervisor turns that into a start error.
- The run dir is absent: REFUSE. The child cannot bind its socket without it.
- An optional path is absent or unreachable: SKIP that one rule. A skipped grant makes the policy
  tighter, never wider, so it cannot weaken the sandbox in silence.

FOUR THINGS THE KERNEL DOES THAT COST REAL TIME. Each one comes from a measurement on a live
kernel, and no manual page states all four.

1. An exec needs EXECUTE on the ELF loader as well as on the program. The kernel opens the loader
   with exec intent, so a rule on the interpreter alone denies the exec with EACCES. A grant on
   the loader also lets the loader run any file it can read, so the exec surface bounds intent
   rather than an escape. The write surface and the split carry that weight.
2. A rule on a file refuses directory-only rights with EINVAL. So a file rule keeps EXECUTE,
   WRITE_FILE, READ_FILE, TRUNCATE, and IOCTL_DEV, and it drops the rest.
3. Rules accumulate along the path to the root, and a deeper rule adds rights. No rule subtracts.
   So a read grant on ``/`` would make every later restriction void, and this module grants no
   such root.
4. Landlock governs a Unix socket bind through MAKE_SOCK on the directory. It governs no connect
   at all. So the sockets the privilege split needs stay reachable, and a confined child still
   answers the agent.

A fifth, smaller one: name resolution reads more of ``/etc`` than an author expects. The curated
list below covers ``nsswitch.conf``, ``gai.conf``, ``services``, and ``protocols``, and DNS fails
without them.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import stat
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

EXECUTOR_ROLE = "executor"
FETCHER_ROLE = "fetcher"
MCP_HOST_ROLE = "mcp-host"

LAYER_LANDLOCK = "landlock"
LAYER_NONE = "none"

# The fixed entry point for each role. The launcher below reads a role and never an argv, so a
# caller of this module still cannot choose a program.
ROLE_MODULES = {
    EXECUTOR_ROLE: "nanoinfra.gates.executor",
    FETCHER_ROLE: "nanoinfra.gates.fetcher",
    MCP_HOST_ROLE: "nanoinfra.gates.mcp_host",
}

# The filesystem access rights, from include/uapi/linux/landlock.h.
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14
FS_IOCTL_DEV = 1 << 15

NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1

# A file rule accepts these rights only. Every other right belongs to a directory, and the kernel
# answers EINVAL for a file rule that names one.
_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

# The rights each grant carries.
_READ = FS_READ_FILE | FS_READ_DIR
_EXEC = FS_EXECUTE | FS_READ_FILE
_WRITE = (
    _READ
    | FS_WRITE_FILE
    | FS_REMOVE_DIR
    | FS_REMOVE_FILE
    | FS_MAKE_DIR
    | FS_MAKE_REG
    | FS_MAKE_SOCK
    | FS_MAKE_FIFO
    | FS_MAKE_SYM
    | FS_REFER
    | FS_TRUNCATE
)
# A device node needs a write right and an ioctl right. It needs no MAKE right, because an
# unprivileged process creates no device node anyway.
_DEVICE = _READ | FS_WRITE_FILE | FS_TRUNCATE | FS_IOCTL_DEV
# The run dir takes the rights a bind needs and nothing more. MAKE_SOCK creates the socket node.
# REMOVE_FILE clears a stale socket before the bind. WRITE_FILE lets a peer connect to a socket
# this child created. MAKE_DIR creates the executor's operator socket dir on a first start.
#
# No MAKE_REG right means a confined helper drops no file in there. That matters because a pip
# install puts all three sockets in one dir, so a file one helper wrote would sit where another
# helper could read it.
_SOCKET_DIR = _READ | FS_WRITE_FILE | FS_MAKE_SOCK | FS_REMOVE_FILE | FS_MAKE_DIR

# The rights each ABI version knows. A ruleset that names a later right fails with EINVAL, so the
# handled set stops at the version the kernel reports.
_HANDLED_FS_BY_ABI = {
    1: (1 << 13) - 1,
    2: (1 << 14) - 1,
    3: (1 << 15) - 1,
    4: (1 << 15) - 1,
}
_HANDLED_FS_LATEST = (1 << 16) - 1
_ABI_FOR_NET = 4
_ABI_FOR_SCOPE = 6

# Landlock landed with one number per syscall on every architecture that took the whole set at
# once. An architecture outside this table degrades rather than calls a number that means
# something else there.
_SYSCALLS_BY_MACHINE = {
    "aarch64": (444, 445, 446),
    "armv7l": (444, 445, 446),
    "armv8l": (444, 445, 446),
    "i386": (444, 445, 446),
    "i686": (444, 445, 446),
    "loongarch64": (444, 445, 446),
    "ppc64le": (444, 445, 446),
    "riscv64": (444, 445, 446),
    "s390x": (444, 445, 446),
    "x86_64": (444, 445, 446),
}

_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_RULE_NET_PORT = 2
_PR_SET_NO_NEW_PRIVS = 38

# The system paths every role reads. A missing entry skips, because images differ.
_SYSTEM_READ_PATHS = ("/usr", "/lib", "/lib32", "/lib64", "/bin", "/sbin", "/opt", "/proc", "/sys")

# The /etc entries a Python process needs. The list is curated rather than the whole directory, so
# a file such as /etc/shadow stays out of reach even where the account could read it.
_ETC_READ_ENTRIES = (
    "alternatives",
    "ca-certificates",
    "ca-certificates.conf",
    "crypto-policies",
    "gai.conf",
    "group",
    "host.conf",
    "hosts",
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "localtime",
    "networks",
    "nsswitch.conf",
    "passwd",
    "pki",
    "protocols",
    "resolv.conf",
    "services",
    "ssl",
    "timezone",
)

# The executor runs ansible and ssh, and both read their own config trees.
_EXECUTOR_ETC_READ_ENTRIES = ("ansible", "ssh")

# The bin dirs a system program lives in. The executor and the MCP host both start one.
_SYSTEM_BIN_PATHS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin", "/usr/local/sbin")

# Where a package manager keeps the program a stdio MCP server runs. npx, uvx, and bunx all write
# a cache and then run a binary out of it.
_TOOLCHAIN_HOME_DIRS = (
    ".asdf",
    ".bun",
    ".cache",
    ".config",
    ".deno",
    ".local",
    ".npm",
    ".nvm",
    ".pyenv",
    ".yarn",
)

# The web ports the fetcher reaches. Port 53 covers the TCP fallback of a DNS resolver, because a
# truncated answer over UDP arrives again over TCP.
_FETCHER_PORTS = (53, 80, 443)
_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)
_PROXY_SCHEME_PORTS = {"http": 80, "https": 443, "socks4": 1080, "socks5": 1080, "socks5h": 1080}


class ConfinementError(RuntimeError):
    """The kernel supports Landlock and this ruleset did not apply."""


@dataclass(frozen=True)
class PathRule:
    """One filesystem grant.

    ``required`` marks a path the child cannot serve without. An absent required path refuses the
    start. An absent optional path drops out of the plan.
    """

    path: Path
    access: int
    required: bool = False


@dataclass(frozen=True)
class ConfinementPlan:
    """The rules one child starts under.

    The plan is a value, so a test reads it without a kernel. ``restrict_connect`` and
    ``restrict_bind`` name the network rights the ruleset handles. A handled right with no rule
    denies every use of it, which is how a role gets "no TCP listener".
    """

    role: str
    rules: tuple[PathRule, ...] = ()
    connect_ports: tuple[int, ...] = ()
    bind_ports: tuple[int, ...] = ()
    restrict_connect: bool = False
    restrict_bind: bool = True
    scope_ipc: bool = True


@dataclass(frozen=True)
class ChildConfinement:
    """The decision for one child, and the callable that applies it.

    The supervisor builds this before the spawn, so the log names the layer before the child
    starts. The child applies the rules after the fork and before the exec.
    """

    plan: ConfinementPlan
    layer: str
    abi: int | None = None
    reason: str | None = None

    def controls(self) -> tuple[str, ...]:
        """The controls this layer really applies, in the words an operator reads."""
        if self.layer == LAYER_NONE:
            return ()
        abi = self.abi or 0
        found = [f"{len(self.plan.rules)} filesystem rules"]
        if abi >= _ABI_FOR_NET:
            if self.plan.restrict_connect:
                ports = ", ".join(str(port) for port in sorted(self.plan.connect_ports))
                found.append(f"tcp connect limited to {ports}")
            found.append("no tcp listener")
        else:
            found.append(f"no tcp rules, because they need abi {_ABI_FOR_NET}")
        if abi >= _ABI_FOR_SCOPE and self.plan.scope_ipc:
            found.append("no signal or abstract socket outside the process tree")
        return tuple(found)

    def summary(self) -> str:
        """One line for the log, and the same line for the startup echo."""
        if self.layer == LAYER_NONE:
            return (
                f"{self.plan.role} confinement: NOT APPLIED, {self.reason}. The privilege split "
                "still holds, and it is the boundary that carries the weight."
            )
        return f"{self.plan.role} confinement: landlock abi {self.abi}, " + ", ".join(
            self.controls()
        )

    def preexec(self) -> Callable[[], None] | None:
        """The callable a spawn runs in the child, or None when there is nothing to apply.

        The work after the fork stays small on purpose. The parent already resolved every path and
        every port, so the child opens paths and calls three syscalls.
        """
        if self.layer == LAYER_NONE:
            return None
        plan = self.plan
        abi = self.abi

        def _apply() -> None:
            try:
                apply_plan(plan, abi=abi)
            except ConfinementError as exc:
                # CPython replaces the exception of a preexec callable with one fixed sentence, so
                # the parent never reads this text. File descriptor 2 is the log of this child, and
                # the supervisor quotes that log in the start error.
                os.write(2, f"[confinement] error: {exc}\n".encode())
                raise

        return _apply


def landlock_abi() -> int | None:
    """The Landlock ABI version this kernel reports, or None when it reports none.

    Every failure answers None. A kernel without the syscall, a kernel with the LSM turned off,
    and a container runtime that blocks the syscall all reach this path, and all three are
    legitimate hosts.
    """
    libc = _libc()
    if libc is None:
        return None
    numbers = _syscall_numbers()
    if numbers is None:
        return None
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(numbers[0]),
        ctypes.c_void_p(0),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_CREATE_RULESET_VERSION),
    )
    if result < 1:
        logger.debug("gates: no landlock support, errno %d", ctypes.get_errno())
        return None
    return int(result)


def plan_child(
    role: str,
    *,
    run_dir: Path | str,
    workspace: Path | str | None = None,
    config_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> ChildConfinement:
    """Build the confinement one child starts under, and state what the host supports.

    The caller passes the paths rather than the config, so a test controls them. ``None`` reads the
    live location, and the fetcher and the MCP host need no workspace at all.
    """
    if role not in ROLE_MODULES:
        raise ConfinementError(f"unknown confinement role {role!r}")
    plan = _build_plan(
        role,
        run_dir=Path(run_dir),
        workspace=Path(workspace) if workspace is not None else None,
        config_path=Path(config_path) if config_path is not None else _live_config_path(),
        data_dir=Path(data_dir) if data_dir is not None else _live_data_dir(),
    )
    abi = landlock_abi()
    if abi is None:
        reason = "this kernel reports no landlock support"
        numbers = _syscall_numbers()
        if numbers is None:
            reason = f"landlock has no known syscall number on {platform.machine()}"
        return ChildConfinement(plan=plan, layer=LAYER_NONE, reason=reason)
    return ChildConfinement(plan=_drop_absent_rules(plan), layer=LAYER_LANDLOCK, abi=abi)


def apply_plan(plan: ConfinementPlan, *, abi: int | None) -> None:
    """Apply *plan* to this process, or raise.

    The call restricts the caller and every child of the caller. It survives an exec, which is how
    a supervisor confines an entry point that it does not import.
    """
    libc = _libc()
    numbers = _syscall_numbers()
    if libc is None or numbers is None or abi is None:
        raise ConfinementError("this host cannot apply a landlock ruleset")
    create, add_rule, restrict_self = numbers
    handled_fs = _HANDLED_FS_BY_ABI.get(abi, _HANDLED_FS_LATEST)
    handled_net = 0
    if abi >= _ABI_FOR_NET:
        if plan.restrict_bind:
            handled_net |= NET_BIND_TCP
        if plan.restrict_connect:
            handled_net |= NET_CONNECT_TCP
    scoped = 0
    if abi >= _ABI_FOR_SCOPE and plan.scope_ipc:
        scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL

    attr = _RulesetAttr(handled_fs, handled_net, scoped)
    size = 24 if abi >= _ABI_FOR_SCOPE else (16 if abi >= _ABI_FOR_NET else 8)
    ctypes.set_errno(0)
    ruleset = libc.syscall(
        ctypes.c_long(create),
        ctypes.byref(attr),
        ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise ConfinementError(
            f"the kernel refused a landlock ruleset for {plan.role}, errno {ctypes.get_errno()}"
        )
    try:
        for rule in plan.rules:
            _add_path_rule(libc, add_rule, ruleset, rule, handled_fs=handled_fs)
        if handled_net & NET_CONNECT_TCP:
            for port in plan.connect_ports:
                _add_port_rule(libc, add_rule, ruleset, port, NET_CONNECT_TCP, plan.role)
        if handled_net & NET_BIND_TCP:
            for port in plan.bind_ports:
                _add_port_rule(libc, add_rule, ruleset, port, NET_BIND_TCP, plan.role)
        ctypes.set_errno(0)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise ConfinementError(
                f"prctl no_new_privs failed for {plan.role}, errno {ctypes.get_errno()}"
            )
        ctypes.set_errno(0)
        restricted = libc.syscall(
            ctypes.c_long(restrict_self), ctypes.c_int(ruleset), ctypes.c_uint32(0)
        )
        if restricted != 0:
            raise ConfinementError(
                f"landlock_restrict_self failed for {plan.role}, errno {ctypes.get_errno()}"
            )
    finally:
        os.close(ruleset)


def support_summary() -> str:
    """The confinement clause the startup echo carries.

    An operator reads one line at start. That line must name the sandbox as a control, and it must
    name the absence of the sandbox just as plainly.
    """
    abi = landlock_abi()
    if abi is None:
        return (
            "confinement: NOT APPLIED, this kernel reports no landlock support. The three helper "
            "processes keep their own accounts and their own sockets"
        )
    controls = "filesystem rules and no tcp listener"
    if abi >= _ABI_FOR_NET:
        controls = "filesystem rules, a tcp port allowlist for the fetcher, and no tcp listener"
    return f"confinement: landlock abi {abi} on each helper process, {controls}"


def main(argv: Sequence[str] | None = None) -> int:
    """Confine this process for one role, then exec that role's fixed entry point.

    The container entry point starts each helper with ``setpriv``, so no Python supervisor runs
    there. This launcher gives that path the same rules the supervisors apply. It reads a role and
    two paths. It reads no command, so it hands no exec right to a caller.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m nanoinfra.gates.confinement",
        description="Apply the confinement of one helper role, then start that helper.",
    )
    parser.add_argument("--role", required=True, choices=sorted(ROLE_MODULES))
    parser.add_argument("--socket", required=True, type=Path, help="Unix socket path to bind.")
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace root path.")
    args = parser.parse_args(argv)

    role: str = args.role
    socket_path: Path = args.socket
    workspace: Path = args.workspace
    decision = plan_child(role, run_dir=socket_path.parent, workspace=workspace)
    if decision.layer == LAYER_NONE:
        sys.stderr.write(f"[confinement] warning: {decision.summary()}\n")
    else:
        try:
            apply_plan(decision.plan, abi=decision.abi)
        except ConfinementError as exc:
            # A refusal here refuses the start. The supervisor in entrypoint.sh reports the exit
            # status, and no helper serves without the layer this kernel accepted a moment ago.
            sys.stderr.write(f"[confinement] error: {exc}\n")
            sys.stderr.write("[confinement] error: this helper refuses to start unconfined\n")
            return 78
        sys.stderr.write(f"[confinement] {decision.summary()}\n")
    sys.stderr.flush()
    command = [
        sys.executable,
        "-m",
        ROLE_MODULES[role],
        "--socket",
        str(socket_path),
        "--workspace",
        str(workspace),
    ]
    os.execv(sys.executable, command)


# ------------------------------------------------------------------ the plan per role


def _build_plan(
    role: str,
    *,
    run_dir: Path,
    workspace: Path | None,
    config_path: Path | None,
    data_dir: Path | None,
) -> ConfinementPlan:
    rules = [PathRule(run_dir, _SOCKET_DIR, required=True)]
    rules += [PathRule(path, _WRITE) for path in _temp_dirs()]
    rules += [PathRule(Path(path), _READ) for path in _SYSTEM_READ_PATHS]
    rules += [PathRule(path, _READ) for path in _python_read_paths()]
    rules += [PathRule(Path("/etc") / name, _READ) for name in _ETC_READ_ENTRIES]
    rules.append(PathRule(Path("/dev"), _DEVICE))
    rules += [PathRule(path, _EXEC) for path in _exec_start_paths()]
    if config_path is not None:
        rules.append(PathRule(config_path, FS_READ_FILE))

    if role == FETCHER_ROLE:
        return ConfinementPlan(
            role=role,
            rules=_merge_rules(rules),
            connect_ports=_fetcher_ports(),
            restrict_connect=True,
        )
    if role == EXECUTOR_ROLE:
        rules += _executor_rules(workspace=workspace, data_dir=data_dir)
        return ConfinementPlan(role=role, rules=_merge_rules(rules))
    rules += _mcp_host_rules()
    return ConfinementPlan(role=role, rules=_merge_rules(rules))


def _executor_rules(*, workspace: Path | None, data_dir: Path | None) -> list[PathRule]:
    """The paths only the executor reaches.

    The workspace holds the credential store, the server inventory, and the job store. The data
    dir holds the gate audit log. The executor writes in both, so both take a write grant.

    The exec surface holds the system bin dirs, because the ansible backend runs ansible and ssh.
    It holds no workspace path and no temp path, so a program the agent wrote never runs here.

    The working dir takes a write grant as well. ``ansible-runner`` writes its artifacts in the
    ``private_data_dir``, and that field falls back to the working dir. One limit stays, and it is
    stated rather than hidden: a ``projectPath`` outside these roots gets no write grant, so an
    ansible run against it fails. A read grant cannot fix that, and no config key exists for it.
    """
    rules: list[PathRule] = []
    rules.append(PathRule(Path.cwd(), _WRITE))
    if workspace is not None:
        rules.append(PathRule(workspace, _WRITE))
    if data_dir is not None:
        rules.append(PathRule(data_dir, _WRITE))
    rules += [PathRule(Path("/etc") / name, _READ) for name in _EXECUTOR_ETC_READ_ENTRIES]
    rules += [PathRule(Path(path), _EXEC) for path in _SYSTEM_BIN_PATHS]
    rules.append(PathRule(Path(sys.executable).resolve().parent, _EXEC))
    return rules


def _mcp_host_rules() -> list[PathRule]:
    """The paths only the MCP host reaches.

    A stdio server arrives through npx, uvx, or bunx, and each one writes a cache in the home dir
    and then runs a program out of it. So those dirs take a write grant and an exec grant.

    No rule names the workspace. The host's own entry point uses the workspace for one log line,
    and the workspace holds the credential store. A stdio server that must read the workspace
    therefore needs a directory outside it. That cost buys the property that matters here: the
    process that starts a program cannot read the credentials the executor decrypts.
    """
    rules: list[PathRule] = []
    try:
        home = Path.home()
    except RuntimeError:
        # A host with no home dir for this account runs a stdio server out of a system bin dir
        # only. The exec surface below still covers that case.
        logger.warning("gates: no home dir, so the MCP host confinement holds no cache dir")
        home = None
    if home is not None:
        for name in _TOOLCHAIN_HOME_DIRS:
            rules.append(PathRule(home / name, _WRITE | FS_EXECUTE))
    rules += [PathRule(Path(path), _EXEC) for path in _SYSTEM_BIN_PATHS]
    rules.append(PathRule(Path(sys.executable).resolve().parent, _EXEC))
    return rules


def _python_read_paths() -> list[Path]:
    """Every path the interpreter reads to start.

    ``sys.path`` covers the stdlib and the site packages. The cwd matters because ``python -m``
    puts it first on the path. A venv adds a prefix that lives outside the base prefix.
    """
    found = [
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.exec_prefix),
        Path(sys.base_exec_prefix),
        Path.cwd(),
    ]
    found += [Path(entry) for entry in sys.path if entry]
    return _unique(found)


def _exec_start_paths() -> list[Path]:
    """The programs every role must exec to reach its own entry point.

    The interpreter is one file rather than its whole directory. The ELF loader joins it, because
    the kernel opens the loader with exec intent and denies the exec without a grant.
    """
    found = [Path(os.path.realpath(sys.executable))]
    found += _loader_paths()
    return _unique(found)


def _loader_paths() -> list[Path]:
    """Every dynamic loader on this image.

    A glob covers glibc, musl, and a second architecture in one image. A child of the MCP host may
    carry a different loader than the interpreter does.
    """
    found: list[Path] = []
    for parent in ("/lib", "/lib32", "/lib64", "/usr/lib", "/usr/lib32", "/usr/lib64"):
        root = Path(parent)
        if not root.is_dir():
            continue
        found += sorted(root.glob("ld-*.so*"))
        for child in sorted(root.glob("*/ld-*.so*")):
            found.append(child)
    return _unique(found)


def _temp_dirs() -> list[Path]:
    """The temp dirs a Python process writes.

    A write grant carries a read grant with it, so every temp file of every process under this
    account becomes readable here. That is the stated cost of a working ``tempfile``. The
    alternative breaks the executor, because the ansible backend needs a private data dir.
    """
    found = [Path("/tmp")]
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name, "").strip()
        if value:
            found.append(Path(value))
    return _unique(found)


def _fetcher_ports() -> tuple[int, ...]:
    """The TCP ports the fetcher may reach.

    A deployment behind a proxy reaches that proxy instead of the site, so the proxy port joins
    the list. Two sources name a proxy: the standard environment variables, and ``tools.web.proxy``
    in the config. Both go in, because a fetcher that cannot reach its proxy fetches nothing.

    The configured search backend joins it for the same reason, and it was missing. A self-hosted
    provider answers where its operator put it -- a SearXNG on ``http://searxng:8080/`` is the case
    that found this -- and 8080 is in no default list, so search failed with "All connection
    attempts failed" while the socket, the account and the policy all looked correct. A fetcher
    that cannot reach its search backend searches nothing.
    """
    ports = set(_FETCHER_PORTS)
    for name in _PROXY_ENV_VARS:
        ports |= _proxy_port(os.environ.get(name))
    ports |= _proxy_port(_configured_proxy())
    ports |= _proxy_port(_configured_search_base_url())
    return tuple(sorted(ports))


def _proxy_port(value: str | None) -> set[int]:
    if not value:
        return set()
    try:
        parts = urlsplit(value if "://" in value else f"http://{value}")
        if parts.port:
            return {parts.port}
        return {_PROXY_SCHEME_PORTS.get(parts.scheme, 80)}
    except ValueError:
        logger.debug("gates: a proxy value is not a URL, so no port joins the fetcher policy")
        return set()


def _configured_proxy() -> str | None:
    """The proxy the config names, or None.

    The read stays optional. A broken config is not this module's failure to report, and the
    fetcher itself reports it.
    """
    try:
        from nanoinfra.config.loader import load_config

        return load_config().tools.web.proxy or None
    except Exception:  # noqa: BLE001 - any config fault leaves the port list as it is
        logger.debug("gates: no config proxy joins the fetcher policy")
        return None


def _configured_search_base_url() -> str | None:
    """The base URL of the configured search provider, or None.

    Optional in the same way ``_configured_proxy`` is: a config this module cannot read leaves the
    port list as it is, and the fetcher reports the real fault itself. Only the port is taken from
    it -- the host stays a decision for the fetcher's own SSRF guard, and a port allowlist that
    named a host would be a second, disagreeing opinion about the destination.
    """
    try:
        from nanoinfra.config.loader import load_config

        return load_config().tools.web.search.base_url or None
    except Exception:  # noqa: BLE001 - any config fault leaves the port list as it is
        logger.debug("gates: no configured search backend joins the fetcher policy")
        return None


def _live_config_path() -> Path | None:
    try:
        from nanoinfra.config.loader import get_config_path

        return get_config_path()
    except Exception:  # noqa: BLE001 - the plan works without it
        return None


def _live_data_dir() -> Path | None:
    """The dir that holds the gate audit log.

    The lookup reads the config path parent rather than ``get_data_dir``. That helper creates the
    directory, and a plan must create nothing.
    """
    config_path = _live_config_path()
    return config_path.parent if config_path is not None else None


def _drop_absent_rules(plan: ConfinementPlan) -> ConfinementPlan:
    """Remove every optional rule whose path is absent.

    Images differ, so an absent ``/lib64`` or ``/etc/ansible`` is normal. A dropped grant tightens
    the policy, so this can never widen the sandbox. A required path stays in the plan, and the
    child then refuses the start.
    """
    kept = tuple(rule for rule in plan.rules if rule.required or rule.path.exists())
    dropped = [str(rule.path) for rule in plan.rules if rule not in kept]
    if dropped:
        logger.debug("gates: %s confinement drops absent paths %s", plan.role, ", ".join(dropped))
    return replace(plan, rules=kept)


def _merge_rules(rules: Iterable[PathRule]) -> tuple[PathRule, ...]:
    """One rule per path, with the rights of every rule that named it.

    The kernel accumulates the rights of the rules along a path, so two rules on one path grant
    the union anyway. One merged rule keeps the plan readable and the rule count honest.
    """
    merged: dict[Path, PathRule] = {}
    for rule in rules:
        found = merged.get(rule.path)
        if found is None:
            merged[rule.path] = rule
            continue
        merged[rule.path] = PathRule(
            rule.path, found.access | rule.access, found.required or rule.required
        )
    return tuple(merged.values())


def _unique(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        found.append(path)
    return found


# ------------------------------------------------------------------ the kernel calls


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    # The kernel struct is packed, and an unpacked copy sends the fd at the wrong offset.
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


class _NetPortAttr(ctypes.Structure):
    _fields_ = (("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64))


_libc_handle: ctypes.CDLL | None = None
_libc_looked_up = False


def _libc() -> ctypes.CDLL | None:
    """The C library of this process, or None where Landlock cannot exist.

    The lookup happens once and stays cached. A load at import time would raise on Windows, and
    the supervisors import this module there as well.
    """
    global _libc_handle, _libc_looked_up
    if _libc_looked_up:
        return _libc_handle
    _libc_looked_up = True
    if sys.platform != "linux":
        return None
    try:
        _libc_handle = ctypes.CDLL(None, use_errno=True)
    except OSError:
        logger.debug("gates: no C library handle, so no landlock probe runs")
        return None
    _libc_handle.syscall.restype = ctypes.c_long
    _libc_handle.prctl.restype = ctypes.c_int
    return _libc_handle


def _syscall_numbers() -> tuple[int, int, int] | None:
    return _SYSCALLS_BY_MACHINE.get(platform.machine())


def _add_path_rule(
    libc: ctypes.CDLL, add_rule: int, ruleset: int, rule: PathRule, *, handled_fs: int
) -> None:
    """Add one filesystem grant, or decide what an absent path means.

    An O_PATH open needs no read right on the target, so a rule on a mode 600 file still works
    from an account that only traverses to it.
    """
    try:
        fd = os.open(rule.path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        if rule.required:
            raise ConfinementError(
                f"the confinement of {rule.path} failed and it is required: {exc}"
            ) from exc
        return
    try:
        access = rule.access & handled_fs
        if not _is_dir(fd):
            # A file rule with a directory-only right answers EINVAL, so the right drops here.
            access &= _FILE_RIGHTS
        if not access:
            return
        attr = _PathBeneathAttr(access, fd)
        ctypes.set_errno(0)
        result = libc.syscall(
            ctypes.c_long(add_rule),
            ctypes.c_int(ruleset),
            ctypes.c_int(_RULE_PATH_BENEATH),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        )
        if result != 0:
            raise ConfinementError(
                f"the kernel refused a rule on {rule.path}, errno {ctypes.get_errno()}"
            )
    finally:
        os.close(fd)


def _add_port_rule(
    libc: ctypes.CDLL, add_rule: int, ruleset: int, port: int, access: int, role: str
) -> None:
    attr = _NetPortAttr(access, port)
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(add_rule),
        ctypes.c_int(ruleset),
        ctypes.c_int(_RULE_NET_PORT),
        ctypes.byref(attr),
        ctypes.c_uint32(0),
    )
    if result != 0:
        raise ConfinementError(
            f"the kernel refused port {port} for {role}, errno {ctypes.get_errno()}"
        )


def _is_dir(fd: int) -> bool:
    """Report whether the open descriptor holds a directory.

    The check runs in the child after a fork, so it imports nothing. An import there can wait on a
    lock that a thread of the parent held at the moment of the fork.
    """
    return stat.S_ISDIR(os.fstat(fd).st_mode)


__all__ = [
    "EXECUTOR_ROLE",
    "FETCHER_ROLE",
    "LAYER_LANDLOCK",
    "LAYER_NONE",
    "MCP_HOST_ROLE",
    "ROLE_MODULES",
    "ChildConfinement",
    "ConfinementError",
    "ConfinementPlan",
    "PathRule",
    "apply_plan",
    "landlock_abi",
    "main",
    "plan_child",
    "support_summary",
]


if __name__ == "__main__":
    raise SystemExit(main())
