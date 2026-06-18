"""CLI Apps helpers for the WebUI HTTP and message surfaces."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from nanobot.apps.cli import CliAppError, CliAppManager, CliAppsRuntimeConfig
from nanobot.config.loader import load_config

QueryParams = dict[str, list[str]]

_CLI_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_CLI_APP_ATTACHMENT_KEYS = (
    "name",
    "display_name",
    "category",
    "entry_point",
    "logo_url",
    "brand_color",
)
_CATALOG_REFRESH_RETRY_SECONDS = 60.0
_catalog_refresh_lock = threading.Lock()
_catalog_refresh_running = False
_catalog_refresh_last_started = 0.0


def _start_catalog_refresh() -> bool:
    global _catalog_refresh_last_started, _catalog_refresh_running
    now = time.monotonic()
    with _catalog_refresh_lock:
        if _catalog_refresh_running:
            return True
        if now - _catalog_refresh_last_started < _CATALOG_REFRESH_RETRY_SECONDS:
            return True
        _catalog_refresh_running = True
        _catalog_refresh_last_started = now

    def refresh() -> None:
        global _catalog_refresh_running
        try:
            _manager().catalog(force_refresh=True)
        except Exception:
            pass
        finally:
            with _catalog_refresh_lock:
                _catalog_refresh_running = False

    threading.Thread(
        target=refresh,
        name="nanobot-cli-app-catalog-refresh",
        daemon=True,
    ).start()
    return True


def _clip_ws_string(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def normalize_cli_app_mentions(raw: Any) -> list[dict[str, str]]:
    """Sanitize structured CLI app mentions sent by the WebUI."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        name = _clip_ws_string(item.get("name"), 64)
        if not name or _CLI_APP_NAME_RE.match(name) is None:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, str] = {"name": key}
        for field in _CLI_APP_ATTACHMENT_KEYS[1:]:
            value = _clip_ws_string(item.get(field), 512 if field == "logo_url" else 160)
            if value:
                row[field] = value
        out.append(row)
    return out


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _manager() -> CliAppManager:
    config = load_config()
    cli_cfg = config.tools.cli_apps
    return CliAppManager(
        workspace=config.workspace_path,
        runtime=CliAppsRuntimeConfig(
            install_timeout=cli_cfg.install_timeout,
            run_timeout=cli_cfg.run_timeout,
            catalog_ttl_seconds=cli_cfg.catalog_ttl_seconds,
        ),
    )


def cli_apps_payload(*, installed_only: bool = False) -> dict[str, Any]:
    manager = _manager()
    if installed_only:
        return manager.installed_payload()
    payload = manager.payload(cache_only=True)
    refresh_pending = not manager.catalog_cache_fresh()
    if refresh_pending:
        _start_catalog_refresh()
    if not payload["apps"]:
        installed = manager.installed_payload()
        if installed["apps"]:
            payload = installed
    payload["catalog_refresh_pending"] = refresh_pending
    return payload


def cli_apps_action(action: str, query: QueryParams) -> dict[str, Any]:
    name = (_query_first(query, "name") or "").strip()
    if not name:
        raise CliAppError("missing CLI app name")
    manager = _manager()
    if action == "install":
        return manager.install(name)
    if action == "update":
        return manager.update(name)
    if action == "uninstall":
        return manager.uninstall(name)
    if action == "test":
        return manager.test(name)
    raise CliAppError(f"unknown CLI app action '{action}'", status=404)
