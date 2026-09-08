"""Per-connection outbound delivery guarantees for the WebSocket channel.

These tests exist because the fanout they cover used to be a bare sequential
``await`` over the subscriber set: one client that stopped reading its socket
held the whole loop, and every other client on that chat waited behind it. The
frames kept arriving in the meantime, so the backlog grew in server memory with
no ceiling. Streaming deltas are the highest-frequency frame the channel sends,
which makes that the ordinary case rather than an exotic one.

Every test here therefore asserts one of four things, and nothing else:

* isolation -- a stalled connection delays only itself,
* a ceiling -- a backlog is bounded in frames and in bytes, and the connection
  that grew it is dropped rather than the frames it was owed,
* no leak -- the writer task and its queue die with the connection on every
  close path, including a send that raises and a server shutdown,
* order -- frames reach one connection in the order they were produced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from nanoinfra.bus.events import OutboundMessage
from nanoinfra.channels.websocket import outbound_delivery
from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.webui.gateway_services import build_gateway_services
from nanoinfra.webui.transcript import read_transcript_lines

_PORT = 29881


class _RecordingConnection:
    """A socket that can be told to stall, so a stall is a test input.

    A real ``ServerConnection`` stalls when the peer stops reading and the OS
    send buffer fills, which is not something a unit test can arrange. This
    stands in for it: ``block_first_send`` holds the first ``send`` open until
    the test releases it, which is exactly the shape of a suspended browser tab.

    ``max_active_sends`` is the order assertion in numeric form. One writer task
    per connection means at most one ``send`` may ever be in flight on it; two
    concurrent sends would be free to complete in either order, and the client
    would render a transcript that never happened.
    """

    def __init__(
        self,
        *,
        block_first_send: bool = False,
        block_close: bool = False,
    ) -> None:
        self.block_first_send = block_first_send
        self.block_close = block_close
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.send_cancelled = asyncio.Event()
        self.closed = asyncio.Event()
        self.release_close = asyncio.Event()
        self.sent: asyncio.Queue[str] = asyncio.Queue()
        self.close_calls: list[tuple[int, str]] = []
        self.transport = MagicMock()
        self.send_calls = 0
        self.active_sends = 0
        self.max_active_sends = 0

    async def send(self, raw: str) -> None:
        self.send_calls += 1
        call_number = self.send_calls
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        self.send_started.set()
        try:
            if self.block_first_send and call_number == 1:
                await self.release_send.wait()
            await self.sent.put(raw)
        except asyncio.CancelledError:
            self.send_cancelled.set()
            raise
        finally:
            self.active_sends -= 1

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))
        self.closed.set()
        if self.block_close:
            await self.release_close.wait()


def _channel(bus: Any = None) -> WebSocketChannel:
    bus = bus if bus is not None else MagicMock()
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": _PORT,
        "path": "/ws",
        "websocketRequiresToken": False,
    }
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


def _message(chat_id: str, text: str) -> OutboundMessage:
    return OutboundMessage(channel="websocket", chat_id=chat_id, content=text)


async def _next_text(connection: _RecordingConnection) -> str:
    raw = await asyncio.wait_for(connection.sent.get(), timeout=1)
    return str(json.loads(raw)["text"])


async def _wait_until_untracked(
    channel: WebSocketChannel,
    connection: _RecordingConnection,
) -> None:
    while channel._delivery.tracks(cast(Any, connection)):
        await asyncio.sleep(0)


async def _wait_for_retirements(channel: WebSocketChannel) -> None:
    while channel._delivery.retiring():
        await asyncio.sleep(0)


def _delivery_tasks() -> list[asyncio.Task[Any]]:
    """Every task this module's delivery layer is allowed to own, by name.

    A leaked writer is invisible in the channel's own dictionaries -- it has
    already dropped its reference to them -- so a leak is only provable against
    the event loop's own task set.
    """
    prefixes = ("websocket-outbound-", "websocket-retire-")
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(prefixes) and not task.done()
    ]


# -- Isolation: a stalled client delays only itself --------------------------


@pytest.mark.asyncio
async def test_a_stalled_client_does_not_block_another_chats_client() -> None:
    channel = _channel()
    slow = _RecordingConnection(block_first_send=True)
    healthy = _RecordingConnection()
    channel._attach(cast(Any, slow), "chat-slow")
    channel._attach(cast(Any, healthy), "chat-healthy")

    try:
        await asyncio.wait_for(channel.send(_message("chat-slow", "slow")), timeout=1)
        await asyncio.wait_for(slow.send_started.wait(), timeout=1)

        await asyncio.wait_for(channel.send(_message("chat-healthy", "healthy")), timeout=1)

        assert await _next_text(healthy) == "healthy"
        assert slow.sent.empty()
    finally:
        slow.release_send.set()
        await channel._cleanup_connection(cast(Any, slow))
        await channel._cleanup_connection(cast(Any, healthy))


@pytest.mark.asyncio
async def test_a_stalled_client_does_not_block_the_same_chats_fanout() -> None:
    """The load-bearing one: both connections are on the chat being fanned out.

    Against the sequential fanout this is the whole defect in five lines. The
    subscriber set is iterated in an arbitrary order, so ``slow`` may be reached
    first, and then ``await connection.send`` never returns -- ``channel.send``
    itself never returns, and ``healthy`` is owed a frame that is never written.
    """
    channel = _channel()
    slow = _RecordingConnection(block_first_send=True)
    healthy = _RecordingConnection()
    channel._attach(cast(Any, slow), "chat-shared")
    channel._attach(cast(Any, healthy), "chat-shared")

    try:
        await asyncio.wait_for(channel.send(_message("chat-shared", "hello")), timeout=1)
        await asyncio.wait_for(slow.send_started.wait(), timeout=1)

        assert await _next_text(healthy) == "hello"
        assert slow.sent.empty()
    finally:
        slow.release_send.set()
        await channel._cleanup_connection(cast(Any, slow))
        await channel._cleanup_connection(cast(Any, healthy))


@pytest.mark.asyncio
async def test_a_stalled_client_does_not_block_a_streaming_delta_fanout() -> None:
    """Same isolation, on the frame the channel actually sends most of."""
    channel = _channel()
    slow = _RecordingConnection(block_first_send=True)
    healthy = _RecordingConnection()
    channel._attach(cast(Any, slow), "chat-stream")
    channel._attach(cast(Any, healthy), "chat-stream")

    try:
        for index in range(4):
            await asyncio.wait_for(
                channel.send_delta("chat-stream", f"chunk-{index}", stream_id="s1"),
                timeout=1,
            )
            assert await _next_text(healthy) == f"chunk-{index}"
        assert slow.sent.empty()
    finally:
        slow.release_send.set()
        await channel._cleanup_connection(cast(Any, slow))
        await channel._cleanup_connection(cast(Any, healthy))


# -- The ceiling: bounded in frames, bounded in bytes ------------------------


@pytest.mark.asyncio
async def test_a_full_frame_queue_drops_only_the_stalled_client(monkeypatch: Any) -> None:
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_FRAMES", 2)
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 1024 * 1024)
    channel = _channel()
    slow = _RecordingConnection(block_first_send=True)
    healthy = _RecordingConnection()
    channel._attach(cast(Any, slow), "chat-shared")
    channel._attach(cast(Any, healthy), "chat-shared")

    try:
        for index in range(4):
            await channel.send(_message("chat-shared", str(index)))
            assert await _next_text(healthy) == str(index)
            if index == 0:
                await asyncio.wait_for(slow.send_started.wait(), timeout=1)

        await asyncio.wait_for(slow.closed.wait(), timeout=1)
        await asyncio.wait_for(_wait_until_untracked(channel, slow), timeout=1)

        assert slow.close_calls == [(1013, "outbound queue full")]
        assert slow not in channel._conn_chats
        assert cast(Any, slow) not in channel._subs["chat-shared"]
        assert cast(Any, healthy) in channel._subs["chat-shared"]
    finally:
        slow.release_send.set()
        await channel._cleanup_connection(cast(Any, slow))
        await channel._cleanup_connection(cast(Any, healthy))


@pytest.mark.asyncio
async def test_the_byte_budget_bounds_a_backlog_of_few_large_frames(
    monkeypatch: Any,
) -> None:
    """A frame count alone is not a memory bound.

    256 frames of a megabyte each is a quarter of a gigabyte held for one tab.
    The byte budget is what actually caps the retained stream, so it has to be
    able to fire while the frame queue is nowhere near full.
    """
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 600)
    channel = _channel()
    connection = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, connection), "chat-large")

    try:
        await channel.send(_message("chat-large", "x" * 200))
        await asyncio.wait_for(connection.send_started.wait(), timeout=1)
        # In flight, so still counted: the writer only credits the budget back
        # once the frame has left.
        await channel.send(_message("chat-large", "y" * 200))
        assert not connection.closed.is_set()

        await channel.send(_message("chat-large", "z" * 200))

        await asyncio.wait_for(connection.closed.wait(), timeout=1)
        await asyncio.wait_for(_wait_until_untracked(channel, connection), timeout=1)
        assert connection.close_calls == [(1013, "outbound queue full")]
        assert channel._delivery.queue_depth(cast(Any, connection)) == 0
    finally:
        connection.release_send.set()
        await channel._cleanup_connection(cast(Any, connection))


@pytest.mark.asyncio
async def test_one_oversized_frame_is_refused_before_it_is_ever_queued(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 64)
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-oversized")

    try:
        await channel.send(_message("chat-oversized", "x" * 256))

        await asyncio.wait_for(connection.closed.wait(), timeout=1)
        await asyncio.wait_for(_wait_until_untracked(channel, connection), timeout=1)
        assert connection.send_calls == 0
        assert connection.close_calls == [(1013, "outbound queue full")]
    finally:
        await channel._cleanup_connection(cast(Any, connection))


@pytest.mark.asyncio
async def test_a_send_that_never_completes_is_bounded_by_the_send_timeout(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_SEND_TIMEOUT_S", 0.01)
    channel = _channel()
    slow = _RecordingConnection(block_first_send=True)
    healthy = _RecordingConnection()
    channel._attach(cast(Any, slow), "chat-shared")
    channel._attach(cast(Any, healthy), "chat-shared")

    try:
        await channel.send(_message("chat-shared", "hello"))

        assert await _next_text(healthy) == "hello"
        await asyncio.wait_for(slow.closed.wait(), timeout=1)
        await asyncio.wait_for(_wait_until_untracked(channel, slow), timeout=1)
        assert slow.close_calls == [(1013, "outbound send timeout")]
        assert slow.send_cancelled.is_set()
        assert slow not in channel._conn_chats
        assert cast(Any, healthy) in channel._subs["chat-shared"]
    finally:
        slow.release_send.set()
        await channel._cleanup_connection(cast(Any, slow))
        await channel._cleanup_connection(cast(Any, healthy))


@pytest.mark.asyncio
async def test_a_close_that_never_completes_is_bounded_and_aborts_the_transport(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 64)
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_CLOSE_TIMEOUT_S", 0.01)
    channel = _channel()
    connection = _RecordingConnection(block_close=True)
    channel._attach(cast(Any, connection), "chat-close-timeout")

    await channel.send(_message("chat-close-timeout", "x" * 256))
    await asyncio.wait_for(connection.closed.wait(), timeout=1)
    await asyncio.wait_for(_wait_until_untracked(channel, connection), timeout=1)

    connection.transport.abort.assert_called_once_with()
    assert connection not in channel._conn_chats
    assert _delivery_tasks() == []


# -- No leak: the writer dies with its connection, on every path -------------


@pytest.mark.asyncio
async def test_cleanup_cancels_a_blocked_writer_and_drops_its_backlog() -> None:
    channel = _channel()
    connection = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, connection), "chat-cleanup")
    channel._conn_default[cast(Any, connection)] = "chat-cleanup"

    await channel.send(_message("chat-cleanup", "one"))
    await asyncio.wait_for(connection.send_started.wait(), timeout=1)
    await channel.send(_message("chat-cleanup", "two"))

    await asyncio.wait_for(channel._cleanup_connection(cast(Any, connection)), timeout=1)
    # Idempotent: the connection loop's finally and a retirement can both reach
    # here for the same socket.
    await channel._cleanup_connection(cast(Any, connection))

    assert connection.send_cancelled.is_set()
    assert connection not in channel._conn_chats
    assert connection not in channel._conn_default
    assert not channel._delivery.tracks(cast(Any, connection))
    assert channel._subs == {}
    assert _delivery_tasks() == []


@pytest.mark.asyncio
async def test_a_send_that_raises_retires_that_connection_and_leaves_no_task() -> None:
    channel = _channel()
    connection = AsyncMock()
    connection.send.side_effect = RuntimeError("unexpected")
    channel._attach(connection, "chat-raise")
    healthy = _RecordingConnection()
    channel._attach(cast(Any, healthy), "chat-raise")

    await channel.send(_message("chat-raise", "hello"))

    assert await _next_text(healthy) == "hello"
    while connection in channel._conn_chats:
        await asyncio.sleep(0)
    connection.close.assert_awaited_once_with(code=1011, reason="outbound send failed")
    assert not channel._delivery.tracks(connection)
    assert _delivery_tasks() == []

    await channel._cleanup_connection(cast(Any, healthy))


@pytest.mark.asyncio
async def test_stop_cancels_every_connection_writer() -> None:
    channel = _channel()
    connection = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, connection), "chat-stop")

    await channel.send(_message("chat-stop", "one"))
    await asyncio.wait_for(connection.send_started.wait(), timeout=1)
    await asyncio.wait_for(channel.stop(), timeout=1)

    assert connection.send_cancelled.is_set()
    assert not channel._delivery.tracks(cast(Any, connection))
    assert not channel._delivery.retiring()
    assert channel._subs == {}
    assert _delivery_tasks() == []


@pytest.mark.asyncio
async def test_stop_cancels_writers_before_it_waits_on_the_server_task() -> None:
    """Order matters: the server task can be waiting on a blocked handler.

    A shutdown that awaits the listener first and only then cancels the writers
    is a shutdown that hangs for as long as the stalled client does.
    """
    channel = _channel()
    connection = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, connection), "chat-stop-order")
    channel._running = True
    channel._stop_event = asyncio.Event()

    await channel.send(_message("chat-stop-order", "one"))
    await asyncio.wait_for(connection.send_started.wait(), timeout=1)

    async def _server_shutdown() -> None:
        assert channel._stop_event is not None
        await channel._stop_event.wait()
        await connection.send_cancelled.wait()

    channel._server_task = asyncio.create_task(_server_shutdown())

    await asyncio.wait_for(channel.stop(), timeout=1)

    assert connection.send_cancelled.is_set()
    assert channel._server_task is None
    assert _delivery_tasks() == []


@pytest.mark.asyncio
async def test_a_failing_cleanup_still_retires_the_connection(
    monkeypatch: Any,
) -> None:
    """A retirement that raises would leave the retire task's exception unread.

    That is the asyncio failure nobody sees: a dead task whose exception is only
    reported at garbage-collection time, with no connection named.
    """
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 64)
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-cleanup-failure")

    def _boom(_conn: Any) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(channel, "_discard_connection_state", _boom)

    await channel.send(_message("chat-cleanup-failure", "x" * 256))
    await asyncio.wait_for(_wait_for_retirements(channel), timeout=1)

    assert connection.close_calls == [(1013, "outbound queue full")]
    assert not channel._delivery.tracks(cast(Any, connection))
    assert _delivery_tasks() == []


# -- Order: one writer, one in-flight send, produced order preserved ---------


@pytest.mark.asyncio
async def test_frames_reach_one_connection_in_the_order_they_were_produced() -> None:
    channel = _channel()
    connection = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, connection), "chat-order")

    try:
        await channel.send(_message("chat-order", "one"))
        await asyncio.wait_for(connection.send_started.wait(), timeout=1)
        await channel.send(_message("chat-order", "two"))
        await channel.send(_message("chat-order", "three"))

        connection.release_send.set()

        assert [await _next_text(connection) for _ in range(3)] == ["one", "two", "three"]
        assert connection.max_active_sends == 1
    finally:
        connection.release_send.set()
        await channel._cleanup_connection(cast(Any, connection))


@pytest.mark.asyncio
async def test_a_long_backlog_drains_in_order_under_one_writer() -> None:
    channel = _channel()
    connection = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, connection), "chat-order-long")

    try:
        await channel.send_delta("chat-order-long", "0", stream_id="s1")
        await asyncio.wait_for(connection.send_started.wait(), timeout=1)
        for index in range(1, 64):
            await channel.send_delta("chat-order-long", str(index), stream_id="s1")

        connection.release_send.set()

        received = [await _next_text(connection) for _ in range(64)]
        assert received == [str(index) for index in range(64)]
        assert connection.max_active_sends == 1
    finally:
        connection.release_send.set()
        await channel._cleanup_connection(cast(Any, connection))


# -- A retired connection stays retired --------------------------------------


@pytest.mark.asyncio
async def test_attach_cannot_re_register_a_closing_connection() -> None:
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-before-cleanup")
    await channel._cleanup_connection(cast(Any, connection))

    channel._attach(cast(Any, connection), "chat-after-cleanup")

    assert not channel._delivery.tracks(cast(Any, connection))
    assert connection not in channel._conn_chats
    assert "chat-after-cleanup" not in channel._subs


@pytest.mark.asyncio
async def test_a_frame_for_a_retired_connection_recreates_no_state() -> None:
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-stale")
    await channel._cleanup_connection(cast(Any, connection))

    await channel._safe_send_to(cast(Any, connection), "stale")

    assert connection.send_calls == 0
    assert not channel._delivery.tracks(cast(Any, connection))
    assert _delivery_tasks() == []


@pytest.mark.asyncio
async def test_an_inbound_envelope_cannot_revive_a_retired_connection() -> None:
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-before-cleanup")
    await channel._cleanup_connection(cast(Any, connection))

    await channel._dispatch_envelope(cast(Any, connection), "client", {"type": "new_chat"})

    assert not channel._delivery.tracks(cast(Any, connection))
    assert connection.send_calls == 0
    assert channel._subs == {}


@pytest.mark.asyncio
async def test_a_reconnecting_object_is_admitted_again_by_the_connection_loop() -> None:
    """Retirement is per socket, not a permanent ban on an object identity.

    ``_retired`` is a ``WeakSet``, so a dead connection leaves it by garbage
    collection. A live object that the loop admits again -- which is what a
    reused stub in a test is, and what a pooled object would be in production --
    has to be usable, or the WeakSet becomes a slow leak of refused clients.
    """
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-first")
    await channel._cleanup_connection(cast(Any, connection))
    assert not channel._delivery.tracks(cast(Any, connection))

    channel._delivery.readmit(cast(Any, connection))
    channel._attach(cast(Any, connection), "chat-second")

    try:
        assert channel._delivery.tracks(cast(Any, connection))
        await channel.send(_message("chat-second", "welcome back"))
        assert await _next_text(connection) == "welcome back"
    finally:
        await channel._cleanup_connection(cast(Any, connection))


# -- What a dropped connection costs, measured -------------------------------


@pytest.mark.asyncio
async def test_nothing_is_lost_when_a_stalled_client_is_dropped_mid_stream(
    monkeypatch: Any,
) -> None:
    """The justification for dropping a connection instead of a frame.

    Dropping a streaming delta would leave the client rendering an answer with a
    hole in it, and nothing would ever fill the hole -- the frame is gone and the
    client has no way to know it existed. Dropping the connection is visible and
    repairable, because every frame is persisted to the session transcript
    *before* delivery is attempted. This test is that claim in code: overflow the
    queue mid-stream, then read back the transcript and find every delta,
    including the ones the socket never carried.
    """
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_FRAMES", 2)
    channel = _channel()
    chat_id = "chat-transcript-recovery"
    stalled = _RecordingConnection(block_first_send=True)
    channel._attach(cast(Any, stalled), chat_id)

    produced = [f"delta-{index}" for index in range(6)]
    for index, delta in enumerate(produced):
        await channel.send_delta(chat_id, delta, stream_id="s1")
        if index == 0:
            await asyncio.wait_for(stalled.send_started.wait(), timeout=1)

    await asyncio.wait_for(stalled.closed.wait(), timeout=1)
    await asyncio.wait_for(_wait_until_untracked(channel, stalled), timeout=1)
    stalled.release_send.set()

    assert stalled.close_calls == [(1013, "outbound queue full")]
    # It really did lose frames off the socket -- otherwise the assertion below
    # would be proving nothing.
    assert stalled.sent.qsize() < len(produced)
    persisted = [
        line["text"]
        for line in read_transcript_lines(f"websocket:{chat_id}")
        if line.get("event") == "delta"
    ]
    assert persisted == produced


# -- No leak, continued: the idle case and the overlapping case --------------


@pytest.mark.asyncio
async def test_an_idle_connection_holds_no_writer_task() -> None:
    """The strongest anti-leak property: an idle connection owns nothing.

    The writer returns when its queue empties rather than parking on
    ``queue.get()``, so between turns there is no task to leak, no task to
    cancel, and nothing holding a reference to a connection that may be about to
    close.
    """
    channel = _channel()
    connection = _RecordingConnection()
    channel._attach(cast(Any, connection), "chat-idle")

    try:
        await channel.send(_message("chat-idle", "one"))
        assert await _next_text(connection) == "one"
        assert _delivery_tasks() == []

        await channel.send(_message("chat-idle", "two"))
        assert await _next_text(connection) == "two"
        assert _delivery_tasks() == []
    finally:
        await channel._cleanup_connection(cast(Any, connection))


@pytest.mark.asyncio
async def test_a_peer_that_hung_up_is_retired_without_a_close_handshake() -> None:
    """The ordinary disconnect: no close frame is owed to a socket already gone."""
    channel = _channel()
    gone = AsyncMock()
    gone.send.side_effect = ConnectionClosed(Close(1006, ""), Close(1006, ""), True)
    channel._attach(gone, "chat-gone")

    await channel.send(_message("chat-gone", "hello"))
    while gone in channel._conn_chats:
        await asyncio.sleep(0)

    gone.close.assert_not_awaited()
    assert not channel._delivery.tracks(gone)
    assert channel._subs == {}
    assert _delivery_tasks() == []


@pytest.mark.asyncio
async def test_cleanup_may_overlap_a_retirement_that_is_still_closing(
    monkeypatch: Any,
) -> None:
    """Two teardown paths for one socket must not fight over it.

    A retirement started by the writer can still be inside ``connection.close``
    when the connection loop's own ``finally`` reaches cleanup for the same
    socket. Both have to be safe, and the queue and its writer have to end up
    gone exactly once.
    """
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 64)
    channel = _channel()
    connection = _RecordingConnection(block_close=True)
    channel._attach(cast(Any, connection), "chat-retire-race")

    await channel.send(_message("chat-retire-race", "x" * 256))
    await asyncio.wait_for(connection.closed.wait(), timeout=1)

    await asyncio.wait_for(channel._cleanup_connection(cast(Any, connection)), timeout=1)
    connection.release_close.set()
    await asyncio.wait_for(_wait_for_retirements(channel), timeout=1)

    assert connection.close_calls == [(1013, "outbound queue full")]
    assert not channel._delivery.tracks(cast(Any, connection))
    assert connection not in channel._conn_chats
    assert channel._subs == {}
    assert _delivery_tasks() == []


@pytest.mark.asyncio
async def test_stop_waits_for_a_retirement_that_is_still_closing(
    monkeypatch: Any,
) -> None:
    """A shutdown that returns while a socket is still closing has not stopped."""
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_QUEUE_MAX_BYTES", 64)
    monkeypatch.setattr(outbound_delivery, "_OUTBOUND_CLOSE_TIMEOUT_S", 0.05)
    channel = _channel()
    connection = _RecordingConnection(block_close=True)
    channel._attach(cast(Any, connection), "chat-stop-retire")

    await channel.send(_message("chat-stop-retire", "x" * 256))
    await asyncio.wait_for(connection.closed.wait(), timeout=1)

    await asyncio.wait_for(channel.stop(), timeout=1)

    assert not channel._delivery.has_work()
    assert channel._subs == {}
    assert _delivery_tasks() == []
