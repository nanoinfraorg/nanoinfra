"""Cron types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast, overload

from nanoinfra.automations.commissioning_state import CommissioningState
from nanoinfra.automations.delivery import DEFAULT_DELIVERY_POLICY, normalize_policy
from nanoinfra.utils.backoff import (
    DEFAULT_BASE_DELAY_MS,
    DEFAULT_MAX_DELAY_MS,
    BackoffPolicy,
)
from nanoinfra.utils.dict_keys import get_camel_snake


@overload
def _store_int(value: Any, default: Literal[None]) -> int | None: ...


@overload
def _store_int(value: Any, default: int = 0) -> int: ...


def _store_int(value: Any, default: int | None = 0) -> int | None:
    """Coerce JSON numerics to int; treat null/blank like a missing key."""
    if value is None or value == "":
        return default
    return int(value)


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronSchedule:
        return cls(
            kind=data["kind"],
            at_ms=_store_int(get_camel_snake(data, "atMs", "at_ms"), None),
            every_ms=_store_int(get_camel_snake(data, "everyMs", "every_ms"), None),
            expr=data.get("expr"),
            tz=data.get("tz"),
        )


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Legacy delivery fields used by pre-session-bound cron jobs.
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    channel_meta: dict[str, Any] = field(default_factory=dict)
    session_key: str | None = None  # original session key for correct session recording
    origin_channel: str | None = None
    origin_chat_id: str | None = None
    origin_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronPayload:
        return cls(
            kind=data.get("kind", "agent_turn"),
            message=data.get("message", ""),
            deliver=data.get("deliver", False),
            channel=data.get("channel"),
            to=data.get("to"),
            channel_meta=dict(
                get_camel_snake(data, "channelMeta", "channel_meta", {}) or {}
            ),
            session_key=get_camel_snake(data, "sessionKey", "session_key"),
            origin_channel=get_camel_snake(data, "originChannel", "origin_channel"),
            origin_chat_id=get_camel_snake(data, "originChatId", "origin_chat_id"),
            origin_metadata=dict(
                get_camel_snake(data, "originMetadata", "origin_metadata", {}) or {}
            ),
        )



def _store_references(value: Any) -> list[dict[str, str]]:
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


def _store_names(value: Any) -> list[str]:
    """Read a stored list of names, tolerating a null or a stray scalar."""
    if not isinstance(value, (list, tuple)):
        return []
    entries = list(cast("list[object] | tuple[object, ...]", value))
    return [str(name).strip() for name in entries if str(name).strip()]

@dataclass
class CronRetryPolicy:
    """How a failed run is retried. Off by default, so upgrading changes nothing."""

    #: Retries after the first failure, not total attempts. Zero disables retrying.
    attempts: int = 0
    base_delay_ms: int = DEFAULT_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS

    @property
    def enabled(self) -> bool:
        return self.attempts > 0

    def backoff(self) -> BackoffPolicy:
        return BackoffPolicy(base_delay_ms=self.base_delay_ms, max_delay_ms=self.max_delay_ms)

    @classmethod
    def from_store_dict(cls, data: dict[str, Any] | None) -> CronRetryPolicy:
        if not data:
            return cls()
        return cls(
            attempts=_store_int(data.get("attempts"), 0) or 0,
            base_delay_ms=(
                _store_int(get_camel_snake(data, "baseDelayMs", "base_delay_ms"), None)
                or DEFAULT_BASE_DELAY_MS
            ),
            max_delay_ms=(
                _store_int(get_camel_snake(data, "maxDelayMs", "max_delay_ms"), None)
                or DEFAULT_MAX_DELAY_MS
            ),
        )


@dataclass
class CronRunRecord:
    """A single execution record for a cron job."""
    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None
    #: Why this run happened: "scheduled", "manual" or "retry". A record written before this
    #: existed reads as "scheduled", which is what it almost certainly was.
    reason: str = "scheduled"

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronRunRecord:
        return cls(
            run_at_ms=_store_int(get_camel_snake(data, "runAtMs", "run_at_ms", 0)),
            status=data["status"],
            duration_ms=_store_int(get_camel_snake(data, "durationMs", "duration_ms", 0)),
            error=data.get("error"),
            reason=str(data.get("reason") or "scheduled"),
        )


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)
    #: Retries already spent on the *current* failure. Reset the moment a run succeeds, is
    #: skipped, or exhausts the policy, so it measures this outage rather than the job's life.
    retry_attempts: int = 0
    #: True while ``next_run_at_ms`` holds a retry rather than a scheduled slot. Without this,
    #: recomputing next-run times on startup would silently drop a pending retry.
    retry_pending: bool = False

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronJobState:
        history = cast(
            list[object],
            get_camel_snake(data, "runHistory", "run_history", []) or [],
        )
        return cls(
            next_run_at_ms=_store_int(
                get_camel_snake(data, "nextRunAtMs", "next_run_at_ms"), None
            ),
            last_run_at_ms=_store_int(
                get_camel_snake(data, "lastRunAtMs", "last_run_at_ms"), None
            ),
            last_status=get_camel_snake(data, "lastStatus", "last_status"),
            last_error=get_camel_snake(data, "lastError", "last_error"),
            run_history=[
                record
                if isinstance(record, CronRunRecord)
                else CronRunRecord.from_store_dict(cast(dict[str, Any], record))
                for record in history
                if isinstance(record, (dict, CronRunRecord))
            ],
            retry_attempts=_store_int(
                get_camel_snake(data, "retryAttempts", "retry_attempts", 0)
            ),
            retry_pending=bool(get_camel_snake(data, "retryPending", "retry_pending", False)),
        )


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    retry: CronRetryPolicy = field(default_factory=CronRetryPolicy)
    #: Whether this job's outcome reaches the operator. Defaults to today's behaviour.
    delivery: str = DEFAULT_DELIVERY_POLICY
    #: Skills this job's prompt carries in full. Empty means the whole catalogue is summarised,
    #: which is what every job had before.
    skills: list[str] = field(default_factory=list[str])
    #: MCP servers this automation declares (#204). A `mention` server sends only a one-line
    #: advertisement unless a turn names it, and an unattended turn has nobody to type `@server`.
    mcp_presets: list[str] = field(default_factory=list[str])
    #: Data connectors this automation declares (#204). A `mention` connector sends only a
    #: one-line advertisement unless a turn names it, and an unattended turn types no `@`.
    connectors: list[str] = field(default_factory=list[str])
    #: Built-in tool groups this automation declares (#210). Same reason as the two above: a
    #: `mention` group is one advertised line until a turn names it, and nobody types `@` here.
    tool_groups: list[str] = field(default_factory=list[str])
    #: Resources this job references, as ``{"kind": ..., "id": ...}``. Ids only: the name is
    #: re-read at run time, so a renamed server keeps resolving. Resolved *before* the turn is
    #: built, and an id that no longer resolves stops the run rather than letting the model fall
    #: back to matching on a name.
    references: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    #: What a commissioning run found about this job (#189). Unchecked by default, which is also
    #: what every job written before commissioning existed reads as.
    commissioning: CommissioningState = field(default_factory=CommissioningState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False

    @classmethod
    def from_dict(cls, kwargs: dict[str, Any]) -> CronJob:
        state_kwargs = dict(cast(dict[str, Any], kwargs.get("state", {})))
        state_kwargs["run_history"] = [
            record
            if isinstance(record, CronRunRecord)
            else CronRunRecord(**cast(dict[str, Any], record))
            for record in cast(list[object], state_kwargs.get("run_history", []))
        ]
        kwargs["schedule"] = CronSchedule(
            **cast(dict[str, Any], kwargs.get("schedule", {"kind": "every"}))
        )
        kwargs["payload"] = CronPayload(**cast(dict[str, Any], kwargs.get("payload", {})))
        kwargs["state"] = CronJobState(**state_kwargs)
        retry = kwargs.get("retry")
        if retry is not None and not isinstance(retry, CronRetryPolicy):
            kwargs["retry"] = CronRetryPolicy(**cast(dict[str, Any], retry))
        # This path reads `asdict(job)` from the pending-action file, so every nested dataclass
        # arrives as a plain dict. A dict left here reaches `_save_store`, which then asks it for
        # `to_dict` and fails the whole store write -- the same class of fault as #179's.
        commissioning = kwargs.get("commissioning")
        if commissioning is not None and not isinstance(commissioning, CommissioningState):
            kwargs["commissioning"] = CommissioningState.from_dict(commissioning)
        return cls(**cast(Any, kwargs))

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> CronJob:
        """Load a job from jobs.json (camelCase with snake_case fallbacks)."""
        return cls(
            id=data["id"],
            name=data["name"],
            enabled=data.get("enabled", True),
            schedule=CronSchedule.from_store_dict(data["schedule"]),
            payload=CronPayload.from_store_dict(data.get("payload") or {}),
            state=CronJobState.from_store_dict(data.get("state") or {}),
            retry=CronRetryPolicy.from_store_dict(data.get("retry")),
            delivery=normalize_policy(data.get("delivery")),
            skills=_store_names(data.get("skills")),
            mcp_presets=_store_names(get_camel_snake(data, "mcpPresets", "mcp_presets", [])),
            connectors=_store_names(data.get("connectors")),
            tool_groups=_store_names(get_camel_snake(data, "toolGroups", "tool_groups", [])),
            references=_store_references(data.get("references")),
            commissioning=CommissioningState.from_dict(data.get("commissioning")),
            created_at_ms=_store_int(get_camel_snake(data, "createdAtMs", "created_at_ms", 0)),
            updated_at_ms=_store_int(get_camel_snake(data, "updatedAtMs", "updated_at_ms", 0)),
            delete_after_run=bool(
                get_camel_snake(data, "deleteAfterRun", "delete_after_run", False)
            ),
        )


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
