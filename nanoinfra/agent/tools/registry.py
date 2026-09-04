"""Tool registry for dynamic tool management."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from nanoinfra.agent.tools.base import Tool, ToolResult
from nanoinfra.agent.tools.capabilities import capability_class_of
from nanoinfra.agent.tools.context import ContextAware, current_request_context

if TYPE_CHECKING:
    from nanoinfra.runtime_context import RuntimeContextProvider


def is_tool_error_result(result: Any) -> bool:
    return isinstance(result, ToolResult) and result.is_error


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def source_counts(self) -> dict[str, int]:
        """How many registered tools each `Tool.source` contributes.

        Counted regardless of `available()`, unlike `schema_breakdown`: the caller for this is the
        MCP advertisement (#204), which exists precisely to name tools the turn is *not* carrying.
        """
        counts: dict[str, int] = {}
        for tool in self._tools.values():
            source = tool.source
            counts[source] = counts.get(source, 0) + 1
        return counts

    def get_runtime_context_providers(self) -> list[RuntimeContextProvider]:
        """Return tool-owned providers in stable tool-name order."""
        providers: list[RuntimeContextProvider] = []
        for name in sorted(self._tools):
            provider = self._tools[name].runtime_context_provider()
            if provider is not None:
                providers.append(provider)
        return providers

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = cast(dict[str, Any], fn).get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended.  The result is cached until the next
        register/unregister call. Request-scoped availability is applied after
        the cached schemas are built.
        """
        if self._cached_definitions is None:
            definitions = [tool.to_schema() for tool in self._tools.values()]
            builtins: list[dict[str, Any]] = []
            mcp_tools: list[dict[str, Any]] = []
            for schema in definitions:
                name = self._schema_name(schema)
                if name.startswith("mcp_"):
                    mcp_tools.append(schema)
                else:
                    builtins.append(schema)

            builtins.sort(key=self._schema_name)
            mcp_tools.sort(key=self._schema_name)
            self._cached_definitions = builtins + mcp_tools

        return [
            schema
            for schema in self._cached_definitions
            if self._is_available(self._schema_name(schema))
        ]

    def _is_available(self, name: str) -> bool:
        """Whether this tool's schema belongs in the current request.

        Two gates, and the group one lives here rather than in `Tool.available()` because a group
        is declared by *tool name*: the registry is the only place that maps a name to a tool
        without every tool class having to participate. It also means a group may name an MCP or
        connector tool, which costs nothing and is occasionally what an operator wants.
        """
        from nanoinfra.agent.tools import groups

        tool = self._tools.get(name)
        if tool is None:
            return False
        return tool.available() and groups.is_attached(name)

    def schema_breakdown(self) -> list[dict[str, Any]]:
        """What the tool schemas cost, per source, for the prompt manifest (#203).

        Measured on the schemas that would actually be sent -- the same availability filter
        `get_definitions` applies -- because a panel that counted tools the request does not carry
        would be a panel that disagrees with the bill.

        Grouped by `Tool.source` rather than by parsing names: `mcp_<server>_<tool>` is sanitised
        and both halves may hold underscores, so the string cannot be split reliably.
        """
        import json

        from nanoinfra.utils.helpers import count_text_tokens

        grouped: dict[str, dict[str, int]] = {}
        per_tool: dict[str, list[dict[str, int | str]]] = {}
        for schema in self.get_definitions():
            name = self._schema_name(schema)
            tool = self._tools.get(name)
            source = tool.source if tool is not None else "builtin"
            serialised = json.dumps(schema, ensure_ascii=False)
            chars = len(serialised)
            tokens = count_text_tokens(serialised)
            entry = grouped.setdefault(source, {"chars": 0, "tokens": 0, "items": 0})
            entry["chars"] += chars
            entry["tokens"] += tokens
            entry["items"] += 1
            # Named, because "31 tools" is a number that raises the question it cannot answer, and
            # a reader deciding what to trim needs to know *which* of the thirty-one is the
            # expensive one. Names and sizes only: a schema is not carried anywhere.
            per_tool.setdefault(source, []).append(
                {"name": name, "chars": chars, "tokens": tokens}
            )
        return [
            {
                "source": source,
                **totals,
                "tools": sorted(
                    per_tool.get(source, []),
                    key=lambda row: (-int(row["tokens"]), str(row["name"])),
                ),
            }
            for source, totals in sorted(grouped.items(), key=lambda item: -item[1]["tokens"])
        ]

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self._tools.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
                )
            )
        if not tool.available():
            return None, params, ToolResult.error(f"Error: Tool '{name}' is unavailable")

        # The acting agent's tool groups are a ceiling, not a suggestion (#257). Withholding the
        # schema is not enough by itself: a model that names a tool it never saw would otherwise
        # reach it. Checked here rather than in `execute`, because the runner prepares a call and
        # awaits the tool directly -- this is the one place both paths pass through.
        from nanoinfra.agent.tools import groups

        ceiling_refusal = groups.agent_ceiling_refusal(name)
        if ceiling_refusal:
            return None, params, ToolResult.error(ceiling_refusal)

        # Compatibility for external tools that still implement the legacy
        # setter protocol. Built-ins read the authoritative ContextVar
        # directly and never copy routing state.
        if isinstance(tool, ContextAware) and (ctx := current_request_context()) is not None:
            tool.set_context(ctx)

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' parameters must be a JSON object, got "
                    f"{type(params).__name__}. Use named parameters like "
                    'tool_name(param1="value1", param2="value2") matching the tool schema.'
                )
            )

        cast_params = tool.cast_params(cast(dict[str, Any], params))
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                ToolResult.error(f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors))
            )
        return tool, cast_params, None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict):
            return params
        arguments_payload = cast(dict[str, Any], params)
        if set(arguments_payload) != {"arguments"}:
            return arguments_payload
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return arguments_payload
        return cls._coerce_argument_value(arguments_payload.get("arguments"))

    async def execute(self, name: str, params: Any) -> Any:
        """Execute a tool by name with given parameters."""
        hint = "\n\n[Analyze the error above and try a different approach.]"
        # Imported here, not at module level: the usage package reaches `providers/base.py`, which
        # reaches `config/schema.py`, which imports this module -- and #57 already carries the
        # deferred resolve that cycle needs. A per-call import is a dict lookup after the first.
        from nanoinfra.llm_usage.tool_calls import classify_tool_error, tool_call_record

        # One tool-call row per call (#232). The scope is re-entrant, so this records nothing when
        # the caller already opened one -- the runner does, because it executes a prepared tool
        # directly and never reaches this method for a tool it resolved.
        with tool_call_record(tool=name) as row:
            tool, params, error = self.prepare_call(name, params)
            if error:
                row.failed(classify_tool_error(error))
                return ToolResult.error(str(error) + hint)

            row.capability_class = capability_class_of(tool)
            try:
                assert tool is not None  # guarded by prepare_call()
                result = await tool.execute(**params)
                if is_tool_error_result(result):
                    row.failed(classify_tool_error(result))
                    return ToolResult.error(str(result) + hint)
                return result
            except Exception as e:
                row.failed("exception")
                return ToolResult.error(f"Error executing {name}: {str(e)}" + hint)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
