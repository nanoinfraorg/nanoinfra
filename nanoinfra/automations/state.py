"""State an automation keeps between its own runs.

Before this, an automation that needed to remember something across runs had to hand-roll it in
prose. A real job carried::

    keep a small state file (e.g. $HOME/workspace/.cron_state/nanoinfra_blockers.json) listing
    previously reported issue numbers, and skip those

The operator picked the path, the format and the semantics, and every run depended on the model
reading and rewriting that file correctly. Nothing could inspect it, nothing could reset it, and a
second automation writing the same path would silently share it.

Two properties this module exists to hold:

- **Scope is not a parameter.** The id comes from the running turn, never from an argument, so one
  automation cannot read or clear another's state. See
  :func:`nanoinfra.session.automation_turns.automation_identity`.
- **A bound is a bound.** The agent writes here on every run, unattended, so an unbounded store is
  a way to fill a disk slowly. Writes that would exceed the cap are refused, not truncated: a
  caller that learns its write was refused can adapt, and one handed silently altered state
  cannot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from filelock import FileLock
from loguru import logger

#: Generous for a dedup list, small enough that a runaway loop hits it the same day.
MAX_STATE_BYTES = 64 * 1024
#: A single value that big is a payload, not state. Caught before the whole-document check so the
#: error names the offender.
MAX_VALUE_BYTES = 16 * 1024
MAX_KEYS = 512
MAX_KEY_LENGTH = 128

#: Ids come from cron (uuid4 slice) and triggers (``tdl_``-style hex), so this is permissive by
#: design and exists to keep a path traversal out of a filename, not to validate an id format.
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


class AutomationStateError(RuntimeError):
    """Base class for state store errors."""


class AutomationStateTooLargeError(AutomationStateError):
    """A write would push the automation's state past its cap."""


class AutomationStateStore:
    """One JSON document per automation, under the workspace."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "automations" / "state"
        self._lock = FileLock(str(self.workspace_path / "automations" / ".state.lock"))

    # --- reads ---

    def snapshot(self, automation_id: str) -> dict[str, Any]:
        """Return the whole document, or an empty one."""
        path = self._path(automation_id)
        if not path.exists():
            return {}
        try:
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # A corrupted document reads as empty rather than raising. The alternative is an
            # automation that can never run again because one truncated write poisoned its state,
            # and the operator can see the file.
            logger.exception("Automation state: unreadable document for {}", automation_id)
            return {}
        if not isinstance(raw, dict):
            return {}
        values = cast(object, cast(dict[str, object], raw).get("values"))
        if not isinstance(values, dict):
            return {}
        return {str(key): value for key, value in cast(dict[object, Any], values).items()}

    def get(self, automation_id: str, key: str) -> Any:
        return self.snapshot(automation_id).get(_clean_key(key))

    # --- writes ---

    def set(self, automation_id: str, key: str, value: Any) -> dict[str, Any]:
        """Store one value and return the resulting document."""
        clean = _clean_key(key)
        encoded = _encode_value(value)
        if len(encoded) > MAX_VALUE_BYTES:
            raise AutomationStateTooLargeError(
                f"value for '{clean}' is {len(encoded)} bytes, over the "
                f"{MAX_VALUE_BYTES}-byte limit for one key"
            )
        with self._lock:
            values = self.snapshot(automation_id)
            if clean not in values and len(values) >= MAX_KEYS:
                raise AutomationStateTooLargeError(
                    f"automation state already holds {MAX_KEYS} keys"
                )
            values[clean] = json.loads(encoded)
            self._write_unlocked(automation_id, values)
            return values

    def delete(self, automation_id: str, key: str) -> bool:
        clean = _clean_key(key)
        with self._lock:
            values = self.snapshot(automation_id)
            if clean not in values:
                return False
            del values[clean]
            self._write_unlocked(automation_id, values)
            return True

    def clear(self, automation_id: str) -> bool:
        """Forget everything this automation knows. The reset an operator could not do before."""
        with self._lock:
            path = self._path(automation_id)
            if not path.exists():
                return False
            path.unlink(missing_ok=True)
            return True

    # --- internals ---

    def _write_unlocked(self, automation_id: str, values: dict[str, Any]) -> None:
        document = json.dumps({"version": 1, "values": values}, ensure_ascii=False)
        if len(document.encode("utf-8")) > MAX_STATE_BYTES:
            raise AutomationStateTooLargeError(
                f"automation state would be over the {MAX_STATE_BYTES}-byte limit"
            )
        path = self._path(automation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(document, encoding="utf-8")
        tmp.replace(path)

    def _path(self, automation_id: str) -> Path:
        return self.root / f"{_safe_id(automation_id)}.json"


def _safe_id(automation_id: str) -> str:
    value = (automation_id or "").strip()
    if not value:
        raise AutomationStateError("automation id is required")
    return _SAFE_ID.sub("_", value)[:128]


def _clean_key(key: str) -> str:
    value = (key or "").strip()
    if not value:
        raise AutomationStateError("state key is required")
    if len(value) > MAX_KEY_LENGTH:
        raise AutomationStateError(f"state key is longer than {MAX_KEY_LENGTH} characters")
    return value


def _encode_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise AutomationStateError(f"state value is not JSON-serialisable: {exc}") from exc
