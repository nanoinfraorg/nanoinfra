"""Name a nanoinfra process the way the operating system reports it.

A deployment with the privilege split runs four processes, and `ps -o comm`, `top`, `htop` and
`pgrep` all showed the same word for three of them: the interpreter. The full command line does
carry the role -- the helpers start as `python -m nanoinfra.gates.confinement --role executor` --
but `comm` is what the short listings print and what `pgrep` matches unless it is given `-f`.

`comm` is set with `prctl(PR_SET_NAME)`, which is one call and needs no C extension. The other
name, the one in `/proc/pid/cmdline`, cannot be changed from Python without overwriting the
process's own `argv` memory, which is what `setproctitle` exists to do. It is not what was
missing, so this module does not reach for it.

The kernel truncates to 15 bytes plus a terminator (`TASK_COMM_LEN` is 16). The names shipped here
are short for that reason and the accounts already carry the project: `ps -eo user,comm` prints
`nanoinfra-exec exec`, so repeating the prefix in `comm` would spend the budget on a word that is
already on the line.

Linux only. `prctl` is a Linux call, and on any other platform this is a no-op rather than an
error -- the same shape `confinement.py` takes, since a name is a convenience and refusing to
start over one would be absurd.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from typing import Final

from loguru import logger

# From include/uapi/linux/prctl.h. The value is stable ABI: it has been 15 since 2.6.9.
PR_SET_NAME: Final = 15

# TASK_COMM_LEN is 16 in include/linux/sched.h, and one byte of that is the terminator.
COMM_MAX_BYTES: Final = 15

# The name each process answers to. `nanoinfra` is the CLI itself, which a console script already
# names; it is set anyway, because `python -m nanoinfra` reports the interpreter instead.
CLI_NAME: Final = "nanoinfra"
GATEWAY_NAME: Final = "gateway"
EXECUTOR_NAME: Final = "exec"
FETCHER_NAME: Final = "fetch"
MCP_HOST_NAME: Final = "mcp"
CONNECTOR_HOST_NAME: Final = "connector"


def set_process_name(name: str) -> bool:
    """Set this process's `comm` to *name*. Returns whether the kernel took it.

    A name longer than the kernel's limit is refused rather than truncated: a `comm` cut mid-word
    names the wrong thing, and every name this project ships is checked against the limit by its
    own test. A caller that passes something longer has a bug, not a display problem.
    """
    if sys.platform != "linux":
        return False
    encoded = name.encode("ascii", errors="replace")
    if not encoded or len(encoded) > COMM_MAX_BYTES:
        logger.debug(
            "process name {!r} is {} bytes, and comm holds {}",
            name,
            len(encoded),
            COMM_MAX_BYTES,
        )
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        result = int(libc.prctl(PR_SET_NAME, encoded, 0, 0, 0))
    except (OSError, AttributeError) as exc:
        # A libc this cannot load or a prctl it does not export. The process runs unnamed.
        logger.debug("could not set the process name to {!r}: {}", name, exc)
        return False
    if result != 0:
        logger.debug("prctl(PR_SET_NAME, {!r}) returned {}", name, result)
        return False
    return True
