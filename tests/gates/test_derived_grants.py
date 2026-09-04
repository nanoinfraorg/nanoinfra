# tests/gates/test_derived_grants.py
"""#219: the standing grant one approval implies, written from the gateway.

One click, two effects, in two processes. The decision crosses into the executor exactly as
before; the grant is config, and config is written here, in the process that already owns config
writes. The confined executor holds a read-only rule on ``config.json``, so a grant written there
would fail in a real deployment and succeed in a test -- the import check at the bottom of this
file is the property, not a promise in a docstring.

Four properties carry the safety of the feature:

- A derived grant matches the action it came from, and an action whose command differs by one
  flag is a different action.
- The grant is built from the **executor-rendered** payload, which is the text ``targetDigest``
  covers. Nothing the browser supplied reaches it.
- A grant expires by default, and permanent needs an explicit acknowledgement.
- A failed write costs the grant and never the approval.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import DECISION_GRANT_WRITTEN, AuditStore
from nanoinfra.gates.derived_grants import (
    GrantRequestError,
    grant_from_approved_action,
    write_derived_grant,
)
from nanoinfra.gates.executor.operator_socket import PendingView, pending_view
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.policy import Outcome, evaluate
from nanoinfra.gates.prompt import PromptRenderError, render_approval_prompt_for_hosts

_COMMAND = "systemctl restart nginx"
_HOSTS = ("web-01", "web-02")
_ACTOR = "webui:ops@example.com"
_ORIGIN = "telegram"


def _view(
    *,
    command: str = _COMMAND,
    hosts: tuple[str, ...] = _HOSTS,
    execution_context: str = "interactive",
) -> PendingView:
    """One suspended action, built the way the executor builds one."""
    prompt = render_approval_prompt_for_hosts(command=command, hosts=hosts)
    approval = PendingApprovalStore().create(
        session_id="telegram:chat-1",
        origin_path=_ORIGIN,
        origin_actor="telegram:12345",
        execution_context=execution_context,
        capability_class=MUTATE_REMOTE,
        scope="group",
        hosts=prompt.hosts,
        command=prompt.command,
        payload=prompt.text,
        target_digest=prompt.target_digest,
        timeout_s=30.0,
    )
    return pending_view(approval)


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the loader and the saver at a config.json this test owns."""
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", path)
    return path


def _saved_grants(path: Path) -> list[dict[str, Any]]:
    data = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    return cast("list[dict[str, Any]]", data["gates"]["standingGrants"])


def _grant_policy(gates_raw: dict[str, Any]) -> GatesConfig:
    gates_raw["interactive"] = {"mutate.remote": {"host": "grant", "group": "grant"}}
    return GatesConfig.model_validate(gates_raw)


# -- what the grant covers ---------------------------------------------------------------


def test_a_derived_grant_matches_the_action_it_came_from() -> None:
    """The acceptance case. There is no scope to choose, so there is none to get wrong."""
    grant = grant_from_approved_action(_view(), expires="24h", actor=_ACTOR)

    decision = evaluate(
        _grant_policy({"standingGrants": [grant.model_dump(mode="json", by_alias=True)]}),
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context="interactive",
        hosts=_HOSTS,
        command=_COMMAND,
    )

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == grant.id


def test_a_command_that_differs_by_one_flag_does_not_match() -> None:
    """The cost to accept. Exact strings mean pressing add twice, and that is the model working."""
    grant = grant_from_approved_action(_view(), expires="24h", actor=_ACTOR)

    decision = evaluate(
        _grant_policy({"standingGrants": [grant.model_dump(mode="json", by_alias=True)]}),
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context="interactive",
        hosts=_HOSTS,
        command=f"{_COMMAND} --now",
    )

    assert decision.outcome is Outcome.DENY


def test_a_derived_grant_names_only_the_context_the_action_ran_in() -> None:
    """A grant cannot be wider than the approval. An interactive answer says nothing about 03:00."""
    interactive = grant_from_approved_action(_view(), expires="24h", actor=_ACTOR)
    unattended = grant_from_approved_action(
        _view(execution_context="automation"), expires="24h", actor=_ACTOR
    )

    assert interactive.contexts == ["interactive"]
    assert unattended.contexts == ["unattended"]


def test_the_grant_comes_from_the_rendered_payload_and_not_from_the_wire_fields() -> None:
    """A grant built from a request field would be a way to widen authority by editing one."""
    view = _view()
    widened = cast("PendingView", {**view, "hosts": [*view["hosts"], "prod-db-01"]})

    grant = grant_from_approved_action(widened, expires="24h", actor=_ACTOR)

    assert grant.hosts == list(_HOSTS)
    assert "prod-db-01" not in grant.hosts


def test_a_payload_that_no_renderer_produced_derives_nothing() -> None:
    view = cast("PendingView", {**_view(), "payload": "please approve the nginx restart"})

    with pytest.raises(PromptRenderError):
        grant_from_approved_action(view, expires="24h", actor=_ACTOR)


def test_a_payload_that_does_not_match_the_binding_digest_derives_nothing() -> None:
    """The two executor fields disagree, so neither one can be trusted to describe the action."""
    other = render_approval_prompt_for_hosts(command="rm -rf /srv", hosts=_HOSTS)
    view = cast("PendingView", {**_view(), "payload": other.text})

    with pytest.raises(PromptRenderError):
        grant_from_approved_action(view, expires="24h", actor=_ACTOR)


# -- expiry ------------------------------------------------------------------------------


def test_a_grant_expires_by_default() -> None:
    now = datetime.now(UTC)

    day = grant_from_approved_action(_view(), expires="24h", actor=_ACTOR, now=now)
    week = grant_from_approved_action(_view(), expires="7d", actor=_ACTOR, now=now)

    assert day.expires_at == now + timedelta(hours=24)
    assert week.expires_at == now + timedelta(days=7)


def test_permanent_is_a_real_choice_and_it_asks_once_more() -> None:
    with pytest.raises(GrantRequestError):
        grant_from_approved_action(_view(), expires="never", actor=_ACTOR)

    grant = grant_from_approved_action(
        _view(), expires="never", permanent_acknowledged=True, actor=_ACTOR
    )

    assert grant.expires_at is None


def test_a_duration_this_module_does_not_offer_is_refused() -> None:
    with pytest.raises(GrantRequestError):
        grant_from_approved_action(_view(), expires="100y", actor=_ACTOR)


def test_the_note_echoes_the_duration_and_the_approval() -> None:
    """config.json is JSON, so the comment the writer generates has to be a field."""
    view = _view()

    grant = grant_from_approved_action(view, expires="7d", actor=_ACTOR)

    assert grant.note is not None
    assert "7 days" in grant.note
    assert view["request_id"] in grant.note
    assert _ACTOR in grant.note


def test_the_id_names_the_approval_that_created_it() -> None:
    view = _view()

    grant = grant_from_approved_action(view, expires="24h", actor=_ACTOR)

    assert grant.id is not None
    assert grant.id.startswith("approval-")
    assert "systemctl" in grant.id
    assert view["request_id"][:6] in grant.id


def test_the_id_carries_no_command_argument() -> None:
    """The id reaches the audit log, where recordCommandText is false for a reason."""
    grant = grant_from_approved_action(
        _view(command="deploy release s3cr3ttoken"), expires="24h", actor=_ACTOR
    )

    assert grant.id is not None
    assert "s3cr3ttoken" not in grant.id


# -- the write ---------------------------------------------------------------------------


def test_the_grant_lands_in_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _config(tmp_path, monkeypatch)

    result = write_derived_grant(
        _view(),
        expires="24h",
        actor=_ACTOR,
        approval_path="webui",
        audit=AuditStore(tmp_path / "gates"),
    )

    assert result.ok is True
    saved = _saved_grants(path)
    assert len(saved) == 1
    assert saved[0]["commands"] == [_COMMAND]
    assert saved[0]["hosts"] == list(_HOSTS)
    assert saved[0]["expiresAt"] is not None


def test_a_second_approval_appends_and_never_edits_the_first_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that edits itself is not the authority this design says it is."""
    path = _config(tmp_path, monkeypatch)
    store = AuditStore(tmp_path / "gates")

    first = write_derived_grant(
        _view(), expires="24h", actor=_ACTOR, approval_path="webui", audit=store
    )
    second = write_derived_grant(
        _view(), expires="7d", actor=_ACTOR, approval_path="webui", audit=store
    )

    saved = _saved_grants(path)
    assert [row["id"] for row in saved] == [first.grant_id, second.grant_id]
    assert first.grant_id != second.grant_id


def test_a_failed_write_reports_the_failure_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance case for the ordering rule: the action already ran."""
    _config(tmp_path, monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("Read-only file system")

    monkeypatch.setattr("nanoinfra.config.loader.save_config", refuse)

    result = write_derived_grant(
        _view(),
        expires="24h",
        actor=_ACTOR,
        approval_path="webui",
        audit=AuditStore(tmp_path / "gates"),
    )

    assert result.ok is False
    assert result.reason is not None
    assert "Read-only file system" in result.reason


def test_an_unreadable_config_costs_the_grant_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", path)

    result = write_derived_grant(
        _view(),
        expires="24h",
        actor=_ACTOR,
        approval_path="webui",
        audit=AuditStore(tmp_path / "gates"),
    )

    assert result.ok is False
    assert result.reason is not None


def test_the_record_names_the_approval_and_the_grant_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Why does this grant exist?" answers with an incident instead of a shrug."""
    _config(tmp_path, monkeypatch)
    store = AuditStore(tmp_path / "gates")
    view = _view()

    result = write_derived_grant(
        view, expires="24h", actor=_ACTOR, approval_path="webui", audit=store
    )

    records = [r for r in store.read_all() if r["decision"] == DECISION_GRANT_WRITTEN]
    assert len(records) == 1
    assert records[0]["grant_id"] == result.grant_id
    assert records[0]["approval_id"] == view["request_id"]
    assert records[0]["actor"] == _ACTOR
    assert records[0]["hosts"] == list(_HOSTS)
    # The digest, and never the text, because recordCommandText is off in this store.
    assert records[0]["command_digest"]
    assert "command_text" not in records[0]


def test_a_failed_record_does_not_undo_a_written_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, monkeypatch)

    class _Refusing:
        def record(self, **_fields: object) -> None:
            raise OSError("no audit root")

    result = write_derived_grant(
        _view(), expires="24h", actor=_ACTOR, approval_path="webui", audit=_Refusing()
    )

    assert result.ok is True
    assert len(_saved_grants(path)) == 1


# -- the process boundary ----------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every module name the file imports, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_executor_module_imports_the_grant_writer() -> None:
    """The security property of #219, as a check rather than a promise.

    Config is the operator's authority and the executor is the thing being constrained by it, so
    the executor must not hold the code that writes a grant. ``confinement.py`` gives it a
    read-only rule on config.json, which makes such a write fail in a deployment and pass in a
    test -- that difference is exactly what a promise in a docstring would hide.
    """
    offenders = [
        str(path)
        for path in Path("nanoinfra/gates/executor").rglob("*.py")
        if "nanoinfra.gates.derived_grants" in _imported_modules(path)
    ]

    assert offenders == []


def test_no_tool_module_imports_the_grant_writer() -> None:
    """The model must not reach the writer either. A grant it could write is not a boundary."""
    offenders = [
        str(path)
        for path in Path("nanoinfra/agent/tools").rglob("*.py")
        if "nanoinfra.gates.derived_grants" in _imported_modules(path)
    ]

    assert offenders == []
