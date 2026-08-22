"""Persistent types for local triggers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from nanoinfra.automations.commissioning_state import CommissioningState
from nanoinfra.automations.delivery import DEFAULT_DELIVERY_POLICY, normalize_policy
from nanoinfra.utils.dict_keys import get_camel_snake as _get

TriggerStatus = Literal["ok", "error"]


def _int_or_zero(value: Any) -> int:
    """Coerce a stored JSON numeric, using zero for null or empty values."""
    return 0 if value is None or value == "" else int(value)


def _references(value: Any) -> list[dict[str, str]]:
    """Read stored ``{kind, id}`` pairs, dropping anything malformed."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for entry in list(cast("list[object] | tuple[object, ...]", value)):
        if not isinstance(entry, dict):
            continue
        item = cast("dict[str, object]", entry)
        kind = str(item.get("kind") or "").strip()
        ident = str(item.get("id") or "").strip()
        if kind and ident:
            out.append({"kind": kind, "id": ident})
    return out


def _names(value: Any) -> list[str]:
    """Read a stored list of names, tolerating a null or a stray scalar."""
    if not isinstance(value, (list, tuple)):
        return []
    entries = list(cast("list[object] | tuple[object, ...]", value))
    return [str(name).strip() for name in entries if str(name).strip()]


def _optional_int(value: Any) -> int | None:
    """Coerce a stored JSON numeric; null/blank stays None."""
    if value is None or value == "":
        return None
    return int(value)


@dataclass
class TriggerRunRecord:
    """A single local trigger delivery record."""

    run_at_ms: int
    status: TriggerStatus
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TriggerRunRecord":
        return cls(
            run_at_ms=_int_or_zero(_get(data, "runAtMs", "run_at_ms", 0)),
            status=str(data.get("status") or "error"),  # type: ignore[arg-type]
            error=data.get("error"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runAtMs": self.run_at_ms,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class LocalTrigger:
    """A session-bound local trigger."""

    id: str
    name: str
    enabled: bool
    channel: str
    chat_id: str
    session_key: str
    sender_id: str = "trigger"
    #: Whether this trigger's outcome reaches the operator. Defaults to today's behaviour.
    delivery: str = DEFAULT_DELIVERY_POLICY
    #: Skills this trigger's prompt carries in full. Empty means the whole catalogue.
    skills: list[str] = field(default_factory=list[str])
    #: Resources this trigger references, as ``{"kind": ..., "id": ...}``. See CronJob.references.
    references: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    #: SHA-256 of this trigger's key, or empty when it has none. The plaintext is shown once at
    #: issue time and never stored, so a stolen triggers.json yields no working key. A plain
    #: digest rather than a KDF is deliberate: the key is 32 random bytes, not a password, so
    #: there is no dictionary to stretch against.
    #: What a commissioning run found about this trigger (#189). See CronJob.commissioning.
    commissioning: CommissioningState = field(default_factory=CommissioningState)
    key_hash: str = ""
    key_created_at_ms: int = 0
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    last_message: str = ""
    last_run_at_ms: int | None = None
    last_status: TriggerStatus | None = None
    last_error: str | None = None
    run_history: list[TriggerRunRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LocalTrigger":
        raw_history = cast(
            list[Any],
            data.get("runHistory", data.get("run_history", [])) or [],
        )
        history: list[TriggerRunRecord] = [
            record
            if isinstance(record, TriggerRunRecord)
            else TriggerRunRecord.from_dict(cast(dict[str, Any], record))
            for record in raw_history
            if isinstance(record, (dict, TriggerRunRecord))
        ]
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            enabled=bool(data.get("enabled", True)),
            channel=str(data.get("channel") or ""),
            chat_id=str(_get(data, "chatId", "chat_id", "")),
            session_key=str(_get(data, "sessionKey", "session_key", "")),
            sender_id=str(_get(data, "senderId", "sender_id", "trigger") or "trigger"),
            delivery=normalize_policy(data.get("delivery")),
            skills=_names(data.get("skills")),
            references=_references(data.get("references")),
            commissioning=CommissioningState.from_dict(data.get("commissioning")),
            key_hash=str(_get(data, "keyHash", "key_hash", "") or ""),
            key_created_at_ms=_int_or_zero(_get(data, "keyCreatedAtMs", "key_created_at_ms", 0)),
            origin_metadata=dict(_get(data, "originMetadata", "origin_metadata", {}) or {}),
            created_at_ms=_int_or_zero(_get(data, "createdAtMs", "created_at_ms", 0)),
            updated_at_ms=_int_or_zero(_get(data, "updatedAtMs", "updated_at_ms", 0)),
            last_message=str(_get(data, "lastMessage", "last_message", "") or ""),
            last_run_at_ms=_optional_int(_get(data, "lastRunAtMs", "last_run_at_ms")),
            last_status=_get(data, "lastStatus", "last_status"),  # type: ignore[arg-type]
            last_error=_get(data, "lastError", "last_error"),
            run_history=history,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "channel": self.channel,
            "chatId": self.chat_id,
            "sessionKey": self.session_key,
            "senderId": self.sender_id,
            "delivery": self.delivery,
            "skills": list(self.skills),
            "references": [dict(item) for item in self.references],
            "commissioning": self.commissioning.to_dict(),
            "keyHash": self.key_hash,
            "keyCreatedAtMs": self.key_created_at_ms,
            "originMetadata": self.origin_metadata,
            "createdAtMs": self.created_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "lastMessage": self.last_message,
            "lastRunAtMs": self.last_run_at_ms,
            "lastStatus": self.last_status,
            "lastError": self.last_error,
            "runHistory": [record.to_dict() for record in self.run_history],
        }


@dataclass
class TriggerDelivery:
    """One pending local trigger delivery written by the CLI."""

    id: str
    trigger_id: str
    content: str
    created_at_ms: int
    attempts: int = 0
    last_error: str | None = None
    #: Earliest time this delivery may be claimed again. Zero means "now", which is what every
    #: delivery written before backoff existed carries, so old files stay claimable.
    not_before_ms: int = 0
    #: The dead-lettered delivery this one was replayed from. Lineage, not a copy: a replay must
    #: be identifiable in history rather than reading as a fresh event.
    replay_of: str = ""
    path: Path | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        path: Path | None = None,
    ) -> "TriggerDelivery":
        return cls(
            id=str(data["id"]),
            trigger_id=str(_get(data, "triggerId", "trigger_id", "")),
            content=str(data.get("content") or ""),
            created_at_ms=_int_or_zero(_get(data, "createdAtMs", "created_at_ms", 0)),
            attempts=_int_or_zero(data.get("attempts", 0)),
            last_error=data.get("lastError") or data.get("last_error"),
            not_before_ms=_int_or_zero(_get(data, "notBeforeMs", "not_before_ms", 0)),
            replay_of=str(_get(data, "replayOf", "replay_of", "") or ""),
            path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "triggerId": self.trigger_id,
            "content": self.content,
            "createdAtMs": self.created_at_ms,
            "attempts": self.attempts,
            "lastError": self.last_error,
            "notBeforeMs": self.not_before_ms,
            "replayOf": self.replay_of,
        }
