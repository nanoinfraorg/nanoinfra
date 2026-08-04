from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanoinfra.bus.events import InboundMessage
from nanoinfra.command.builtin import build_help_text, register_builtin_commands
from nanoinfra.command.router import CommandContext, CommandRouter
from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META, RuntimeContextBlock


def _ctx(tmp_path: Path, content: str) -> CommandContext:
    loop = SimpleNamespace(workspace=tmp_path)
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content=content,
    )
    return CommandContext(
        msg=msg,
        session=None,
        key="websocket:chat-1",
        raw=content,
        loop=loop,
    )


@pytest.mark.asyncio
async def test_no_args_lists_saved_diagrams(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Example"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams")

    response = await router.dispatch(ctx)

    assert response is not None
    assert "Example" in response.content
    assert diagram.id in response.content
    assert "Use `/infradiagrams <name-or-id>`" in response.content


@pytest.mark.asyncio
async def test_no_args_with_no_saved_diagrams(tmp_path: Path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams")

    response = await router.dispatch(ctx)

    assert response is not None
    assert response.content == "No saved diagrams."


@pytest.mark.asyncio
async def test_attach_by_id_falls_through_to_agent_turn(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram = store.create(
        {
            "name": "Web app",
            "targets": ["prod-web-01"],
            "nodes": [{"id": "web", "position": {"x": 0, "y": 0}, "data": {"label": "Web"}}],
            "edges": [],
        }
    )

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, f"/infradiagrams {diagram.id}")

    response = await router.dispatch(ctx)

    # None means "fall through to the normal agent turn" -- same contract as /goal.
    assert response is None
    blocks = ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, RuntimeContextBlock)
    assert block.source == "infradiagram"
    assert "Web app" in block.content
    assert '"id":"web"' in block.content


@pytest.mark.asyncio
async def test_attach_by_name_case_insensitive(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    store.create({"name": "Web App"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams web app")

    response = await router.dispatch(ctx)

    assert response is None
    blocks = ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert "Web App" in blocks[0].content


@pytest.mark.asyncio
async def test_attach_by_id_with_trailing_question(tmp_path: Path) -> None:
    """`/infradiagrams <id> <question>` must still resolve -- the id is a prefix, not the whole query."""
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "vLLM deployment basic"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, f"/infradiagrams {diagram.id} what do you think of this design")

    response = await router.dispatch(ctx)

    assert response is None
    blocks = ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert "vLLM deployment basic" in blocks[0].content


@pytest.mark.asyncio
async def test_attach_by_name_with_spaces_and_trailing_question(tmp_path: Path) -> None:
    """A multi-word name followed by free text must match the name, not fail as one long query."""
    store = DiagramStore(tmp_path)
    store.create({"name": "vLLM deployment basic"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams vLLM deployment basic what do you think of this design")

    response = await router.dispatch(ctx)

    assert response is None
    blocks = ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert "vLLM deployment basic" in blocks[0].content


@pytest.mark.asyncio
async def test_attach_by_name_prefix_requires_word_boundary(tmp_path: Path) -> None:
    """A name must not match as a prefix of a longer, unrelated word (e.g. "Web" inside "Webhooks")."""
    store = DiagramStore(tmp_path)
    store.create({"name": "Web"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams Webhooks are cool")

    response = await router.dispatch(ctx)

    assert response is not None
    assert "No saved diagram matches" in response.content


@pytest.mark.asyncio
async def test_attach_picks_longest_matching_name(tmp_path: Path) -> None:
    """When one saved name is a prefix of another, the longest actual match wins."""
    store = DiagramStore(tmp_path)
    store.create({"name": "Web"})
    longer = store.create({"name": "Web App"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams Web App what do you think")

    response = await router.dispatch(ctx)

    assert response is None
    blocks = ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert longer.name in blocks[0].content


@pytest.mark.asyncio
async def test_attach_unknown_name_returns_not_found(tmp_path: Path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, "/infradiagrams ghost")

    response = await router.dispatch(ctx)

    assert response is not None
    assert "No saved diagram matches" in response.content
    assert RUNTIME_CONTEXT_INPUT_META not in ctx.msg.metadata


@pytest.mark.asyncio
async def test_attach_appends_to_existing_runtime_context_blocks(tmp_path: Path) -> None:
    """A WebUI turn may already carry a block (e.g. a session mention) -- must not clobber it."""
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Example"})

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path, f"/infradiagrams {diagram.id}")
    ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META] = [
        RuntimeContextBlock(source="session_mention", content="pre-existing block")
    ]

    response = await router.dispatch(ctx)

    assert response is None
    blocks = ctx.msg.metadata[RUNTIME_CONTEXT_INPUT_META]
    assert [b.source for b in blocks] == ["session_mention", "infradiagram"]


def test_infradiagrams_command_is_in_help_text() -> None:
    assert "/infradiagrams" in build_help_text()
