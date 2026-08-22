"""What a commissioning run left on the automation record -- #189, #190.

A commissioning run answers a question about one automation, so its answer belongs on that
automation and not in a log an operator has to correlate. Two things live here:

- the **finding**: what a scheduled run would meet, in the words an operator reads, plus the
  grants that would permit it. A refused automation is saved disabled with this attached, rather
  than saved enabled and certain to refuse at 03:00, and rather than not saved at all -- the
  authoring work survives either way, and only one of the three tells the truth on the schedule.
- the **fingerprint**: what the run was about. Re-commissioning costs a model turn, so it happens
  when the message, the references or the declared skills change and not when a schedule moves or
  a name is edited. Those three decide which commands the automation will run; the others do not.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

#: Never commissioned. The state an automation created before this existed also reads as, which
#: is why it is the default rather than an error: an upgrade must not report every existing
#: automation as refused.
UNCHECKED = "unchecked"
#: Every previewed action would be permitted on the schedule.
OK = "ok"
#: At least one action would be refused. The automation is disabled and the finding says why.
REFUSED = "refused"
#: The rehearsal itself could not complete. Not the same as refused: nothing was learned.
ERROR = "error"

_STATUSES = frozenset({UNCHECKED, OK, REFUSED, ERROR})

#: A finding is operator-facing text, and text from a preview can be long -- a group preview names
#: every host. Bounded, because this is persisted on every automation record.
MAX_FINDING_CHARS = 4_000
MAX_PROPOSED_GRANTS = 16


@dataclass(frozen=True, slots=True)
class CommissioningState:
    """The commissioning verdict carried on one automation record."""

    status: str = UNCHECKED
    checked_at_ms: int | None = None
    finding: str = ""
    #: What the verdict was about, so a later save can tell whether it still applies.
    fingerprint: str = ""
    #: Grants that would permit the refused actions, as `gates.standingGrants` takes them.
    proposed_grants: tuple[dict[str, Any], ...] = ()

    @property
    def blocks_the_schedule(self) -> bool:
        return self.status == REFUSED

    def applies_to(self, fingerprint: str) -> bool:
        """Whether this verdict was reached about the automation as it stands now."""
        return bool(self.fingerprint) and self.fingerprint == fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checkedAtMs": self.checked_at_ms,
            "finding": self.finding,
            "fingerprint": self.fingerprint,
            "proposedGrants": [dict(grant) for grant in self.proposed_grants],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CommissioningState:
        """Read a stored verdict, and fall back to unchecked for anything malformed.

        A record written by an older version carries nothing here, and a hand-edited one can
        carry anything. Neither may read as `ok`: a verdict that cannot be trusted has to read as
        one that was never reached.
        """
        if not isinstance(raw, Mapping):
            return cls()
        data: dict[str, Any] = {str(key): value for key, value in cast(
            "Mapping[Any, Any]", raw
        ).items()}
        status = str(data.get("status") or UNCHECKED)
        if status not in _STATUSES:
            status = UNCHECKED
        checked: Any = data.get("checkedAtMs", data.get("checked_at_ms"))
        grants_raw: Any = data.get("proposedGrants", data.get("proposed_grants")) or []
        grants: list[dict[str, Any]] = []
        if isinstance(grants_raw, Sequence) and not isinstance(grants_raw, (str, bytes)):
            entries: list[Any] = list(cast("Sequence[Any]", grants_raw))
            for entry in entries[:MAX_PROPOSED_GRANTS]:
                if isinstance(entry, Mapping):
                    grants.append({
                        str(key): value
                        for key, value in cast("Mapping[Any, Any]", entry).items()
                    })
        return cls(
            status=status,
            checked_at_ms=int(checked) if isinstance(checked, (int, float)) else None,
            finding=str(data.get("finding") or "")[:MAX_FINDING_CHARS],
            fingerprint=str(data.get("fingerprint") or ""),
            proposed_grants=tuple(grants),
        )


def commissioning_fingerprint(
    *,
    message: str,
    references: Sequence[Mapping[str, str]] = (),
    skills: Sequence[str] = (),
) -> str:
    """Identify an automation by what decides which commands it will run.

    Deliberately not the whole record. A renamed automation, or one whose schedule moved from
    hourly to nightly, runs the same commands, and paying for a model turn to learn that again
    teaches an operator to dread saving.
    """
    payload = json.dumps(
        {
            "message": message.strip(),
            "references": sorted(
                (str(item.get("kind", "")), str(item.get("id", ""))) for item in references
            ),
            "skills": sorted(str(skill) for skill in skills),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ERROR",
    "MAX_FINDING_CHARS",
    "MAX_PROPOSED_GRANTS",
    "OK",
    "REFUSED",
    "UNCHECKED",
    "CommissioningState",
    "commissioning_fingerprint",
]
