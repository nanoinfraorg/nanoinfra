"""What the Apps page shows for a data connector, and the one action it offers.

The row's job is the posture, which is the same job Settings → Identity does for a person:
**who it acts as, and what it may do.** So the payload carries three kinds of fact that an
operator cannot get anywhere else:

- **what the gate will answer for each operation**, under the policy in force, in both
  contexts. A row that said "enabled" would send somebody to a log at 03:00 to find out why an
  automation is refused.
- **which scopes the consent actually granted**, and therefore which classes are unavailable.
  A scope that was never granted is invisible until a call fails.
- **whether it has ever worked**, and who as. A connector that was never tested and one whose
  refresh token was revoked look identical without it.

Everything here is read-only and side-effect free except ``connector_test``, which performs one
declared read through the executor -- the whole path at once: the client, the consent, the
refresh, the scope subset, the projection, and the gate answering `allow`.

There is no Connect action, and that is the decision in `proposals/data-connectors.md`: consent
is a person at a browser, so the payload carries the exact command to run instead of a button
that would have to host a redirect on this origin. An authorisation the agent could start would
be an authorisation nobody performed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from nanoinfra.agent.tools.capabilities import READ
from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.config.loader import load_config
from nanoinfra.connectors import state as connector_state
from nanoinfra.connectors.contracts import ConnectorOperation, ConnectorPlugin
from nanoinfra.connectors.credentials import ConnectorCredential
from nanoinfra.connectors.registry import discover_connectors
from nanoinfra.connectors.setup import ActiveConnector, resolve_active
from nanoinfra.gates.policy import evaluate_connector, load_policy
from nanoinfra.webui.settings_api import WebUISettingsError

# The states a row can be in, and the reason each one is separate. `active` is working.
# `not_activated` means the operator asked for it in `connectors.active` and it did not come up,
# which is a problem with a fix. `inactive` means nobody asked, which is not a problem at all.
STATE_ACTIVE = "active"
STATE_NOT_ACTIVATED = "not_activated"
STATE_INACTIVE = "inactive"


def _decision(
    gates: Any, plugin: ConnectorPlugin, op: ConnectorOperation, context: str
) -> dict[str, str]:
    """What the gate would answer for one operation in one context. Asks nobody."""
    decision = evaluate_connector(
        gates,
        capability_class=op.capability_class,
        execution_context=context,
        connector=plugin.name,
        operation=op.name,
    )
    return {
        "outcome": decision.outcome.value,
        "reason": decision.reason,
        "grant_id": decision.grant_id or "",
    }


def _scope_rows(
    plugin: ConnectorPlugin, credential: ConnectorCredential | None
) -> list[dict[str, Any]]:
    """Each scope the connector declares, whether it was granted, and what it buys.

    Grouped by the class that needs it, because that is the consequence: a missing
    `calendar.events` does not mean "a scope is missing", it means the writes are unavailable.
    """
    granted: frozenset[str] = (
        credential.granted() if credential is not None else frozenset()
    )
    rows: list[dict[str, Any]] = []
    for capability_class in plugin.classes:
        for scope in plugin.credential.scopes_for(capability_class):
            rows.append(
                {
                    "scope": scope,
                    "short": scope.rsplit("/", 1)[-1],
                    "capability_class": capability_class,
                    "granted": scope in granted if credential is not None else False,
                }
            )
    return rows


def _authorize_command(name: str, credential_id: str) -> str:
    """The command that produces a refresh token, with the client id left to the operator.

    Rendered rather than executed. The credential id is not enough to re-authorise: the OAuth
    client id lives in config and the client secret in the store, and the flow needs a person.
    """
    suffix = f"  # credential: {credential_id}" if credential_id else ""
    return f"nanoinfra connectors authorize {name} --client-id <client-id>{suffix}"


def _setup_fields(plugin: ConnectorPlugin) -> list[dict[str, Any]]:
    """The connector's own settings, from its manifest, for a generic form.

    Nothing per-connector is hand-written in the shared UI: the fields, their kinds, their
    defaults and the required groups all come from the package, exactly as a channel's do.
    """
    if plugin.setup is None:
        return []
    required_names: set[str] = set()
    for requirement in plugin.setup.required:
        for alternative in requirement.alternatives:
            required_names.update(alternative)
    return [
        {
            "name": name,
            "kind": spec.kind,
            "default": spec.default if spec.default is not None else "",
            "required": name in required_names,
            "choices": sorted(spec.choices),
            "secret": spec.kind == "secret",
        }
        for name, spec in plugin.setup.fields.items()
    ]


def _row(
    plugin: ConnectorPlugin,
    *,
    cfg: ConnectorRuntimeConfig,
    active: ActiveConnector | None,
    problem: str,
    gates: Any,
    recorded: connector_state.ConnectorState,
) -> dict[str, Any]:
    connector_cfg = cfg.connectors.get(plugin.name)
    asked_for = plugin.name in cfg.active
    if active is not None:
        state = STATE_ACTIVE
    elif asked_for:
        state = STATE_NOT_ACTIVATED
    else:
        state = STATE_INACTIVE

    enabled_names: set[str] = (
        {op.name for op in active.operations} if active is not None else set()
    )
    operations = [
        {
            "name": op.name,
            "tool": plugin.tool_name(op),
            "capability_class": op.capability_class,
            "method": op.method,
            "description": op.description,
            "returns": list(op.returns),
            "enabled": op.name in enabled_names,
            "interactive": _decision(gates, plugin, op, "interactive"),
            "unattended": _decision(gates, plugin, op, "cron"),
        }
        for op in plugin.operations
    ]
    credential = active.credential if active is not None else None
    return {
        "name": plugin.name,
        "display_name": plugin.display_name,
        "description": plugin.description,
        "state": state,
        "problem": problem,
        "credential": connector_cfg.credential if connector_cfg else "",
        "max_class": (connector_cfg.max_class if connector_cfg else None) or "",
        "settings": dict(connector_cfg.settings) if connector_cfg else {},
        "defaults": dict(active.defaults) if active is not None else {},
        "official_url": plugin.setup.official_url if plugin.setup else "",
        "setup_fields": _setup_fields(plugin),
        "operations": operations,
        "scopes": _scope_rows(plugin, credential),
        "classes": list(plugin.classes),
        # The three runtime facts. Empty means "not yet", which the row says rather than implying
        # a failure.
        "acts_as": recorded.acts_as,
        "refreshed_at": recorded.refreshed_at,
        "tested_at": recorded.tested_at,
        "test_summary": recorded.test_summary,
        "last_error": recorded.last_error,
        "last_error_at": recorded.last_error_at,
        "authorize_command": _authorize_command(
            plugin.name, connector_cfg.credential if connector_cfg else ""
        ),
        "testable": state == STATE_ACTIVE and any(op["enabled"] and op["capability_class"] == READ for op in operations),
    }


def registered_connector_tools() -> set[str]:
    """The connector tools this process actually registered.

    What the model can call, rather than what config says it should be able to call. The two
    disagree whenever config changed after boot, and that gap is what `requires_reload` reports.
    """
    from nanoinfra.connectors.registration import registered_tool_names

    return registered_tool_names()


def webui_connectors_payload(workspace_path: Path | str | None = None) -> dict[str, Any]:
    """Every installed connector, with its posture. Reads config, the manifests and the record."""
    config = load_config()
    workspace = Path(workspace_path) if workspace_path is not None else Path(config.workspace_path)
    installed = discover_connectors()
    active, problems = resolve_active(config.connectors)
    by_name = {entry.name: entry for entry in active}
    reasons = {problem.connector: problem.reason for problem in problems}
    recorded = connector_state.read_all(workspace)
    gates = load_policy()

    rows = [
        _row(
            plugin,
            cfg=config.connectors,
            active=by_name.get(name),
            problem=reasons.get(name, ""),
            gates=gates,
            recorded=recorded.get(name, connector_state.ConnectorState()),
        )
        for name, plugin in sorted(installed.items())
    ]
    # What config says the model should be able to call, against what it can. A `docker compose
    # up -d` after a config edit answers "Running" and changes nothing, so the operator has done
    # the documented thing and the tools are still absent -- the row has to say so.
    expected = {
        row["tool"] for entry in rows for row in entry["operations"] if row["enabled"]
    }
    registered = registered_connector_tools()
    missing = sorted(expected - registered)
    stale = sorted(registered - expected)

    return {
        "connectors": rows,
        "installed_count": len(rows),
        "active_count": len(by_name),
        "requires_reload": bool(missing or stale),
        "missing_tools": missing,
        "stale_tools": stale,
        # Named so the UI can say where activation happens rather than offering a toggle it
        # must not have: enabling a connector gives a package a token and a capability class.
        "activation_key": "connectors.active",
    }


def _first_read_operation(entry: ActiveConnector) -> ConnectorOperation | None:
    return next((op for op in entry.operations if op.capability_class == READ), None)


#: How many rows a test asks for. The test exists to prove a credential works, not to read
#: data, and an unbounded listing brought back 250 events on the first real run.
TEST_RESULT_LIMIT = 3


def _test_arguments(operation: ConnectorOperation) -> str:
    """The smallest call that still proves the path.

    Bounded through the operation's own declared parameter, when it has one. A connector with
    no way to say "just a few" is called with nothing rather than with a guessed key: an
    undeclared argument is refused by the executor, and rightly.
    """
    properties = operation.parameters.get("properties")
    if isinstance(properties, dict) and "maxResults" in properties:
        return json.dumps({"maxResults": TEST_RESULT_LIMIT})
    return "{}"


def connector_test(name: str, *, workspace_path: Path | str | None = None) -> dict[str, Any]:
    """Perform one declared read and report what came back.

    This is the useful action. A connector that has never been tested and one whose token was
    revoked look identical without it, and the difference matters at 03:00. It proves the whole
    path in one call, and it proves it through the executor rather than around it -- so a test
    that passes says the real path works, not that a different one would.
    """
    from nanoinfra.agent.tools.server_execution import default_socket_path
    from nanoinfra.gates.executor.client import ExecutorClient, ExecutorUnavailableError

    config = load_config()
    workspace = Path(workspace_path) if workspace_path is not None else Path(config.workspace_path)
    active, problems = resolve_active(config.connectors)
    entry = next((item for item in active if item.name == name), None)
    if entry is None:
        reason = next((p.reason for p in problems if p.connector == name), "")
        raise WebUISettingsError(
            f"connector {name!r} is not active{f': {reason}' if reason else ''}", status=404
        )

    operation = _first_read_operation(entry)
    if operation is None:
        raise WebUISettingsError(
            f"connector {name!r} offers no read operation here, so there is nothing to test "
            "without writing to a real account",
            status=400,
        )

    client = ExecutorClient(default_socket_path())
    try:
        response = client.connector_call(
            connector=name,
            operation=operation.name,
            arguments_json=_test_arguments(operation),
            session_id=None,
            execution_context="interactive",
            preview_requested=False,
        )
    except ExecutorUnavailableError as exc:
        raise WebUISettingsError(
            f"the executor is not reachable, so nothing was tested: {exc}", status=503
        ) from exc

    if not response.ok:
        message = response.error or response.reason or "the read did not succeed"
        connector_state.record(
            workspace, name, last_error=message, last_error_at=connector_state.now_iso()
        )
        return {
            "ok": False,
            "connector": name,
            "operation": operation.name,
            "message": message,
        }

    summary, acts_as = _summarise(response.output, operation, entry)
    connector_state.record(
        workspace,
        name,
        tested_at=connector_state.now_iso(),
        test_summary=summary,
        acts_as=acts_as,
        # A read that just succeeded retracts the last failure. Without this the field only ever
        # accumulated: a connector fixed two releases ago kept showing the error that no longer
        # happens, in red, under a row that works. An error nothing retracts stops being
        # information and becomes decoration.
        clear=("last_error", "last_error_at"),
    )
    return {
        "ok": True,
        "connector": name,
        "operation": operation.name,
        "summary": summary,
        "acts_as": acts_as,
        "result": response.output,
    }


def connector_objects(
    *, workspace_path: Path | str | None = None, refresh: bool = True
) -> dict[str, Any]:
    """The objects a person may pin with a mention, for the composer's autocomplete.

    ``refresh`` performs one declared read per connector through the executor and caches what
    came back. Passing false reads the cache alone, which is what a caller wants when it only
    needs to know what exists without paying for a call.

    A connector whose listing fails contributes its cached objects and a reason. One unreachable
    API must not empty a menu that worked a minute ago.
    """
    from nanoinfra.agent.tools.server_execution import default_socket_path
    from nanoinfra.gates.executor.client import ExecutorClient

    config = load_config()
    workspace = Path(workspace_path) if workspace_path is not None else Path(config.workspace_path)
    active, _problems = resolve_active(config.connectors)
    recorded = connector_state.read_all(workspace)

    objects: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    client = ExecutorClient(default_socket_path()) if refresh else None

    for entry in active:
        for mention in entry.plugin.mentions:
            listing = entry.plugin.operation(mention.operation)
            if listing is None or not any(op.name == mention.operation for op in entry.operations):
                # The deployment disabled the listing operation, so this kind is not offered
                # here. Silent on purpose: the operator chose it.
                continue
            fresh: list[dict[str, Any]] | None = None
            if client is not None:
                fresh, failure = _list_objects(client, entry, mention, listing)
                if failure:
                    problems.append({"connector": entry.name, "message": failure})
            if fresh is not None:
                connector_state.record(
                    workspace,
                    entry.name,
                    objects=json.dumps(fresh, ensure_ascii=False),
                    objects_at=connector_state.now_iso(),
                    # A live listing is a successful read, so it retracts the last failure for the
                    # same reason a successful test does.
                    clear=("last_error", "last_error_at"),
                )
                objects.extend(fresh)
                continue
            cached = _cached_objects(recorded.get(entry.name))
            objects.extend(entry for entry in cached if entry.get("kind") == mention.kind)

    return {"objects": objects, "problems": problems}


def _argument_consumers(
    entry: ActiveConnector, mention: Any, listing: ConnectorOperation
) -> list[str]:
    """The enabled tools that actually take this id, listing excluded.

    The point of pinning an id is that a later call uses it, so the hint has to name the call.
    Naming the listing operation instead -- the first version of this did -- would tell the model
    to pass a calendar id to the tool that produced it.
    """
    argument = mention.argument
    if not argument:
        return []
    placeholder = f"{{{argument}}}"
    consumers: list[str] = []
    for op in entry.operations:
        if op.name == listing.name:
            continue
        properties = op.parameters.get("properties")
        declared = isinstance(properties, dict) and argument in properties
        if declared or placeholder in op.path:
            consumers.append(entry.plugin.tool_name(op))
    return consumers


def _list_objects(
    client: Any,
    entry: ActiveConnector,
    mention: Any,
    listing: ConnectorOperation,
) -> tuple[list[dict[str, Any]] | None, str]:
    """One read, projected into mention targets. Returns None when nothing usable came back."""
    from nanoinfra.gates.executor.client import ExecutorUnavailableError

    try:
        response = client.connector_call(
            connector=entry.name,
            operation=mention.operation,
            arguments_json="{}",
            session_id=None,
            execution_context="interactive",
            preview_requested=False,
        )
    except ExecutorUnavailableError as exc:
        return None, f"the executor is not reachable: {exc}"
    if not response.ok:
        return None, response.error or response.reason or "the listing did not succeed"

    try:
        payload = cast(object, json.loads(response.output or "{}"))
    except ValueError:
        return None, "the listing returned something this page cannot parse"
    if not isinstance(payload, dict):
        return None, "the listing returned no object"

    items = cast(dict[str, Any], payload).get(listing.collection or "items")
    if not isinstance(items, list):
        return None, "the listing returned no collection"

    consumers = _argument_consumers(entry, mention, listing)
    found: list[dict[str, Any]] = []
    for raw in cast(list[Any], items):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        ident = item.get(mention.id_field)
        if not isinstance(ident, str) or not ident:
            continue
        label = item.get(mention.label_field)
        detail = " · ".join(
            str(item[field]) for field in mention.detail_fields if item.get(field) not in (None, "")
        )
        found.append(
            {
                "connector": entry.name,
                "kind": mention.kind,
                "id": ident,
                "name": str(label) if isinstance(label, str) and label else ident,
                "detail": detail,
                "argument": mention.argument,
                # Every tool that takes this id, so the context block can name the call rather
                # than the listing that produced it.
                "tools": consumers,
                "tool": consumers[0] if consumers else "",
            }
        )
    return found, ""


def _cached_objects(recorded: connector_state.ConnectorState | None) -> list[dict[str, Any]]:
    if recorded is None or not recorded.objects:
        return []
    try:
        raw = cast(object, json.loads(recorded.objects))
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[Any], raw) if isinstance(item, dict)]


def _summarise(
    output: str, operation: ConnectorOperation, entry: ActiveConnector
) -> tuple[str, str]:
    """One line about what came back, and the account it came from when the payload names it.

    The Test result exists to prove a credential works rather than to be read, so this counts
    rows and never renders the data. The account is the one fact worth keeping: a primary
    calendar's own id is the address the connector acts as, which is how the row can say "acts
    as" without asking for an identity scope the connector does not need.
    """
    try:
        payload = cast(object, json.loads(output or "{}"))
    except ValueError:
        return "the read succeeded and returned something this page cannot parse", ""
    if not isinstance(payload, dict):
        return "the read succeeded", ""

    body = cast(dict[str, Any], payload)
    acts_as = ""
    for key in ("id", "summary"):
        value = body.get(key)
        if isinstance(value, str) and "@" in value:
            acts_as = value
            break

    if operation.collection:
        items = body.get(operation.collection)
        if isinstance(items, list):
            count = len(cast(list[Any], items))
            fields = ", ".join(operation.returns[:4]) or "the declared fields"
            noun = "item" if count == 1 else "items"
            return f"{operation.name} returned {count} {noun} with {fields}", acts_as
    kept = ", ".join(sorted(body)[:5])
    return f"{operation.name} returned {kept or 'an empty object'}", acts_as


__all__ = [
    "STATE_ACTIVE",
    "STATE_INACTIVE",
    "STATE_NOT_ACTIVATED",
    "TEST_RESULT_LIMIT",
    "connector_objects",
    "connector_test",
    "webui_connectors_payload",
]
