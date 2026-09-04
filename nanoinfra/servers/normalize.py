"""Validation for untrusted Server creation/update payloads."""

from __future__ import annotations

from typing import Any, cast

from nanoinfra.servers.types import Server

_VALID_PROVIDERS = {"ssh", "ansible-runner", "ssm", "api"}
_MAX_NAME_LENGTH = 120


class ServerValidationError(ValueError):
    """Raised when a server payload has a structural problem the client must fix."""


def normalize_server_input(raw: Any, *, server_id: str) -> Server:
    if not isinstance(raw, dict):
        raise ServerValidationError("server payload must be an object")
    payload = cast(dict[str, Any], raw)

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ServerValidationError("name is required")
    name = name[:_MAX_NAME_LENGTH]

    provider_id = str(payload.get("providerId") or "")
    if provider_id not in _VALID_PROVIDERS:
        raise ServerValidationError(f"providerId must be one of {sorted(_VALID_PROVIDERS)}, got {provider_id!r}")

    raw_config_value: Any = payload.get("config") or {}
    if not isinstance(raw_config_value, dict):
        raise ServerValidationError("config must be an object")
    raw_config = cast(dict[str, Any], raw_config_value)
    config = {str(k): str(v) for k, v in raw_config.items()}

    secret_ref = payload.get("secretRef")
    secret_ref = str(secret_ref) if secret_ref else None

    raw_tags_value: Any = payload.get("tags") or []
    if not isinstance(raw_tags_value, list):
        raise ServerValidationError("tags must be an array")
    raw_tags = cast(list[Any], raw_tags_value)
    tags = [str(t) for t in raw_tags]

    return Server(
        id=server_id,
        name=name,
        provider_id=provider_id,
        config=config,
        secret_ref=secret_ref,
        tags=tags,
        created_at="",
        updated_at="",
        # Not read off the payload on purpose (#225): a client cannot claim a server has memory,
        # and ``ServerStore.update`` carries the real value across. Only a notes write moves it.
        notes_updated_at=None,
    )


__all__ = ["ServerValidationError", "normalize_server_input"]
