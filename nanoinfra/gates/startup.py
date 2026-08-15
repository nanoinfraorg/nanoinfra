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

The line carries the confinement of the helper processes as well (#20). A sandbox is a
control, so the checklist an operator reads must report it. A host without Landlock
support reads the absence just as plainly, because silence would read as a guarantee.

``identity_posture_line`` names the identity posture when an operator turned
``gates.identityIndependence`` on (#47, item 11). That flag trades a security property for a
workflow, so the deployment reads what it gave up at every start. It is a line of its own, and
it starts with the same ``identity:`` prefix as the trusted-proxy posture of #62, so one grep
answers what this gateway believes about a person. The default reads nothing, because a line
about every default teaches nobody to read one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.confinement import support_summary

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULTS = GatesConfig()

# What an operator reads when identity independence is on (#47, item 11). The sentence names the
# property the deployment gave up, in the words of the proposal, because the flag is the one gate
# setting that trades a security property for a workflow. A deployment that left it off reads
# nothing, since a line about every default teaches nobody to read one.
_IDENTITY_INDEPENDENCE_POSTURE = (
    "gates.identityIndependence is on, so a different person on the request origin path may "
    "approve. The origin identity is an assertion of the agent, so this deployment gives up the "
    "property that one compromised account cannot hold both halves"
)


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
    return f"gates: unattended {', '.join(parts)}, {grants}{origin}. {support_summary()}"


def identity_posture_line(gates: GatesConfig) -> str | None:
    """Name the identity posture of the gate, or None when the shipped default is in force.

    One posture, one line. #72 gives each posture a line of its own, because a posture that
    arrives as the tail of another line reads as a detail of that line.

    The line names a setting and a consequence, and it names no person. An approver list in a
    log is an address list in a log, and a log is shipped elsewhere often enough that this
    would be a leak nobody chose.
    """
    if not gates.identity_independence:
        return None
    return f"identity: {_IDENTITY_INDEPENDENCE_POSTURE}."


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


__all__ = ["identity_posture_line", "policy_summary", "refused_automation_warning"]
