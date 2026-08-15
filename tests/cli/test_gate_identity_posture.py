"""The identity posture the gate echoes at start -- nanoinfraorg/nanoinfra#72.

``gates.identityIndependence`` trades a security property for a workflow. So the deployment
reads what it gave up at every start, and it reads it on a line of its own. A sentence at the
end of the policy line reads as a detail of the policy. This is a posture, and #72 names each
posture on one line.

``tests/channels/test_trusted_proxy_jwt_admission.py`` holds the other four posture lines,
because the channel that reads the assertion is the one that echoes them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nanoinfra.cli.gateway_runtime import _echo_gate_policy
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.startup import identity_posture_line, policy_summary


class _Logger:
    """Every line the echo wrote, kept by the level the echo chose."""

    def __init__(self) -> None:
        self.info_lines: list[str] = []
        self.warning_lines: list[str] = []
        self.debug_lines: list[str] = []

    def info(self, template: str, *args: Any) -> None:
        self.info_lines.append(str(template).format(*args))

    def warning(self, template: str, *args: Any) -> None:
        self.warning_lines.append(str(template).format(*args))

    def debug(self, template: str, *args: Any) -> None:
        self.debug_lines.append(str(template).format(*args))


def _echo(monkeypatch: Any, gates: GatesConfig) -> _Logger:
    logger = _Logger()
    monkeypatch.setattr("nanoinfra.cli.gateway_runtime.logger", logger)
    _echo_gate_policy(SimpleNamespace(gates=gates), SimpleNamespace(list_jobs=lambda: []))
    assert logger.debug_lines == [], logger.debug_lines
    return logger


def test_identity_independence_reads_as_a_line_of_its_own(monkeypatch: Any) -> None:
    """The flag an operator turned on, named at the start that follows."""
    logger = _echo(monkeypatch, GatesConfig(identityIndependence=True))

    posture = [line for line in logger.warning_lines if line.startswith("identity:")]
    assert len(posture) == 1
    assert "gates.identityIndependence" in posture[0]
    # The property the deployment gave up, in the words of the proposal.
    assert "one compromised account cannot hold both halves" in posture[0]
    # And it is not also a tail of the policy line, because one fact on two lines reads as two.
    assert all("identityIndependence" not in line for line in logger.info_lines)


def test_the_default_posture_names_nothing(monkeypatch: Any) -> None:
    """A line about every default teaches nobody to read one."""
    logger = _echo(monkeypatch, GatesConfig())

    assert logger.warning_lines == []
    assert all("identityIndependence" not in line for line in logger.info_lines)
    assert identity_posture_line(GatesConfig()) is None


def test_the_posture_line_names_no_person(monkeypatch: Any) -> None:
    """A posture names a setting and a consequence. An approver list in a log is an address
    list in a log, and a log reaches more readers than the operator who ships it.
    """
    gates = GatesConfig.model_validate(
        {
            "identityIndependence": True,
            "approvers": [
                {"channel": "webui", "sender": "webui:alberto@example.com"},
                {"channel": "telegram", "sender": "123456789"},
            ],
        }
    )

    logger = _echo(monkeypatch, gates)

    for line in logger.info_lines + logger.warning_lines:
        assert "alberto@example.com" not in line
        assert "123456789" not in line


def test_the_policy_line_stays_about_the_policy() -> None:
    """The gate policy line keeps every fact it carried, and drops the posture sentence."""
    line = policy_summary(GatesConfig(identityIndependence=True))

    assert "gates: unattended" in line
    assert "identityIndependence" not in line
