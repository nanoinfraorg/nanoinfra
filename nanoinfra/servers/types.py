"""Persistent types for the server inventory.

Field names mirror nanoinfra/diagrams/types.py's camelCase-JSON /
snake_case-Python split. ``secret_ref`` is an opaque string (a Secret's
id, by convention) -- this module has no import of nanoinfra.secrets and
never validates it points anywhere real; that's the execution engine's
job, not inventory CRUD's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Server:
    """One inventoried server: how to reach it, and how to authenticate."""

    id: str
    name: str
    provider_id: str  # "ssh" | "ansible-runner" | "ssm" | "api"
    config: dict[str, str] = field(default_factory=dict)
    secret_ref: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "providerId": self.provider_id,
            "config": self.config,
            "secretRef": self.secret_ref,
            "tags": self.tags,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Server:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            provider_id=str(data["providerId"]),
            config={str(k): str(v) for k, v in dict(data.get("config") or {}).items()},
            secret_ref=(str(data["secretRef"]) if data.get("secretRef") else None),
            tags=[str(t) for t in list(data.get("tags") or [])],
            created_at=str(data["createdAt"]),
            updated_at=str(data["updatedAt"]),
        )


@dataclass
class ServerSummary:
    """The lightweight listing shape shown in the inventory gallery and TargetPicker."""

    id: str
    name: str
    provider_id: str
    tags: list[str]
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "providerId": self.provider_id,
            "tags": self.tags,
            "updatedAt": self.updated_at,
        }


__all__ = ["Server", "ServerSummary"]
