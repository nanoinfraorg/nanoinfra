# tests/gates/test_grant_expiry.py
"""#218: a standing grant that expires, and a refusal that says so.

A grant born from a click would otherwise live forever, and a convenient path is exactly how
permanent permissions accumulate. Three properties carry the value here.

An expired grant is treated as absent, so the unattended allow path closes on the date the
operator chose. An expired grant is never *reported* as absent, because the state this feature
has to explain is a turn that ran unattended yesterday and waits for a human today. And a config
with no ``expiresAt`` behaves exactly as it did before this field existed, because every grant
written until now omits it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.policy import Outcome, evaluate, evaluate_connector

AUTOMATION = "automation"
INTERACTIVE = "interactive"
_COMMAND = "systemctl reload nginx"
_HOST = "staging-web-01"
_CONNECTOR = "google-calendar"
_OPERATION = "create_event"


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def _policy(*, decision: str = "grant", **grant_over: Any) -> GatesConfig:
    grant: dict[str, Any] = {
        "id": "reload-nginx",
        "contexts": ["unattended"],
        "hosts": [_HOST],
        "commands": [_COMMAND],
    }
    grant.update(grant_over)
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": decision, "group": decision}},
            "standingGrants": [grant],
        }
    )


def _decide(gates: GatesConfig, *, context: str = AUTOMATION):
    return evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=context,
        hosts=(_HOST,),
        command=_COMMAND,
    )


def test_a_grant_with_no_expiry_behaves_exactly_as_before() -> None:
    """Absent means never expires. Every config written before #218 omits the field."""
    decision = _decide(_policy())

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "reload-nginx"


def test_a_grant_whose_expiry_is_still_ahead_matches() -> None:
    decision = _decide(_policy(expiresAt=_iso(timedelta(hours=24))))

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "reload-nginx"


def test_an_expired_grant_does_not_match() -> None:
    decision = _decide(_policy(expiresAt=_iso(timedelta(minutes=-1))))

    assert decision.outcome is Outcome.DENY
    assert decision.grant_id is None


def test_the_refusal_says_expired_and_not_no_match() -> None:
    """The acceptance case. A bare "no grant names this" sends an operator to write it twice."""
    when = _iso(timedelta(minutes=-1))

    reason = _decide(_policy(expiresAt=when)).reason

    assert "reload-nginx" in reason
    assert "expired" in reason
    assert when[:10] in reason


def test_an_approve_decision_says_why_a_human_is_being_asked_today() -> None:
    """An `approve` cell plus an expired grant is the turn that suddenly waits for a person."""
    gates = GatesConfig.model_validate(
        {
            "interactive": {"mutate.remote": {"host": "approve"}},
            "standingGrants": [
                {
                    "id": "reload-nginx",
                    "contexts": ["interactive"],
                    "hosts": [_HOST],
                    "commands": [_COMMAND],
                    "expiresAt": _iso(timedelta(minutes=-1)),
                }
            ],
        }
    )

    decision = _decide(gates, context=INTERACTIVE)

    assert decision.outcome is Outcome.APPROVE
    assert "reload-nginx" in decision.reason
    assert "expired" in decision.reason


def test_a_live_grant_behind_an_expired_one_still_matches() -> None:
    """Two grants for one action: the scan must not stop at the expired row."""
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "stale",
                    "contexts": ["unattended"],
                    "hosts": [_HOST],
                    "commands": [_COMMAND],
                    "expiresAt": _iso(timedelta(minutes=-1)),
                },
                {
                    "id": "fresh",
                    "contexts": ["unattended"],
                    "hosts": [_HOST],
                    "commands": [_COMMAND],
                    "expiresAt": _iso(timedelta(days=7)),
                },
            ],
        }
    )

    decision = _decide(gates)

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "fresh"


def test_nothing_prunes_an_expired_grant() -> None:
    """The line stays in config. A gate that edited the operator's file would not be a gate."""
    gates = _policy(expiresAt=_iso(timedelta(minutes=-1)))

    _decide(gates)

    assert [grant.id for grant in gates.standing_grants] == ["reload-nginx"]


def test_a_timestamp_with_no_offset_reads_as_utc() -> None:
    """A naive value cannot be compared with an aware clock at all, and UTC is fail-closed."""
    gates = GatesConfig.model_validate(
        {"standingGrants": [{"hosts": [_HOST], "commands": [_COMMAND], "expiresAt": "2020-01-01"}]}
    )

    grant = gates.standing_grants[0]

    assert grant.expires_at is not None
    assert grant.expires_at.tzinfo is not None
    assert grant.is_expired(datetime.now(UTC)) is True


def test_an_expired_connector_grant_does_not_match_either() -> None:
    """Expiry is on the grant, not on the kind of action it names."""
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "calendar-hold",
                    "contexts": ["unattended"],
                    "connectors": [_CONNECTOR],
                    "operations": [_OPERATION],
                    "expiresAt": _iso(timedelta(minutes=-1)),
                }
            ],
        }
    )

    decision = evaluate_connector(
        gates,
        capability_class=MUTATE_REMOTE,
        execution_context=AUTOMATION,
        connector=_CONNECTOR,
        operation=_OPERATION,
    )

    assert decision.outcome is Outcome.DENY
    assert "calendar-hold" in decision.reason
    assert "expired" in decision.reason


def test_a_live_connector_grant_still_matches() -> None:
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "calendar-hold",
                    "contexts": ["unattended"],
                    "connectors": [_CONNECTOR],
                    "operations": [_OPERATION],
                    "expiresAt": _iso(timedelta(days=7)),
                }
            ],
        }
    )

    decision = evaluate_connector(
        gates,
        capability_class=MUTATE_REMOTE,
        execution_context=AUTOMATION,
        connector=_CONNECTOR,
        operation=_OPERATION,
    )

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "calendar-hold"
