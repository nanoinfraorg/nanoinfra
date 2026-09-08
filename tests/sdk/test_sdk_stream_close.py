"""Closing an SDK stream must not drop a queued event (upstream PR 5635).

`SDKStreamEmitter.close()` was synchronous, and a synchronous put on a full queue has nowhere to
go: it evicted the oldest queued event to make room for the sentinel. So a consumer slower than
the agent lost an event -- in practice the terminal `text_completed`, the one the caller is
waiting for.
"""

from __future__ import annotations

import asyncio

from nanoinfra.sdk.streaming import _STREAM_SENTINEL, RunStream, SDKStreamEmitter
from nanoinfra.sdk.types import STREAM_EVENT_TEXT_DELTA, RunResult, StreamEvent


def _delta(text: str) -> StreamEvent:
    return StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta=text)


async def test_closing_a_full_stream_keeps_every_queued_event() -> None:
    queue: asyncio.Queue[StreamEvent | object] = asyncio.Queue(maxsize=2)
    emitter = SDKStreamEmitter(queue)
    await emitter.emit(_delta("first"))
    await emitter.emit(_delta("second"))

    # A consumer that has read nothing yet, which is the whole failure: the close has to wait for
    # room rather than make room.
    closing = asyncio.create_task(emitter.close())
    await asyncio.sleep(0)

    oldest = await asyncio.wait_for(queue.get(), 1)
    assert isinstance(oldest, StreamEvent), "the close replaced a queued event with the sentinel"
    assert oldest.delta == "first", "the close evicted the oldest queued event"

    rest = [await asyncio.wait_for(queue.get(), 1) for _ in range(2)]
    await asyncio.wait_for(closing, 1)

    assert isinstance(rest[0], StreamEvent)
    assert rest[0].delta == "second"
    assert rest[1] is _STREAM_SENTINEL


async def test_a_second_close_is_still_a_no_op() -> None:
    """Idempotent, and it must not queue a second sentinel behind the first."""
    queue: asyncio.Queue[StreamEvent | object] = asyncio.Queue(maxsize=2)
    emitter = SDKStreamEmitter(queue)

    await emitter.close()
    await emitter.close()

    assert await asyncio.wait_for(queue.get(), 1) is _STREAM_SENTINEL
    assert queue.empty()


async def test_abandoning_a_full_stream_does_not_strand_the_run() -> None:
    """Waiting for room means the abandon path has to be what makes the room."""
    queue: asyncio.Queue[StreamEvent | object] = asyncio.Queue(maxsize=2)
    emitter = SDKStreamEmitter(queue)
    filled = asyncio.Event()

    async def run() -> RunResult:
        try:
            await emitter.emit(_delta("first"))
            await emitter.emit(_delta("second"))
            filled.set()
            return RunResult(content="done")
        finally:
            await emitter.close()

    task = asyncio.create_task(run())
    stream = RunStream(task, queue)
    await asyncio.wait_for(filled.wait(), 1)

    await asyncio.wait_for(stream.aclose(), 1)

    assert task.done()
