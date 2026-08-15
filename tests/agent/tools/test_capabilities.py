# tests/agent/tools/test_capabilities.py
"""Item 1 (#3): every tool declares a capability class, and an omission fails closed."""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.capabilities import (
    CAPABILITY_CLASSES,
    MUTATE_INVENTORY,
    MUTATE_LOCAL,
    MUTATE_REMOTE,
    READ,
    capability_class_of,
)
from nanoinfra.agent.tools.loader import ToolLoader
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.agent.tools.servers import (
    CreateServerTool,
    DeleteServerTool,
    ListServersTool,
    UpdateServerTool,
)


class _Unclassified(Tool):
    """A third-party-shaped tool that forgets to declare a class."""

    @property
    def name(self) -> str:
        return "unclassified"

    @property
    def description(self) -> str:
        return "declares no capability class"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return ""


def test_tool_without_a_declared_class_resolves_to_mutate_remote() -> None:
    """Fail closed. An omission must not buy permissive defaults."""
    assert capability_class_of(_Unclassified()) == MUTATE_REMOTE


def test_every_shipped_tool_declares_a_capability_class() -> None:
    """CI guard: a new tool without a class breaks the build instead of shipping.

    Membership, not just non-None: a typo such as ``"mutate_local"`` would otherwise pass
    this guard while capability_class_of() silently resolved it to the fail-closed class.
    """
    bad = sorted(
        f"{cls.__name__}={cls.capability_class!r}"
        for cls in ToolLoader().discover()
        if cls.capability_class not in CAPABILITY_CLASSES
    )
    assert bad == []


@pytest.mark.parametrize("tool_cls", [CreateServerTool, UpdateServerTool, DeleteServerTool])
def test_inventory_writes_are_not_mutate_local(tool_cls: type[Tool]) -> None:
    """An inventory write changes what a later remote action means, so it gets its own class.

    UpdateServerTool replaces config and secretRef in full, so `mutate.local` would let an
    unattended turn repoint a granted name at another address.
    """
    assert capability_class_of(tool_cls) == MUTATE_INVENTORY


def test_execute_on_server_is_mutate_remote() -> None:
    assert capability_class_of(ExecuteOnServerTool) == MUTATE_REMOTE


def test_inventory_reads_are_read() -> None:
    assert capability_class_of(ListServersTool) == READ


def test_class_is_a_property_of_the_tool_not_of_its_arguments() -> None:
    """`dry_run=true` does not lower the class. Only the recorded decision changes."""
    assert MUTATE_LOCAL != capability_class_of(ExecuteOnServerTool)
