"""Persistent type for one Server execution job.

``command`` and ``output``/``error`` must never contain a decrypted
secretRef value -- that discipline lives in the execution backends
(Tasks 4-7), not here; this module only defines the shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ServerJob:
    id: str
    server_id: str
    provider_id: str
    command: str
    status: str  # "queued" | "running" | "completed" | "failed" | "timed_out"
    created_at: str
    started_at: str | None
    ended_at: str | None
    exit_code: int | None
    output: str
    error: str | None
    timeout_s: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "serverId": self.server_id,
            "providerId": self.provider_id,
            "command": self.command,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "exitCode": self.exit_code,
            "output": self.output,
            "error": self.error,
            "timeoutS": self.timeout_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerJob:
        return cls(
            id=str(data["id"]),
            server_id=str(data["serverId"]),
            provider_id=str(data["providerId"]),
            command=str(data["command"]),
            status=str(data["status"]),
            created_at=str(data["createdAt"]),
            started_at=(str(data["startedAt"]) if data.get("startedAt") else None),
            ended_at=(str(data["endedAt"]) if data.get("endedAt") else None),
            exit_code=(int(data["exitCode"]) if data.get("exitCode") is not None else None),
            output=str(data.get("output") or ""),
            error=(str(data["error"]) if data.get("error") else None),
            timeout_s=int(data["timeoutS"]),
        )


__all__ = ["ServerJob"]
