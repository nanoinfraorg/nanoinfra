# tests/gates/test_startup_echo.py
"""Item 6 (#8), second half: state the policy in force at start.

A mistyped top-level `gates` key loads as silent defaults, because `Config` accepts extras
and tightening that would break live configs. An operator who wrote a permissive block must
therefore learn the truth at start, not at the next scheduled run.
"""

from __future__ import annotations

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.startup import policy_summary, refused_automation_warning


def test_the_summary_states_the_unattended_decision_for_each_class() -> None:
    summary = policy_summary(GatesConfig())

    assert "mutate.remote" in summary
    assert "mutate.inventory" in summary
    assert "credential.access" in summary


def test_the_summary_marks_a_default_policy_as_a_default() -> None:
    """An operator who sees `deny` cannot tell a shipped default from their own choice."""
    assert "default" in policy_summary(GatesConfig()).lower()


def test_the_summary_does_not_claim_a_default_when_policy_was_written() -> None:
    written = GatesConfig.model_validate({"unattended": {"mutate.remote": {"host": "grant"}}})

    assert "default" not in policy_summary(written).lower()


def test_the_summary_reports_the_grant_count() -> None:
    written = GatesConfig.model_validate(
        {"standingGrants": [{"hosts": ["web-01"], "commands": ["uptime"]}]}
    )

    assert "1 standing grant" in policy_summary(written)


def test_no_grants_reads_as_a_refusal_rather_than_as_an_absence() -> None:
    """An empty grant list is the policy, not a missing policy. It means no automation acts."""
    summary = policy_summary(GatesConfig())

    assert "no automation" in summary.lower()


def test_a_named_automation_is_reported_when_no_grant_covers_it() -> None:
    warning = refused_automation_warning(GatesConfig(), automation_names=["nightly-backup"])

    assert warning is not None
    assert "nightly-backup" in warning


def test_no_warning_when_no_automation_exists() -> None:
    assert refused_automation_warning(GatesConfig(), automation_names=[]) is None


def test_no_warning_when_the_operator_declared_grants() -> None:
    """A deployment with grants may still be wrong, but it is not obviously broken. The
    warning exists to catch the upgrade that silences every automation at once."""
    written = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [{"hosts": ["web-01"], "commands": ["uptime"]}],
        }
    )

    assert refused_automation_warning(written, automation_names=["nightly-backup"]) is None
