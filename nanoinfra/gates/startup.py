"""State the gate policy in force at start -- nanoinfraorg/nanoinfra#8.

An operator can mistype the top-level ``gates`` key. ``Config`` accepts extras today, and
tightening that would break live configs, so a mistyped block loads as silent defaults. The
operator then believes a permissive policy is live while the restrictive default runs.

One line at start closes that gap. The operator reads the policy in force rather than the
policy they think they wrote, and they read it before the next scheduled run rather than
after it fails.

The summary also names a default as a default. An operator who sees ``deny`` cannot
otherwise tell a shipped default from their own decision, and reviewing your own policy
needs that difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanoinfra.config.gates import GatesConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULTS = GatesConfig()


def _unattended_is_default(gates: GatesConfig) -> bool:
    return gates.unattended == _DEFAULTS.unattended


def policy_summary(gates: GatesConfig) -> str:
    """One line naming the unattended decision for each gated class.

    Unattended policy comes first because it is the half #8 enforces. An empty grant list is
    reported as a refusal rather than as an absence: no grants means no automation acts, and
    an operator must not read that state as "no rules yet".
    """
    remote = gates.unattended.mutate_remote
    parts = [
        f"mutate.remote host={remote.host} group={remote.group} all={remote.all}",
        f"mutate.inventory={gates.unattended.mutate_inventory}",
        f"credential.access={gates.unattended.credential_access}",
    ]
    count = len(gates.standing_grants)
    if count == 0:
        grants = "no standing grants, so no automation may run a remote command"
    else:
        grants = f"{count} standing grant" + ("s" if count != 1 else "")
    origin = " (shipped defaults, no gates policy in config)" if _unattended_is_default(gates) else ""
    return f"gates: unattended {', '.join(parts)}, {grants}{origin}"


def refused_automation_warning(
    gates: GatesConfig, *, automation_names: Sequence[str]
) -> str | None:
    """Name the automations this policy refuses, or None when there is nothing to warn about.

    The warning targets one specific event: an upgrade that silences every automation at
    once. A deployment that declares grants may still be wrong, and this cannot tell. A
    deployment with automations and no grants is certainly broken, and it can.
    """
    if not automation_names:
        return None
    if gates.standing_grants:
        return None
    names = ", ".join(automation_names)
    return (
        f"gates: {len(automation_names)} automation(s) will be refused for remote actions: "
        f"{names}. Declare gates.standingGrants for the commands they run, or they stay "
        "refused."
    )


__all__ = ["policy_summary", "refused_automation_warning"]
