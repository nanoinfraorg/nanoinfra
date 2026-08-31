"""Write the credential store on behalf of the account that cannot.

The store belongs to the executor account: `secrets/` is `drwxr-x---` owned by it, with a group
read so the gateway can list metadata. That mode is deliberate — the process the model steers
can enumerate credentials and cannot alter them — and it meant the Secrets page could not create
a secret at all on any deployment with the privilege split.

So the write crosses the wire instead of the mode changing. Three properties make that safe, and
all three are the reason this is not simply "let the gateway write":

- **The bytes are already encrypted.** The gateway holds the key, so it encrypts and this side
  writes ciphertext it cannot read. What moved across the boundary is a file write, not a secret.
- **The verbs are fixed.** `create`, `update`, `delete`, and no read. Reading a plaintext is the
  one thing this wire has never carried, and adding a write does not change that.
- **The id is validated here, not trusted.** A caller names the record it writes, so the name and
  the id are checked against the store's own rules before anything touches the disk.

An update that names no existing record is refused rather than turned into a create: a caller
that believes it is editing must not silently add a row.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.gates.executor.protocol import (
    SECRET_VERBS,
    ExecuteResponse,
    SecretWriteRequest,
)
from nanoinfra.secrets.store import SecretsStoreUnreadableError, SecretStore
from nanoinfra.secrets.types import Secret

# What one record may carry. A ciphertext far above this is not a credential, and the frame cap
# already bounds the whole request; this bounds the field a caller controls.
MAX_CIPHERTEXT_BYTES = 256 * 1024


def _refusal(reason: str) -> ExecuteResponse:
    return ExecuteResponse(ok=False, output="", exit_code=None, error=None, reason=reason)


@dataclass
class SecretWriteRunner:
    """Answers one secret write. Holds the store and nothing else."""

    workspace: Path
    audit: Any = None

    def handle(self, request: SecretWriteRequest) -> ExecuteResponse:
        """Perform one write. Never raises for a refusal: a refusal is a response."""
        if request.verb not in SECRET_VERBS:
            return _refusal(
                f"secret verb {request.verb!r} is not one of {sorted(SECRET_VERBS)}."
            )

        store = SecretStore(self.workspace)
        existing = store.get(request.secret_id)

        if request.verb == "delete":
            if existing is None:
                return _refusal(f"no secret matches {request.secret_id!r}.")
            try:
                deleted = store.delete(request.secret_id)
            except SecretsStoreUnreadableError as exc:
                return _refusal(f"the store refused the delete: {exc}")
            self._record(request, "delete")
            return ExecuteResponse(
                ok=deleted,
                output=request.secret_id,
                exit_code=0 if deleted else None,
                error=None,
                reason=f"deleted {request.secret_id}" if deleted else "nothing was deleted",
            )

        if request.verb == "update" and existing is None:
            # Not turned into a create: a caller that believes it is editing must not add a row.
            return _refusal(f"no secret matches {request.secret_id!r}, so there is nothing to update.")
        if request.verb == "create" and existing is not None:
            return _refusal(f"a secret with id {request.secret_id!r} already exists.")

        if not request.name.strip():
            return _refusal("a secret needs a name.")
        try:
            ciphertext = base64.b64decode(request.ciphertext_b64.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            return _refusal(f"the ciphertext is not valid base64: {exc}")
        if not ciphertext:
            return _refusal("the ciphertext is empty, so there is nothing to store.")
        if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
            return _refusal(
                f"the ciphertext is {len(ciphertext)} bytes, above the {MAX_CIPHERTEXT_BYTES} cap."
            )

        secret = Secret(
            id=request.secret_id,
            name=request.name,
            kind=request.secret_kind,
            provider_id=request.provider_id or "local",
            ciphertext=ciphertext,
            created_at=request.created_at or (existing.created_at if existing else ""),
            updated_at=request.updated_at,
        )
        try:
            if request.verb == "create":
                store.write_record(secret)
            else:
                store.write_update(secret)
        except SecretsStoreUnreadableError as exc:
            # This side owns the directory, so a refusal here is a deployment fault rather than
            # a permission boundary doing its job.
            return _refusal(f"the store refused the write: {exc}")
        except ValueError as exc:
            return _refusal(str(exc))

        self._record(request, request.verb)
        return ExecuteResponse(
            ok=True,
            output=secret.id,
            exit_code=0,
            error=None,
            reason=f"{request.verb}d secret {secret.id}",
        )

    def _record(self, request: SecretWriteRequest, verb: str) -> None:
        """Log the write. The id and the name, never the value.

        Not an audit-log decision record: the class is `mutate.local`, which the gate does not
        decide, and a record there would imply a decision nobody took. This is the line an
        operator greps when a credential changed and nobody remembers doing it.
        """
        logger.info(
            "secrets: {} {} ({}) requested by {} on {}",
            verb,
            request.secret_id,
            request.name or "-",
            request.origin_actor or "an unnamed caller",
            request.origin_path or "an unnamed path",
        )


__all__ = ["MAX_CIPHERTEXT_BYTES", "SecretWriteRunner"]
