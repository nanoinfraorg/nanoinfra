"""Runtime log visibility controls shared by CLI commands."""

from loguru import logger

__all__ = ["_set_nanoinfra_logs"]


def _set_nanoinfra_logs(enabled: bool, *, always_on: "tuple[str, ...]" = ()) -> None:
    """Enable or silence the package's logs, keeping *always_on* modules audible either way.

    `logger.disable("nanoinfra")` silences the whole package -- including its exception handlers.
    That is fine for a CLI one-shot and wrong for a server: the API's log held 45 boot lines and
    nothing else after a dozen requests, so the hang that became 1.7.4 had to be diagnosed with a
    packet capture (#215). A server names the modules whose lines it always wants.
    """
    if enabled:
        logger.enable("nanoinfra")
        return
    logger.disable("nanoinfra")
    for name in always_on:
        logger.enable(name)
