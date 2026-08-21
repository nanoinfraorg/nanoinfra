"""Let an automation remember something between its own runs.

The scope is deliberately not a parameter. The automation id comes from the running turn, so this
tool cannot be pointed at another automation's state -- and on an interactive turn there is no
automation to be scoped to, so it refuses instead of writing somewhere arbitrary.

That refusal is the design. Before this, remembering across runs meant asking the model, in prose,
to maintain a JSON file at a path the operator invented. Two automations given the same path shared
state without either knowing, and nothing could inspect or reset what either believed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanoinfra.agent.tools.base import Tool, ToolResult
from nanoinfra.agent.tools.context import ToolContext, current_request_context
from nanoinfra.agent.tools.schema import StringSchema, tool_parameters_schema
from nanoinfra.automations.state import (
    AutomationStateError,
    AutomationStateStore,
)
from nanoinfra.session.automation_turns import automation_identity

_ACTIONS = ["get", "set", "delete", "list"]

_PARAMETERS = tool_parameters_schema(
    action=StringSchema("Action to perform", enum=_ACTIONS),
    key=StringSchema(
        "REQUIRED for get, set and delete. A short name for the thing being remembered "
        "(e.g. 'reported_issue_numbers'). Not used by list."
    ),
    value=StringSchema(
        "REQUIRED when action='set'. JSON encoded. A JSON array or object is stored as such; "
        "anything that does not parse as JSON is stored as a plain string."
    ),
    required=["action"],
    description=(
        "Per-action requirements are enforced at runtime rather than in the schema, so the "
        "top-level shape stays compatible with providers that reject oneOf/anyOf at the root "
        "of function parameters."
    ),
)


class AutomationStateTool(Tool):
    """Read and write the state of the automation running this turn."""

    capability_class = "mutate.local"

    def __init__(self, workspace: Path):
        self._store = AutomationStateStore(workspace)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return bool(ctx.workspace)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if not ctx.workspace:
            raise RuntimeError("AutomationStateTool requires a workspace")
        return cls(workspace=Path(ctx.workspace))

    @property
    def name(self) -> str:
        return "automation_state"

    @property
    def description(self) -> str:
        return (
            "Remember something between runs of the automation that triggered this turn. "
            "Actions: get, set, delete, list. Use this instead of writing a state file by hand "
            "-- it is scoped to this automation, and an operator can inspect and reset it. "
            "Only available on a scheduled or triggered turn."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _PARAMETERS

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        if action in {"get", "set", "delete"} and not str(params.get("key") or "").strip():
            errors.append(f"key is required when action='{action}'")
        if action == "set" and params.get("value") is None:
            errors.append("value is required when action='set'")
        return errors

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        action: str,
        key: str | None = None,
        value: str | None = None,
        **_extra: Any,
    ) -> ToolResult:
        identity = self._identity()
        if identity is None:
            return ToolResult.error(
                "automation_state is only available on a scheduled or triggered turn. "
                "There is no automation to scope this to."
            )
        _kind, automation_id = identity

        try:
            if action == "list":
                return self._render_list(automation_id)
            if action == "get":
                return self._render_get(automation_id, str(key))
            if action == "set":
                return self._render_set(automation_id, str(key), value)
            if action == "delete":
                removed = self._store.delete(automation_id, str(key))
                return ToolResult(
                    f"Forgot '{key}'." if removed else f"Nothing was stored under '{key}'."
                )
        except AutomationStateError as exc:
            return ToolResult.error(str(exc))

        return ToolResult.error(f"Unknown action '{action}'. Expected one of: {', '.join(_ACTIONS)}")

    # --- rendering ---

    def _render_list(self, automation_id: str) -> ToolResult:
        values = self._store.snapshot(automation_id)
        if not values:
            return ToolResult("This automation has not stored any state yet.")
        return ToolResult(json.dumps(values, ensure_ascii=False, indent=2))

    def _render_get(self, automation_id: str, key: str) -> ToolResult:
        values = self._store.snapshot(automation_id)
        if key not in values:
            # Distinguished from a stored null on purpose: "not set yet" is the first-run answer
            # and the model has to be able to tell it apart from "set to nothing".
            return ToolResult(f"Nothing is stored under '{key}'.")
        return ToolResult(json.dumps(values[key], ensure_ascii=False))

    def _render_set(self, automation_id: str, key: str, value: str | None) -> ToolResult:
        self._store.set(automation_id, key, _decode(value))
        return ToolResult(f"Stored '{key}'.")

    @staticmethod
    def _identity() -> tuple[str, str] | None:
        ctx = current_request_context()
        if ctx is None:
            return None
        return automation_identity(ctx.metadata)


def _decode(value: str | None) -> Any:
    """Parse JSON when it parses, and keep the raw string when it does not.

    A model that means to store the string ``done`` should not have to remember to quote it, and
    one that stores a list should get a list back.
    """
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value
