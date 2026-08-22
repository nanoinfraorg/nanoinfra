# tests/webui/test_commissioning_promotion.py
"""Promoting a commissioning finding into a standing grant -- #186, #187, #188.

The route writes a permission, so what it refuses matters more than what it writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.automations.commissioning_state import OK, REFUSED, CommissioningState
from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronSchedule
from nanoinfra.webui.commissioning_api import (
    CommissioningOperatorSurface,
    PromotionRefusedError,
)


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    config = home / ".nanoinfra" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"gates": {}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", config)
    return config


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **fields: Any) -> dict[str, Any]:
        self.records.append(fields)
        return fields


def _service_with_job(tmp_path: Path, state: CommissioningState) -> tuple[CronService, Any]:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service._running = True
    job = service.add_job(
        name="uptime watch",
        schedule=CronSchedule(kind="every", every_ms=300_000),
        message="Report the uptime",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
    )
    service.set_commissioning(job.id, state, disable=state.status == REFUSED)
    return service, service.get_job(job.id)


def _refused_state(**over: Any) -> CommissioningState:
    fields: dict[str, Any] = {
        "status": REFUSED,
        "finding": "execute_on_server: would be refused.",
        "fingerprint": "abc",
        "proposed_grants": (
            {
                "id": "uptime-watch",
                "contexts": ["unattended"],
                "hosts": ["10.0.0.9"],
                "commands": ["uptime"],
            },
        ),
    }
    fields.update(over)
    return CommissioningState(**fields)


def _surface(service: CronService, audit: _Audit | None = None) -> CommissioningOperatorSurface:
    return CommissioningOperatorSurface(cron_service=service, audit=audit)


async def test_promoting_writes_the_grant_and_says_what_it_covers(tmp_path: Path, _isolated_config: Path) -> None:
    service, job = _service_with_job(tmp_path, _refused_state())
    audit = _Audit()

    result = _surface(service, audit).promote(job.id, actor="webui:alberto", origin_path="webui")

    assert result["granted"] == [
        {
            "id": "uptime-watch",
            "contexts": ["unattended"],
            "hosts": ["10.0.0.9"],
            "commands": ["uptime"],
        }
    ]
    # The sentence that keeps an operator from believing the grant is scoped to one automation.
    assert "any unattended turn" in result["note"]
    assert result["requires_restart"] is True

    from nanoinfra.config.loader import load_config

    [grant] = load_config().gates.standing_grants
    assert grant.hosts == ["10.0.0.9"]
    assert grant.commands == ["uptime"]
    assert grant.contexts == ["unattended"]

    # #188: who promoted which finding into which grant.
    [record] = audit.records
    assert record["decision"] == "grant_promoted"
    assert record["actor"] == "webui:alberto"
    assert record["grant_id"] == "uptime-watch"
    assert "uptime watch" in record["reason"]


async def test_promoting_twice_writes_one_grant(tmp_path: Path) -> None:
    """Two grants for one permission means revoking it twice to take it away."""
    service, job = _service_with_job(tmp_path, _refused_state())
    surface = _surface(service)

    surface.promote(job.id, actor="webui")
    second = surface.promote(job.id, actor="webui")

    from nanoinfra.config.loader import load_config

    assert second["granted"] == []
    assert len(load_config().gates.standing_grants) == 1


async def test_an_automation_with_no_refused_finding_cannot_be_promoted(tmp_path: Path) -> None:
    service, job = _service_with_job(tmp_path, CommissioningState(status=OK, fingerprint="abc"))

    with pytest.raises(PromotionRefusedError, match="no refused commissioning finding"):
        _surface(service).promote(job.id, actor="webui")


async def test_a_finding_with_no_proposed_grant_says_the_policy_has_to_change(tmp_path: Path) -> None:
    """The inventory-write and unbounded-scope cases reach here with an empty proposal (#187)."""
    service, job = _service_with_job(tmp_path, _refused_state(proposed_grants=()))

    with pytest.raises(PromotionRefusedError, match="the policy has to change"):
        _surface(service).promote(job.id, actor="webui")


@pytest.mark.parametrize(
    "grant",
    [
        {"contexts": ["unattended"], "hosts": [], "commands": ["uptime"]},
        {"contexts": ["unattended"], "hosts": ["10.0.0.9"], "commands": []},
        {"contexts": ["interactive"], "hosts": ["10.0.0.9"], "commands": ["uptime"]},
        {"contexts": ["unattended"], "hosts": "10.0.0.9", "commands": ["uptime"]},
        "not a grant at all",
    ],
)
async def test_a_stored_proposal_that_is_not_a_grant_is_refused(tmp_path: Path, grant: Any) -> None:
    """A hand-edited record must cost the promotion rather than buy a wider grant."""
    service, job = _service_with_job(tmp_path, _refused_state(proposed_grants=(grant,)))

    with pytest.raises(PromotionRefusedError):
        _surface(service).promote(job.id, actor="webui")

    from nanoinfra.config.loader import load_config

    assert load_config().gates.standing_grants == []


async def test_an_unknown_automation_is_refused(tmp_path: Path) -> None:
    service, _ = _service_with_job(tmp_path, _refused_state())

    with pytest.raises(PromotionRefusedError, match="not found"):
        _surface(service).promote("nope", actor="webui")


async def test_a_second_automation_gets_its_own_grant_id(tmp_path: Path) -> None:
    service, job = _service_with_job(tmp_path, _refused_state())
    surface = _surface(service)
    surface.promote(job.id, actor="webui")

    other = service.add_job(
        name="uptime watch",
        schedule=CronSchedule(kind="every", every_ms=300_000),
        message="Report the uptime elsewhere",
        session_key="websocket:chat-2",
        origin_channel="websocket",
        origin_chat_id="chat-2",
    )
    service.set_commissioning(
        other.id,
        _refused_state(
            proposed_grants=(
                {
                    "id": "uptime-watch",
                    "contexts": ["unattended"],
                    "hosts": ["10.0.0.10"],
                    "commands": ["uptime"],
                },
            )
        ),
        disable=True,
    )

    result = surface.promote(other.id, actor="webui")

    assert result["granted"][0]["id"] == "uptime-watch-2"


@pytest.mark.asyncio
async def test_a_deployment_that_cannot_run_a_turn_refuses_to_rehearse(tmp_path: Path) -> None:
    service, job = _service_with_job(tmp_path, _refused_state())
    surface = _surface(service)

    assert surface.can_commission is False
    with pytest.raises(PromotionRefusedError, match="cannot run a commissioning turn"):
        await surface.commission(job.id)
