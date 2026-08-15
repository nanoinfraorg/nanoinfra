"""Rebuild denial-latch state from the audit log -- nanoinfraorg/nanoinfra#32.

#15 keeps latch state in the process, and it rejected a latch file for a sound reason: the
model holds filesystem tools, so a file within their reach is more model-reachable than
memory. The consequence is that a restart drops every latch, and the agent can cause the
restart. ``ExecTool`` is ``mutate.local``, #8 does not gate that class, and ``kill -TERM
$PPID`` under systemd ``Restart=`` or a Docker restart policy brings the gateway back with no
latches. Deny, restart, retry is then a loop that no human rate-limits.

The audit log from #16 is the right store, and the latch needs no file of its own. That log
appends, it lives outside the workspace, and a model cannot un-append a line.

Two states are different and must stay different. An empty log is a fresh install, and it
holds no latches. An unreadable log cannot say which sessions to latch, so it degrades and
every gated action waits for an operator. An unreadable audit log must never read as "no
latches".

**A moved log is not an empty log.** #36 found that the agent account could rename the audit
directory, because write rights on a parent allow a rename of any entry inside it. The
segments then vanish, and an absent log reads exactly like a fresh one. Nothing in this module
can tell the two apart. A marker file, or a sentinel inside the directory, sits in the same
renameable parent and moves with it. So the answer is not here. ``entrypoint.sh`` takes the
write right on the parent away, and an ``AuditStore`` opened with ``pin_root`` raises
``AuditRootChangedError`` once its root changes identity. That is an ``OSError``, so it takes the
degraded path below, and a moved log then latches every session instead of none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from nanoinfra.gates.latch import LatchEventKind

if TYPE_CHECKING:
    from nanoinfra.gates.audit import AuditStore

# Decisions that move latch state. The store holds every gate decision, and the rest of them
# say nothing about a latch.
_DENIED = LatchEventKind.DENIED.value
_REFUSED = LatchEventKind.REFUSED.value
_CLEARED = LatchEventKind.CLEARED.value


@dataclass(frozen=True, slots=True)
class RestoredLatches:
    """What the log says the latch state was, at start.

    Read-only by design. The gate half of #15 has no clearing member, and this value must not
    become one either. An operator clears through ``LatchController`` and nothing else.
    """

    latched: dict[tuple[str, str], float] = field(default_factory=dict)
    refusals: dict[tuple[str, str], int] = field(default_factory=dict)
    degraded: bool = False

    def is_latched(self, session_id: str, capability_class: str) -> bool:
        """True when this pair was latched, or when the log could not be read at all."""
        if self.degraded:
            # Fail closed. The log cannot name the sessions it lost, so every pair waits.
            return True
        return (session_id, capability_class) in self.latched

    def refusal_count(self, session_id: str, capability_class: str) -> int:
        return self.refusals.get((session_id, capability_class), 0)

    def summary(self) -> str:
        """One line for the startup echo (#8)."""
        if self.degraded:
            return (
                "gates: the audit log could not be read, so every session stays latched. "
                "An operator must clear each one after the log is readable again."
            )
        if not self.latched:
            return "gates: no latched sessions restored from the audit log"
        pairs = ", ".join(f"{session}/{cls}" for session, cls in sorted(self.latched))
        return f"gates: restored {len(self.latched)} latched session(s) from the audit log: {pairs}"


def restore_latches(store: AuditStore) -> RestoredLatches:
    """Read the audit segments and return the latch state they describe.

    The latest ``denied`` or ``cleared`` record per session and class decides. Order in the
    file is the order of events, because #16 appends and never rewrites.

    A read failure degrades the whole result. A single unparseable line does not: #16
    documents that only the last line of a segment can tear, and that every earlier record
    survives, so a torn tail must cost the tail rather than every latch the log holds.
    """
    # OSError only, deliberately. A read failure must fail closed, and a programming error
    # must not. A broad `except Exception` here already hid a NameError once, and it reported
    # the bug as "every session stays latched", which looks like a policy state rather than a
    # defect.
    try:
        records, healthy = _read_records(store)
    except OSError as exc:
        logger.warning("gates: audit log unreadable, latches stay closed: {}", exc)
        return RestoredLatches(degraded=True)

    if not healthy:
        return RestoredLatches(degraded=True)

    latched: dict[tuple[str, str], float] = {}
    refusals: dict[tuple[str, str], int] = {}

    for record in records:
        key = _key(record)
        if key is None:
            continue
        decision = str(record.get("decision") or "")
        if decision == _DENIED:
            latched[key] = _timestamp(record)
            refusals.setdefault(key, 0)
        elif decision == _CLEARED:
            latched.pop(key, None)
            refusals.pop(key, None)
        elif decision == _REFUSED:
            refusals[key] = refusals.get(key, 0) + 1

    return RestoredLatches(latched=latched, refusals=refusals, degraded=False)


def _read_records(store: AuditStore) -> tuple[list[dict[str, Any]], bool]:
    """Parse every segment, and say whether the log is trustworthy.

    ``AuditStore.read_all`` skips a line it cannot parse, which is right for the store and
    wrong here. A latch that vanishes because a line was corrupt is a bypass, so this reads the
    segments itself and applies one tolerance rule.

    A failure on the final non-empty line of a segment is tolerated. #16 documents that fsync
    per record leaves only the last line able to tear, so that case is the known torn tail. A
    failure anywhere earlier means the log lost content it should hold, and the restore degrades.
    """
    records: list[dict[str, Any]] = []
    healthy = True

    for segment in store.segments():
        text = segment.read_text(encoding="utf-8")  # OSError degrades in the caller
        lines = [line for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    logger.warning("gates: torn tail in audit segment {}, ignored", segment.name)
                    continue
                logger.warning(
                    "gates: audit segment {} is corrupt at line {}, latches stay closed",
                    segment.name,
                    index + 1,
                )
                healthy = False
                continue
            if isinstance(parsed, dict):
                records.append(cast("dict[str, Any]", parsed))

    return records, healthy


def _key(record: dict[str, Any]) -> tuple[str, str] | None:
    """The (session, class) pair a record latches, or None when it latches nothing.

    A gate decision can carry no session id. It cannot latch a session either.
    """
    session_id = record.get("session_id")
    capability_class = record.get("capability_class")
    if not session_id or not capability_class:
        return None
    return (str(session_id), str(capability_class))


def _timestamp(record: dict[str, Any]) -> float:
    raw = record.get("ts")
    try:
        return float(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        # A record with no usable timestamp still latches. The pair matters, and the time is
        # only shown to an operator.
        return 0.0


__all__ = ["RestoredLatches", "restore_latches"]
