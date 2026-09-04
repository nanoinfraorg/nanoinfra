"""The agent an automation runs as, and why that agent is a ceiling (#257).

An automation used to run an unattended turn with the deployment's default agent and a list of
skills of its own. Naming an agent instead answers a question the record could not: *the
`apt-package-check-daily` job wants one host and `execute_on_server`, and an agent scoped to
exactly that is a smaller blast radius than the default agent running the same prompt.*

The rule that makes it worth having, stated once so it can be tested:

    **The agent sets the ceiling and the job may only narrow it.**

A job that names an agent and then asks for something the agent does not have is refused. The
alternative -- widening -- would make naming an agent the way to escape its contract, which is the
authority laundering the delegation design already refuses, wearing a different costume.

Nothing here re-implements what a delegated turn already does. ``DelegateBinding`` is the answer
to "who is acting and with what", and ``tools_for_groups`` is the answer to "which tools survive
the groups it declared". Both come from ``nanoinfra/agent/delegation.py``, so an automation's
agent and a delegated agent are capped by one mechanism rather than two that can disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.delegation import DelegateBinding

if TYPE_CHECKING:
    from nanoinfra.config.schema import NamedAgentConfig

#: The empty roster: what every deployment that configures no named agent has.
_NO_AGENTS: "Mapping[str, NamedAgentConfig]" = {}


def roster_from_config() -> "Mapping[str, NamedAgentConfig]":
    """The configured agents, read fresh.

    A fresh read rather than a captured one, for the reason ``allowed_delegates`` gives: the
    roster is authority, so an agent removed from config stops being nameable on the next save
    and the next run -- not on the next restart.

    A config that cannot be loaded yields no agents, which refuses every job that names one. That
    is the safe direction: the alternative would be running a narrowed job with the default
    agent's whole tool set because a file failed to parse.
    """
    try:
        from nanoinfra.config.loader import load_config

        return load_config().agents.named
    except Exception:
        return _NO_AGENTS


def job_agent_refusal(
    *,
    agent: str,
    skills: Sequence[str] = (),
    roster: "Mapping[str, NamedAgentConfig]",
) -> str | None:
    """Why this job may not run as *agent*, or ``None`` when it may.

    A reason rather than an exception, because both callers want the text: the save path returns
    it to the operator who typed it, and the run path records it as the run's error. Every reason
    names the agent, because "unknown agent" in a log at 03:00 is not a reason anybody can act
    on.

    A job that names nothing is never refused. That is the deployment's default agent, which is
    what every job is today, so this function has to be a no-op on the whole existing store.
    """
    name = (agent or "").strip()
    if not name:
        return None
    entry = roster.get(name)
    if entry is None:
        known = ", ".join(sorted(roster)) or "none"
        return (
            f"{name!r} is not a configured agent. Configured agents: {known}. "
            "Agents are declared in `agents.named` in config, because which agents exist and "
            "what each may reach is authority and lives in a file a human reviews."
        )
    allowed = list(entry.skills or [])
    if not allowed:
        # The agent declared no skills, so it summarises the whole catalogue and the job's own
        # picker is the narrowing it always was. Nothing to check.
        return None
    outside = [skill for skill in skills if skill not in allowed]
    if outside:
        return (
            f"{name!r} does not have the skill {outside[0]!r}. A job may only narrow the skills "
            f"its agent carries ({', '.join(allowed)}), never add to them -- otherwise naming an "
            "agent would be the way around its configuration."
        )
    return None


def enforce_agent_binding(
    *,
    agent: str,
    skills: Sequence[str] = (),
    roster: "Mapping[str, NamedAgentConfig] | None",
) -> None:
    """Raise ``ValueError`` with the reason this automation may not name *agent*.

    The write-side half of :func:`job_agent_refusal`, shared by the cron store and the trigger
    store so "which roster, and what happens when it says no" has one answer rather than one per
    automation kind.

    ``roster`` of ``None`` means read config now. A blank name returns before that read, so a
    store full of automations naming nothing never touches config at all.
    """
    if not (agent or "").strip():
        return
    reason = job_agent_refusal(
        agent=agent,
        skills=skills,
        roster=roster if roster is not None else roster_from_config(),
    )
    if reason:
        raise ValueError(reason)


def agent_addendum_prefix(binding: DelegateBinding | None) -> str:
    """The acting agent's own instructions, in front of the automation's.

    Appended to the platform's text and never substituted for it, which is the rule a delegated
    turn follows too: an addendum specialises an agent, and an agent that could replace the prompt
    could drop the tool contract and the safety notes with it. Those live in the system prompt the
    turn already carries, so this sits in front of the automation's own instruction -- identity
    first, then the task.

    In front also matters when the task is not ours. A trigger's message comes from whatever fired
    it, so the trusted text has to precede the untrusted text rather than follow it.
    """
    if binding is None or not binding.addendum.strip():
        return ""
    return (
        f"You are `{binding.name}`, the agent this automation runs as.\n\n"
        f"{binding.addendum.strip()}\n\n---\n\n"
    )


def parse_automation_agent(values: "Mapping[str, Any]") -> dict[str, str] | str:
    """The ``agent`` field of an automation update, or the reason it is not one.

    Shaped like the other field parsers in the WebUI automations route -- a dict of what to
    update, or a string that becomes the 400 -- and kept here rather than there so the field's
    whole contract is in one file: the name, the record, the ceiling and the refusal.

    Only the *shape* is checked. Whether the deployment has that agent is
    :func:`job_agent_refusal`'s answer, at the moment the job is written, against config.
    """
    if "agent" not in values:
        return {}
    raw = values.get("agent")
    if not isinstance(raw, str):
        return "agent must be a string"
    # An empty string clears the choice, which is the deployment's default agent. So the field is
    # removable from a job that named one, without a second verb for "unset".
    return {"agent": raw.strip()}


def job_agent_binding(
    *,
    agent: str,
    skills: Sequence[str] = (),
    roster: "Mapping[str, NamedAgentConfig]",
    asked_by: str,
) -> DelegateBinding | None:
    """What the turn runs with, or ``None`` when the job names no agent.

    Raises ``ValueError`` with the reason from :func:`job_agent_refusal` when the job names an
    agent it may not have. Callers translate that into the refusal their surface owns: a 400 on
    save, a terminal run error on execution.

    ``actor`` stays ``None`` and ``delegated_by`` names the automation, which is exactly true: no
    person is waiting on this turn, so a standing grant is the only thing that can authorise what
    it does. This binding is *not* written into ``RequestContext.delegated_by`` -- a job that
    names a coordinator agent must still be able to delegate, and that field is what refuses a
    second level.
    """
    name = (agent or "").strip()
    if not name:
        return None
    reason = job_agent_refusal(agent=name, skills=skills, roster=roster)
    if reason:
        raise ValueError(reason)
    entry = roster[name]
    return DelegateBinding(
        name=name,
        delegated_by=asked_by,
        # `None` means the agent declared no ceiling, and a binding says that with an empty
        # tuple plus `declared_tool_groups=False`. Collapsing them would turn "unrestricted" into
        # "no grouped tools at all" for every job whose agent narrows nothing.
        tool_groups=tuple(entry.tool_groups or ()),
        declared_tool_groups=entry.tool_groups is not None,
        # The job's own picker narrows; an empty picker means the agent's whole list. Both are
        # already inside the ceiling, because the refusal above is what got us here.
        skills=tuple(skills) if skills else tuple(entry.skills or ()),
        addendum=entry.addendum,
    )
