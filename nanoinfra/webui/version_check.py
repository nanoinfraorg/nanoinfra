"""On-demand version checker for nanoinfra releases.

Checks PyPI for newer versions when explicitly requested (no background polling).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from nanoinfra import __version__

logger = logging.getLogger(__name__)

_PYPI_URL = "https://pypi.org/pypi/nanoinfra/json"
_CACHE_TTL_S = 300  # 5 minutes cache to avoid hammering PyPI

_cache: tuple[float, str | None] = (0.0, None)


def check_for_update() -> dict[str, Any] | None:
    """Check PyPI for a newer version. Returns update info dict or None if up-to-date.

    Uses a short cache to avoid repeated requests within the TTL window.
    This is a blocking call — invoke from a thread or background task.
    """
    global _cache
    now = time.monotonic()
    cached_at, cached_val = _cache
    if now - cached_at < _CACHE_TTL_S and cached_val is not None:
        latest = cached_val
    else:
        try:
            resp = httpx.get(_PYPI_URL, timeout=5.0, follow_redirects=True)
            resp.raise_for_status()
            latest = resp.json().get("info", {}).get("version")
        except Exception:
            # `opt(exception=True)` and not `logger.exception`: loguru drops `exc_info` as an
            # unused format binding, and `exception` would promote a best-effort background check
            # to ERROR. A missed version banner is not an error.
            logger.opt(exception=True).debug("PyPI version check failed")
            return None
        _cache = (now, latest)

    if not latest or latest == __version__:
        return None
    return {
        "currentVersion": __version__,
        "latestVersion": latest,
        "pypiUrl": "https://pypi.org/project/nanoinfra/",
    }
