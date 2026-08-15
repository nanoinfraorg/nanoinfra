"""The executor process entry point -- nanoinfraorg/nanoinfra#18.

``python -m nanoinfra.gates.executor --socket <path> --workspace <path>`` starts the executor.
The supervisor spawns this module and nothing else, so the entry point is the whole contract
between the two processes.

This is an internal surface, so argparse is enough. The user-facing CLI uses typer, and a typer
app here would put the executor on the command surface that the agent can reach.
"""

from __future__ import annotations

import argparse
from pathlib import Path


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


if __name__ == "__main__":
    raise SystemExit(main())
