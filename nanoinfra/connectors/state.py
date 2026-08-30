"""What one connector has actually done, recorded so a row can say it.

The Apps row has to answer three questions config cannot: who does this connector act as,
when did its token last refresh, and has it ever worked. All three are runtime facts, and all
three are produced in the executor -- the only process that mints a token or makes a call --
so they are written there and read by the WebUI.

Small on purpose. This is not a store: it is one JSON file of recorded facts, written with the
same atomic replace the rest of the codebase uses, and losing it costs a row three lines until
the next refresh rather than costing a deployment anything.

No secret ever lands here. ``acts_as`` is an address a person recognises, and the token itself
stays in the executor's memory for the minutes it lives.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from loguru import logger

STATE_DIR_NAME = "connectors"
STATE_FILE_NAME = "state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class ConnectorState:
    """The recorded facts for one connector.

    Every field is optional, because a connector that was activated one minute ago has none of
    them and the row has to say exactly that rather than imply a failure.
    """

    #: The account the connector acts as, learned from a call rather than from config: the
    #: consent may name an account the operator did not pass on the command line.
    acts_as: str = ""
    #: When the executor last minted an access token. A token that stopped refreshing is the
    #: failure this makes visible.
    refreshed_at: str = ""
    #: When a read last succeeded, and what it returned in one line.
    tested_at: str = ""
    test_summary: str = ""
    #: The last thing that went wrong, in the words the operator would otherwise find in a log.
    last_error: str = ""
    last_error_at: str = ""
    #: The mentionable objects last listed, as ``[{kind, id, name, detail}]`` JSON.
    #:
    #: Cached because a mention is resolved on every send, and a live listing there would put a
    #: network call on the path of every message and fail a send when an API is slow. The picker
    #: refreshes it; resolution reads it; an id that is not in it is refused rather than passed
    #: through, which is the same posture the server and diagram kinds have against their stores.
    objects: str = ""
    objects_at: str = ""

    def merged(self, **fields: Any) -> ConnectorState:
        """A copy with these fields replaced. Empty values do not erase a recorded fact."""
        current = asdict(self)
        for key, value in fields.items():
            if value not in (None, ""):
                current[key] = value
        return ConnectorState(**current)


def state_path(workspace: Path) -> Path:
    return Path(workspace) / STATE_DIR_NAME / STATE_FILE_NAME


def read_all(workspace: Path) -> dict[str, ConnectorState]:
    """Every recorded connector state, or an empty map.

    A missing, unreadable or malformed file is an absence rather than an error: this is a
    record of what happened, and a payload that failed because of it would take the whole Apps
    page down for a cosmetic field.
    """
    path = state_path(workspace)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("connector state at {} is unreadable: {}", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}

    found: dict[str, ConnectorState] = {}
    known = set(ConnectorState.__dataclass_fields__)
    for name, entry in cast(dict[str, Any], raw).items():
        if not isinstance(entry, dict):
            continue
        fields = {
            key: value
            for key, value in cast(dict[str, Any], entry).items()
            if key in known and isinstance(value, str)
        }
        found[str(name)] = ConnectorState(**fields)
    return found


def read_one(workspace: Path, connector: str) -> ConnectorState:
    return read_all(workspace).get(connector, ConnectorState())


def record(workspace: Path, connector: str, **fields: Any) -> ConnectorState:
    """Merge these facts into one connector's record and write the file.

    Read-modify-write with an atomic replace. Two executor threads can answer two connector
    calls at once, so the replace is what keeps a half-written file off the disk; a lost update
    between two of them costs one timestamp, which is the right price for not holding a lock
    across a filesystem write in the process that must stay responsive.
    """
    path = state_path(workspace)
    current = read_all(workspace)
    updated = current.get(connector, ConnectorState()).merged(**fields)
    current[connector] = updated
    payload = {name: asdict(state) for name, state in current.items()}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp = Path(handle.name)
        temp.replace(path)
    except OSError as exc:
        # A record that will not write must not fail the action it describes: the call already
        # happened, and the row losing a timestamp is not worth a refusal a person cannot act on.
        logger.warning("connector state for {} did not write: {}", connector, exc)
    return updated


__all__ = [
    "STATE_DIR_NAME",
    "STATE_FILE_NAME",
    "ConnectorState",
    "now_iso",
    "read_all",
    "read_one",
    "record",
    "state_path",
]
