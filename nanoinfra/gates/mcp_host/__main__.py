"""The MCP host process entry point -- nanoinfraorg/nanoinfra#22.

``python -m nanoinfra.gates.mcp_host --socket <path> --workspace <path>`` starts the host. The
supervisor spawns this module and nothing else, so the entry point is the whole contract between
the two processes. It reads the same two flags as the executor (#18) and the fetcher (#19), because
one shape means one supervisor pattern.

This is an internal surface, so argparse is enough. The user-facing CLI uses typer, and a typer app
here would put the host on the command surface that the agent can reach.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nanoinfra.utils.process_name import MCP_HOST_NAME, set_process_name


def main(argv: list[str] | None = None) -> int:
    """Parse the two paths and serve until a signal stops the process."""
    parser = argparse.ArgumentParser(
        prog="python -m nanoinfra.gates.mcp_host",
        description="Run stdio MCP servers and serve their sessions over a Unix domain socket.",
    )
    parser.add_argument("--socket", required=True, type=Path, help="Unix socket path to bind.")
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace root path.")
    args = parser.parse_args(argv)

    # The import stays local. A module level import would pull in the MCP SDK before argparse
    # checks the arguments. A bad argument must fail first.
    from nanoinfra.gates.mcp_host.server import serve_forever

    serve_forever(args.socket, workspace=args.workspace)
    return 0


if __name__ == "__main__":
    # `confinement.main` execs this module, and an exec resets `comm` to the
    # interpreter. So the name belongs here, after the last exec.
    set_process_name(MCP_HOST_NAME)
    raise SystemExit(main())
