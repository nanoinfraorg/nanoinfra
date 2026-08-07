"""REST-facing payload builders for the WebUI Secrets module.

Pattern: nanoinfra/webui/diagrams_api.py -- small pure functions the
gateway HTTP dispatcher (ws_http.py) calls into, gated by check_api_token
at the call site, not in here. Every payload here uses Secret.to_public_dict()
-- never to_storage_dict() -- so a decrypted or encrypted value can never
reach an HTTP response through this module.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.secrets.store import SecretStore


def webui_secrets_payload(store: SecretStore) -> dict[str, Any]:
    """``GET /api/webui/secrets``."""
    return {"secrets": [secret.to_public_dict() for secret in store.list_secrets()]}


def webui_secret_detail_payload(store: SecretStore, secret_id: str) -> dict[str, Any] | None:
    """``GET /api/webui/secrets/<id>`` -- ``None`` means 404."""
    secret = store.get(secret_id)
    if secret is None:
        return None
    return {"secret": secret.to_public_dict()}


def create_webui_secret(store: SecretStore, raw: dict[str, Any]) -> dict[str, Any]:
    """Raises SecretValidationError (-> 400) or SecretsNotConfiguredError /
    PostgresSecretsNotConfiguredError (-> the caller maps those to 409)."""
    secret = store.create(raw)
    return {"secret": secret.to_public_dict()}


def update_webui_secret(store: SecretStore, secret_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    secret = store.update(secret_id, raw)
    if secret is None:
        return None
    return {"secret": secret.to_public_dict()}


def delete_webui_secret(store: SecretStore, secret_id: str) -> bool:
    return store.delete(secret_id)


__all__ = [
    "create_webui_secret",
    "delete_webui_secret",
    "update_webui_secret",
    "webui_secret_detail_payload",
    "webui_secrets_payload",
]
