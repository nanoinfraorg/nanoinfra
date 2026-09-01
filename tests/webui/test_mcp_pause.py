"""A configured MCP server can be paused without losing it (#206).

The Apps row had two states: configured -- in every prompt, always -- and the trash icon, which
loses the command, the env, the headers and the tool allowlist with it. The middle state is where
the money is: three GitHub servers exposing the *same* fifteen tools cost ~15K tokens a turn to use
one of them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nanoinfra.config.schema import Config, MCPServerConfig
from nanoinfra.webui.mcp_presets_api import McpPresetError, mcp_presets_action


def _stub_provider() -> Any:
    """Enough provider for `from_config`, which builds a real one otherwise."""
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


def _query(name: str) -> dict[str, list[str]]:
    """A query maps a key to a *list*, the way a URL does.

    Passing a plain string made `_query_first` return its first character, so a pause of
    `github-nanoinfraorg` looked like a pause of `g` and answered 404 -- a bug in this file rather
    than in the code under test, and the kind that reads as a real failure.
    """
    return {"name": [name]}


@pytest.fixture
def configured(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Config:
    """One configured server, loaded from memory so a save is observable in the same object."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.tools.mcp_servers["github-nanoinfraorg"] = MCPServerConfig(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_TOKEN": "t"},
        enabled_tools=["list_issues", "search_code"],
    )
    monkeypatch.setattr("nanoinfra.webui.mcp_presets_api.load_config", lambda: config)
    monkeypatch.setattr("nanoinfra.webui.mcp_presets_api.save_config", lambda cfg: None)
    return config


def _row(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for row in payload["presets"]:
        if row["name"] == name:
            return row
    raise AssertionError(f"{name} is not in the payload")


def test_pausing_keeps_everything_and_says_so(configured: Config) -> None:
    payload = mcp_presets_action("pause", _query("github-nanoinfraorg"))

    row = _row(payload, "github-nanoinfraorg")
    assert row["paused"] is True
    assert row["installed"] is True, "paused is not uninstalled"
    # The reason a pause exists rather than a remove: none of this is lost.
    server = configured.tools.mcp_servers["github-nanoinfraorg"]
    assert server.command == "npx"
    assert server.env == {"GITHUB_TOKEN": "t"}
    assert server.enabled_tools == ["list_issues", "search_code"]
    assert server.enabled is False


def test_resuming_puts_it_back(configured: Config) -> None:
    mcp_presets_action("pause", _query("github-nanoinfraorg"))

    payload = mcp_presets_action("resume", _query("github-nanoinfraorg"))

    assert _row(payload, "github-nanoinfraorg")["paused"] is False


def test_a_paused_server_is_dropped_by_the_shared_filter(configured: Config) -> None:
    """The filter lives in `merged_mcp_servers` because the gateway, the mcp-host, the registry
    reload and the loop all read it -- a disabled server one of them still believed in would be a
    host launching something the registry ignores."""
    from nanoinfra.agent.plugins import merged_mcp_servers

    assert "github-nanoinfraorg" in merged_mcp_servers(configured)

    mcp_presets_action("pause", _query("github-nanoinfraorg"))

    assert merged_mcp_servers(configured) == {}


def test_the_loop_that_connects_does_not_get_a_paused_server(configured: Config) -> None:
    """The test above passed while the feature did not work, which is the point of this one.

    It asserted the helper and was *named* after a claim about consumers it never called. The
    consumer that opens the connections is `AgentLoop.from_config`, and it was passing
    `config.tools.mcp_servers` -- the raw dict -- so all three paused servers reconnected on the
    next restart and the operator watched it happen in his own logs.
    """
    from nanoinfra.agent.loop import AgentLoop

    mcp_presets_action("pause", _query("github-nanoinfraorg"))

    loop = AgentLoop.from_config(configured, provider=_stub_provider())

    assert loop._mcp_servers == {}  # pyright: ignore[reportPrivateUsage]


def test_the_loop_still_gets_a_server_that_is_not_paused(configured: Config) -> None:
    """The other half: the filter must not be an empty map for everybody."""
    from nanoinfra.agent.loop import AgentLoop

    loop = AgentLoop.from_config(configured, provider=_stub_provider())

    assert list(loop._mcp_servers) == ["github-nanoinfraorg"]  # pyright: ignore[reportPrivateUsage]


def test_pausing_something_that_is_not_configured_is_a_404(configured: Config) -> None:
    with pytest.raises(McpPresetError) as raised:
        mcp_presets_action("pause", _query("github-absent"))

    assert raised.value.status == 404


def test_a_pause_asks_for_a_restart(configured: Config) -> None:
    """The registry is built at boot, so the change lands on the next start rather than mid-turn.
    Saying so beats a row that looks applied and is not."""
    payload = mcp_presets_action("pause", _query("github-nanoinfraorg"))

    assert payload["requires_restart"] is True


def test_pause_is_not_the_same_verb_as_enable(configured: Config) -> None:
    """`enable` already means *install this preset*. One verb for "configure it" and "put it back
    in the prompt" would make the button's meaning depend on state nobody can see."""
    mcp_presets_action("pause", _query("github-nanoinfraorg"))

    message = mcp_presets_action("resume", _query("github-nanoinfraorg"))["last_action"]["message"]

    assert "Resumed" in message
