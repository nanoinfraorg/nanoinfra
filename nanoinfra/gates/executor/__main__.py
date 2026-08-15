"""The executor process entry point -- nanoinfraorg/nanoinfra#18.

``python -m nanoinfra.gates.executor --socket <path> --workspace <path>`` starts the executor.
The supervisor spawns this module and nothing else, so the entry point is the whole contract
between the two processes.

This is an internal surface, so argparse is enough. The user-facing CLI uses typer, and a typer
app here would put the executor on the command surface that the agent can reach.

**The child's log carries no frame locals.** loguru prints the local variables of each frame in
a traceback by default, and this process holds the values that must not reach a file: a resolved
command (#16 records a digest for that reason), a decrypted credential, and a transcript text
under scrub (#41). The supervisor sends this process's stderr to a file, so one unexpected
exception would otherwise write a plaintext credential into the run directory.

The configuration runs under the ``__main__`` guard rather than inside :func:`main`. A test calls
``main`` in process, and a handler swap there would take the test runner's own sink away.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger


def main(argv: list[str] | None = None) -> int:
    """Parse the two paths and serve until a signal stops the process."""
    parser = argparse.ArgumentParser(
        prog="python -m nanoinfra.gates.executor",
        description="Serve execution requests over a Unix domain socket.",
    )
    parser.add_argument("--socket", required=True, type=Path, help="Unix socket path to bind.")
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace root path.")
    args = parser.parse_args(argv)

    # The import stays local. A module level import would pull in the server, its store, and its
    # transports before argparse checks the arguments. A bad argument must fail first.
    from nanoinfra.gates.executor.server import serve_forever

    serve_forever(args.socket, workspace=args.workspace)
    return 0


def configure_child_logging() -> None:
    """Send this process's records to stderr, and print no frame locals.

    ``backtrace`` stays on, so a traceback still names the file and the line of each frame.
    ``diagnose`` goes off, because it prints the value of every local in those frames.
    """
    logger.remove()
    logger.add(sys.stderr, backtrace=True, diagnose=False)


if __name__ == "__main__":
    configure_child_logging()
    raise SystemExit(main())
