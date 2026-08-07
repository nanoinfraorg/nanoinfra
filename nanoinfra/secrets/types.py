"""Persistent types for stored secrets.

Two serialization methods on the same dataclass, deliberately named apart:
``to_storage_dict`` is the full at-rest representation (includes
ciphertext) written to a local JSON file or a Postgres row.
``to_public_dict`` is the only shape ever returned by a REST route or agent
tool -- metadata only, so a future caller can't wire the wrong one into a
response by picking the "the dict method" without reading which one.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any


@dataclass
class Secret:
    """One stored credential. ``ciphertext`` is opaque outside crypto.py."""

    id: str
    name: str
    kind: str  # "password" | "api_key" | "ssh_key" | "token" -- UI hint only
    provider_id: str  # "local" | "postgres"
    ciphertext: bytes
    created_at: str
    updated_at: str

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "providerId": self.provider_id,
            "ciphertext": base64.b64encode(self.ciphertext).decode("ascii"),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "providerId": self.provider_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> Secret:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            kind=str(data["kind"]),
            provider_id=str(data["providerId"]),
            ciphertext=base64.b64decode(str(data["ciphertext"])),
            created_at=str(data["createdAt"]),
            updated_at=str(data["updatedAt"]),
        )


__all__ = ["Secret"]
