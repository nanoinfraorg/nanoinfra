"""Ask the executor to write the credential store, when this process may not.

**Deliberately not a method on `ExecutorClient`.** That class is imported by the tool tree, so a
method there would be one import away from a chat turn — and this one writes credentials. The
WebUI and the agent share a process, so the boundary cannot be a uid here: it is which module a
caller must reach for, and `tests/agent/test_secret_write_isolation.py` fails the build if
anything under `nanoinfra/agent/` reaches for this one.

The value is encrypted before it gets here. This module carries ciphertext and never a plaintext,
which is what makes the round trip a file write rather than a secret in flight.
"""

from __future__ import annotations

import base64
import socket
from pathlib import Path

from nanoinfra.gates.executor.protocol import (
    ExecuteResponse,
    ProtocolError,
    SecretWriteRequest,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.secrets.types import Secret

# A write is one validated file operation, so it either answers quickly or the executor is gone.
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 30.0


class SecretWriteUnavailableError(RuntimeError):
    """The executor could not be reached, so the write did not happen."""


class SecretWriteRefusedError(RuntimeError):
    """The executor refused the write, and the reason is the operator's to read."""


def _origin() -> tuple[str | None, str | None]:
    """The path and the person this request arrived on, for the log line the executor writes."""
    from nanoinfra.agent.tools.context import current_request_context

    context = current_request_context()
    if context is None:
        return None, None
    channel = (context.channel or "").strip() or None
    sender = (context.sender_id or "").strip() or None
    return channel, sender


class SecretWriteClient:
    """One request per connection, the same shape `ExecutorClient` uses."""

    def __init__(self, socket_path: Path | str) -> None:
        self._socket_path = Path(socket_path)

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def create(self, secret: Secret) -> ExecuteResponse:
        return self._send("create", secret)

    def update(self, secret: Secret) -> ExecuteResponse:
        return self._send("update", secret)

    def delete(self, secret_id: str) -> ExecuteResponse:
        return self._send_request(
            SecretWriteRequest(
                verb="delete",
                secret_id=secret_id,
                name="",
                secret_kind="",
                provider_id="",
                ciphertext_b64="",
                created_at="",
                updated_at="",
                origin_path=_origin()[0],
                origin_actor=_origin()[1],
            )
        )

    def _send(self, verb: str, secret: Secret) -> ExecuteResponse:
        origin_path, origin_actor = _origin()
        return self._send_request(
            SecretWriteRequest(
                verb=verb,
                secret_id=secret.id,
                name=secret.name,
                secret_kind=secret.kind,
                provider_id=secret.provider_id,
                ciphertext_b64=base64.b64encode(secret.ciphertext).decode("ascii"),
                created_at=secret.created_at,
                updated_at=secret.updated_at,
                origin_path=origin_path,
                origin_actor=origin_actor,
            )
        )

    def _send_request(self, request: SecretWriteRequest) -> ExecuteResponse:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(CONNECT_TIMEOUT_S)
                conn.connect(str(self._socket_path))
                conn.settimeout(WRITE_TIMEOUT_S)
                write_frame(conn, encode_request(request))
                return decode_response(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            raise SecretWriteUnavailableError(
                f"could not reach the executor at {self._socket_path}: {exc}"
            ) from exc


__all__ = [
    "SecretWriteClient",
    "SecretWriteRefusedError",
    "SecretWriteUnavailableError",
]
