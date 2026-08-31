"""REST-facing payload builders for the WebUI Secrets module.

Pattern: nanoinfra/webui/diagrams_api.py -- small pure functions the
gateway HTTP dispatcher (ws_http.py) calls into, gated by check_api_token
at the call site, not in here. Every payload here uses Secret.to_public_dict()
-- never to_storage_dict() -- so a decrypted or encrypted value can never
reach an HTTP response through this module.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanoinfra.secrets.store import SecretsStoreUnreadableError, SecretStore
from nanoinfra.secrets.types import Secret


def _delegate_write(verb: str, secret: Secret | None, secret_id: str) -> None:
    """Ask the executor to perform a write this process may not.

    Reached only after the store refused, which is the container case: `secrets/` belongs to
    the executor account, with a group read so this process can list metadata and not alter it.
    The record is already encrypted here, so what crosses the socket is a file write.

    Imported inside the function on purpose. The module-level import graph is what
    `tests/agent/test_secret_write_isolation.py` reads, and a credential-writing client should
    not become a module every importer of this one also gets.
    """
    from nanoinfra.agent.tools.server_execution import default_socket_path
    from nanoinfra.webui.secret_write_client import (
        SecretWriteClient,
        SecretWriteRefusedError,
        SecretWriteUnavailableError,
    )

    client = SecretWriteClient(default_socket_path())
    try:
        if verb == "create" and secret is not None:
            response = client.create(secret)
        elif verb == "update" and secret is not None:
            response = client.update(secret)
        else:
            response = client.delete(secret_id)
    except SecretWriteUnavailableError as exc:
        raise SecretsStoreUnreadableError(
            f"the secret store belongs to another account and the executor is not reachable, "
            f"so nothing was written: {exc}"
        ) from exc
    if not response.ok:
        raise SecretWriteRefusedError(response.reason or "the executor refused the write")
    logger.info("secrets: {} {} performed by the executor", verb, secret_id)


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
    secret = store.build_create(raw)
    try:
        store.write_record(secret)
    except SecretsStoreUnreadableError:
        # The store belongs to the executor account. Ask it, rather than widening the mode on a
        # directory whose mode is the reason a compromised agent cannot replace a credential.
        _delegate_write("create", secret, secret.id)
    return {"secret": secret.to_public_dict()}


def update_webui_secret(store: SecretStore, secret_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    secret = store.build_update(secret_id, raw)
    if secret is None:
        return None
    try:
        store.write_update(secret)
    except SecretsStoreUnreadableError:
        _delegate_write("update", secret, secret.id)
    return {"secret": secret.to_public_dict()}


def delete_webui_secret(store: SecretStore, secret_id: str) -> bool:
    try:
        return store.delete(secret_id)
    except SecretsStoreUnreadableError:
        # A delete is a write too, so it meets the same refusal in the same deployments.
        if store.get(secret_id) is None:
            return False
        _delegate_write("delete", None, secret_id)
        return True


__all__ = [
    "create_webui_secret",
    "delete_webui_secret",
    "update_webui_secret",
    "webui_secret_detail_payload",
    "webui_secrets_payload",
]
