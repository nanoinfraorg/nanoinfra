"""The tool loader's log lines named nothing.

Five sites passed printf ``%s`` to loguru, which formats with ``{}``. The
message printed a literal ``%s`` and the argument was dropped, so a plugin that
failed to load, or a tool that quietly overwrote another, was reported without
saying which one.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from loguru import logger

from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.loader import ToolLoader


def _sink(level: str) -> tuple[io.StringIO, int]:
    buf = io.StringIO()
    return buf, logger.add(buf, format="{message}", level=level)


def test_failed_plugin_load_names_the_entry_point() -> None:
    ep = MagicMock()
    ep.name = "broken_plugin_sentinel"
    ep.load.side_effect = ImportError("no such module")

    buf, handler = _sink("ERROR")
    try:
        with patch("nanoinfra.agent.tools.loader.entry_points", return_value=[ep]):
            assert ToolLoader()._discover_plugins() == {}
    finally:
        logger.remove(handler)

    out = buf.getvalue()
    assert "Failed to load tool plugin: broken_plugin_sentinel" in out
    assert "%s" not in out


def test_builtin_conflict_names_both_the_plugin_and_the_tool(tmp_path) -> None:
    """Exercises the two ``logger.warning`` collision sites."""

    class _Clashing(Tool):
        _plugin_discoverable = True
        _scopes = {"core"}

        @property
        def name(self) -> str:
            return "read_file"

        @property
        def description(self) -> str:
            return "clashes with a built-in"

        @property
        def parameters(self) -> dict:
            return {"type": "object"}

        @classmethod
        def enabled(cls, ctx):
            return True

        @classmethod
        def create(cls, ctx):
            return _Clashing()

        async def execute(self, **_):
            return "ok"

    ep = MagicMock()
    ep.name = "clashing_plugin"
    ep.load.return_value = _Clashing

    from types import SimpleNamespace

    from nanoinfra.agent.tools.context import ToolContext
    from nanoinfra.agent.tools.registry import ToolRegistry
    from nanoinfra.config.schema import ToolsConfig

    ctx = ToolContext(
        config=ToolsConfig(),
        workspace=str(tmp_path),
        bus=None,
        subagent_manager=SimpleNamespace(
            get_running_count=lambda: 0,
            max_concurrent_subagents=4,
        ),
        cron_service=None,
        timezone="UTC",
    )
    registry = ToolRegistry()

    buf, handler = _sink("WARNING")
    try:
        with patch("nanoinfra.agent.tools.loader.entry_points", return_value=[ep]):
            ToolLoader().load(ctx, registry)
    finally:
        logger.remove(handler)

    out = buf.getvalue()
    assert "%s" not in out
    # `read_file` is a real built-in, so the plugin loses to it by name.
    assert "Plugin _Clashing skipped: conflicts with built-in tool read_file" in out
