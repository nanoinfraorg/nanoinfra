"""Runtime log visibility controls shared by CLI commands."""

from loguru import logger

__all__ = ["_set_nanoinfra_logs"]


def _set_nanoinfra_logs(enabled: bool) -> None:
    if enabled:
        logger.enable("nanoinfra")
    else:
        logger.disable("nanoinfra")
