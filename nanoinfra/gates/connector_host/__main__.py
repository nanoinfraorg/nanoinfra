"""The connector host entry point (#195, part 4).

``python -m nanoinfra.gates.connector_host --socket <path> --workspace <path>``. The same three
flags as the executor, the fetcher and the MCP host, because one shape means one supervisor pattern
-- and `--config` is here for the reason it had to be added to the other three: without it the
loader falls back to `~/.nanoinfra/config.json`, so an instance started with `--config` ran its
helper against another instance's settings.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nanoinfra.utils.process_name import CONNECTOR_HOST_NAME, set_process_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nanoinfra.gates.connector_host",
        description="Make one declared connector request per frame, over a Unix domain socket.",
    )
    parser.add_argument("--socket", required=True, type=Path, help="Unix socket path to bind.")
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace root path.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Config file the parent loaded. Defaults to the standard location.",
    )
    args = parser.parse_args(argv)

    if args.config is not None:
        from nanoinfra.config.loader import set_config_path

        set_config_path(args.config)

    # Local, so a bad argument fails before httpx is imported.
    from nanoinfra.gates.connector_host.server import serve_forever

    serve_forever(args.socket, workspace=args.workspace)
    return 0


if __name__ == "__main__":
    set_process_name(CONNECTOR_HOST_NAME)
    raise SystemExit(main())
