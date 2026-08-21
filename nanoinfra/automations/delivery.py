"""Whether an automation's outcome reaches the operator.

This used to be asked of the model inside the prompt. A real job carried::

    If nothing new, stay silent or say briefly that there are no new blockers.

Which is a hope, not a policy: the turn that decides whether to stay quiet is the same turn that
wants to report. The whole value of moving it here is that the model cannot opt out -- so nothing
in this module reads anything the model wrote, and the ``automation_state`` tool has no method
that reaches the delivery log.
"""

from __future__ import annotations

from typing import Literal, get_args

DeliveryPolicy = Literal["always", "on-change", "on-error", "never"]

DELIVERY_POLICIES: tuple[str, ...] = get_args(DeliveryPolicy)

#: What an automation created before this existed gets, and what it keeps until asked otherwise.
DEFAULT_DELIVERY_POLICY: DeliveryPolicy = "always"


def normalize_policy(value: object) -> DeliveryPolicy:
    """Coerce stored or submitted input to a policy, defaulting to today's behaviour.

    An unknown value reads as ``always`` rather than raising. A typo in a config file should not
    silence an automation -- being noisy about a mistake is recoverable, being silent is not.
    """
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in DELIVERY_POLICIES:
            return candidate  # pyright: ignore[reportReturnType]
    return DEFAULT_DELIVERY_POLICY


def should_deliver(
    policy: DeliveryPolicy,
    *,
    content: str,
    failed: bool,
    last_fingerprint: str | None,
    fingerprint: str,
) -> bool:
    """Decide whether this run's outcome is delivered.

    ``failed`` wins over every policy except ``never``. An automation that broke is the one thing
    an operator has to hear about, and a policy chosen to reduce noise was not chosen to hide a
    failure.
    """
    if policy == "never":
        return False
    if failed:
        return True
    if policy == "on-error":
        return False
    if not content.strip():
        # An empty success is nothing to say. Every policy agrees, including "always": delivering
        # a blank message is noise with no content at all.
        return False
    if policy == "on-change":
        return fingerprint != last_fingerprint
    return True
