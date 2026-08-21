"""Whether an automation's outcome reaches the operator is a field, not a request in the prompt.

A real job asked for it in prose -- "If nothing new, stay silent or say briefly that there are no
new blockers" -- which is a hope, because the turn deciding whether to stay quiet is the same turn
that wants to report (nanoinfraorg/nanoinfra#159).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.automations.delivery import (
    DEFAULT_DELIVERY_POLICY,
    DELIVERY_POLICIES,
    normalize_policy,
    should_deliver,
)
from nanoinfra.automations.state import (
    AutomationDeliveryLog,
    AutomationStateStore,
    response_fingerprint,
)


def _decide(policy: str, content: str, *, failed: bool = False, last: str | None = None) -> bool:
    return should_deliver(
        policy,  # type: ignore[arg-type]
        content=content,
        failed=failed,
        last_fingerprint=last,
        fingerprint=response_fingerprint(content),
    )


# --- the decision ---


def test_always_delivers_anything_with_content() -> None:
    assert _decide("always", "3 new blockers") is True


def test_never_delivers_nothing_even_a_failure() -> None:
    """An operator who asked for silence gets silence. That is what the word means."""
    assert _decide("never", "3 new blockers") is False
    assert _decide("never", "3 new blockers", failed=True) is False


def test_on_error_stays_quiet_on_success() -> None:
    assert _decide("on-error", "nothing new") is False


@pytest.mark.parametrize("policy", ["always", "on-change", "on-error"])
def test_a_failure_is_delivered_whatever_the_policy(policy: str) -> None:
    """A policy chosen to reduce noise was not chosen to hide a failure."""
    assert _decide(policy, "host unreachable", failed=True) is True


def test_on_change_delivers_the_first_time() -> None:
    assert _decide("on-change", "2 blockers", last=None) is True


def test_on_change_stays_quiet_when_nothing_changed() -> None:
    content = "2 blockers: #47, #51"
    assert _decide("on-change", content, last=response_fingerprint(content)) is False


def test_on_change_delivers_when_the_answer_changed() -> None:
    assert _decide("on-change", "3 blockers", last=response_fingerprint("2 blockers")) is True


def test_on_change_ignores_reflowed_whitespace() -> None:
    """A model that reflows the same answer has not changed it, and an on-change policy that
    fires on reflow is one an operator switches off."""
    first = "2 blockers:\n  #47\n  #51"
    reflowed = "2 blockers: #47 #51"
    assert _decide("on-change", reflowed, last=response_fingerprint(first)) is False


@pytest.mark.parametrize("policy", ["always", "on-change"])
def test_an_empty_success_is_never_delivered(policy: str) -> None:
    """Delivering a blank message is noise with no content at all."""
    assert _decide(policy, "") is False
    assert _decide(policy, "   \n ") is False


# --- normalisation ---


def test_the_default_is_todays_behaviour() -> None:
    assert DEFAULT_DELIVERY_POLICY == "always"
    assert normalize_policy(None) == "always"


@pytest.mark.parametrize("policy", DELIVERY_POLICIES)
def test_every_policy_round_trips(policy: str) -> None:
    assert normalize_policy(policy) == policy


def test_case_and_padding_are_tolerated() -> None:
    assert normalize_policy("  On-Change ") == "on-change"


@pytest.mark.parametrize("value", ["on_change", "quiet", "", 7, object()])
def test_an_unknown_value_reads_as_always(value: object) -> None:
    """Being noisy about a typo is recoverable. Being silent is not."""
    assert normalize_policy(value) == "always"


# --- the delivery log ---


def test_the_log_remembers_the_last_delivered_answer(tmp_path: Path) -> None:
    log = AutomationDeliveryLog(tmp_path)
    assert log.last_fingerprint("job-a") is None

    log.record("job-a", response_fingerprint("2 blockers"))

    assert log.last_fingerprint("job-a") == response_fingerprint("2 blockers")


def test_the_log_is_per_automation(tmp_path: Path) -> None:
    log = AutomationDeliveryLog(tmp_path)
    log.record("job-a", "aaa")
    log.record("job-b", "bbb")

    assert log.last_fingerprint("job-a") == "aaa"
    assert log.last_fingerprint("job-b") == "bbb"


def test_the_log_survives_a_new_instance(tmp_path: Path) -> None:
    AutomationDeliveryLog(tmp_path).record("job-a", "aaa")

    assert AutomationDeliveryLog(tmp_path).last_fingerprint("job-a") == "aaa"


def test_a_corrupted_log_reads_as_unknown(tmp_path: Path) -> None:
    """Unknown means "deliver", which is the safe direction for a broken file."""
    log = AutomationDeliveryLog(tmp_path)
    log.record("job-a", "aaa")
    log._path("job-a").write_text("{not json", encoding="utf-8")

    assert log.last_fingerprint("job-a") is None


def test_forget_clears_the_log(tmp_path: Path) -> None:
    log = AutomationDeliveryLog(tmp_path)
    log.record("job-a", "aaa")

    assert log.forget("job-a") is True
    assert log.last_fingerprint("job-a") is None
    assert log.forget("job-a") is False


def test_the_model_cannot_reach_the_delivery_log(tmp_path: Path) -> None:
    """An automation that can edit the record of what it last said can talk itself past an
    on-change policy, so the two stores are separate documents and the tool has no method that
    reaches this one."""
    state = AutomationStateStore(tmp_path)
    log = AutomationDeliveryLog(tmp_path)
    log.record("job-a", "aaa")

    state.set("job-a", "anything", "value")

    assert log.last_fingerprint("job-a") == "aaa"
    assert state.root != log.root
    # And clearing the state an operator can see leaves the delivery bookkeeping alone.
    state.clear("job-a")
    assert log.last_fingerprint("job-a") == "aaa"
