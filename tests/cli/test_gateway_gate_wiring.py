# tests/cli/test_gateway_gate_wiring.py
"""The gateway must build the gate runtime and hand it to the agent (#33).

Every component can be correct and the gate can still be absent in production, because the
tools fall back to policy alone when no runtime arrives. That fallback keeps an embedded or a
test construction working, and it also makes "the gateway forgot to wire it" silent. So the
wiring itself gets a test.
"""

from __future__ import annotations

import inspect

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.tools.context import ToolContext
from nanoinfra.cli import gateway_runtime


def test_the_gateway_passes_a_gate_runtime_to_the_agent() -> None:
    """A source check, because building a real gateway needs a provider and a bus."""
    source = inspect.getsource(gateway_runtime)

    assert "_build_gate_runtime_for_gateway(" in source
    assert "gate=" in source


def test_the_agent_loop_accepts_a_gate() -> None:
    assert "gate" in inspect.signature(AgentLoop.__init__).parameters


def test_the_tool_context_carries_a_gate() -> None:
    assert "gate" in ToolContext.__dataclass_fields__


def test_the_agent_loop_puts_its_gate_into_the_tool_context() -> None:
    """The loop builds the ToolContext, so the field has to travel that far."""
    source = inspect.getsource(AgentLoop._register_default_tools)

    assert "gate=self.gate" in source


def test_the_gateway_keeps_the_latch_controller_out_of_the_agent() -> None:
    """#15 splits the halves. The operator half must not travel toward the tools.

    A gateway that passed the controller into the agent would hand every tool a way to clear a
    latch, which is the one thing that split prevents.
    """
    source = inspect.getsource(gateway_runtime)

    assert "gate=gate_runtime" in source
    assert "gate=latch_controller" not in source
