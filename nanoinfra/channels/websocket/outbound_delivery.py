"""Deliver outbound frames per connection, so one stalled client stalls alone.

The fanout this replaces was a sequential ``await`` over the subscriber set --
twelve such loops, one per frame type. Every one of them had the same three
defects, and they compound:

1. **One slow client held every other client.** The loop awaited
   ``connection.send`` for subscriber *n* before it looked at *n+1*. A browser
   tab that had been suspended by the OS, or a client on a link that had gone
   away without a FIN, stops reading; the kernel send buffer fills; the ``await``
   does not return. Everyone else on that chat waited behind it, and the task
   doing the fanout was the agent's own outbound dispatch, so the delay was not
   even confined to the channel.
2. **The backlog had no ceiling.** Frames kept being produced while the loop was
   stuck, and they accumulated in the event loop's own structures with no bound
   in frames or in bytes. Streaming deltas are the highest-frequency frame this
   channel sends, so the ordinary case -- a long answer streaming to a tab
   somebody left in the background -- was the case that grew memory.
3. **There was no deadline.** Nothing ever decided that a connection had had
   long enough. A half-open socket held its slot until TCP gave up, which can be
   minutes.

The fix is one bounded queue and one writer task per connection. A producer
enqueues and returns; the writer owns the socket. Three properties follow, and
each one is a test in ``tests/test_websocket_outbound_delivery.py``:

**Isolation.** ``enqueue`` never awaits physical I/O, so a producer's cost is
the same whether the peer is reading or not.

**A ceiling, paid by the connection rather than by the frame.** When a queue
would overflow -- in frames or in bytes -- this module drops *the connection*,
with close code 1013, and never drops a frame from a queue it accepted. That
choice is the whole reason the module can be trusted with a transcript: every
frame that matters is written to the session transcript before it is handed
here, so a dropped connection reconnects and replays, while a dropped delta
would leave a client rendering text with a hole in it that nothing would ever
fill. A visibly wrong transcript is worse than a disconnect, because a
disconnect is something the client can see and repair.

The frames that are *not* persisted -- ``goal_state``, ``goal_status``,
``session_updated``, ``runtime_model_updated``, ``diagram_updated``,
``turn_model_updated`` and the control events -- are recoverable for a different
reason: every one of them says "this state changed, read it again". A reconnect
runs ``_hydrate_after_subscribe`` and the client refetches through the HTTP
routes, so losing one costs a refresh rather than a fact. That is the whole
inventory; there is no frame on this wire whose only copy is the one in a queue.

**Order.** At most one writer task exists per connection, and it drains its
queue in a single sequential loop, so exactly one ``send`` is ever in flight and
FIFO order is the order the frames were produced in. The writer exits when the
queue empties rather than parking on ``queue.get()``, which is what keeps an
idle connection from holding a task at all; a frame that arrives during the last
send restarts it from the writer's own ``finally``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

# Outbound delivery is isolated per connection. A bounded queue keeps a slow or
# suspended terminal from retaining an unbounded stream in server memory.
#
# The two bounds are not redundant. 256 frames caps a flood of small deltas;
# 8 MB caps a handful of frames that each carry a large answer or a media
# manifest, which would otherwise sit far below the frame count while holding
# hundreds of megabytes. Either one firing means the same thing: this connection
# is not keeping up.
_OUTBOUND_QUEUE_MAX_FRAMES = 256
_OUTBOUND_QUEUE_MAX_BYTES = 8 * 1024 * 1024

# 10 s is a deadline for one frame leaving, not for a whole answer. A socket
# whose peer has stopped reading gives no error at all, so without this the slot
# is held until TCP times out.
_OUTBOUND_SEND_TIMEOUT_S = 10.0

# The close handshake gets 1 s, then the transport is aborted. A client that is
# not reading frames is unlikely to read the close frame either, and waiting for
# it would reintroduce the stall inside the teardown.
_OUTBOUND_CLOSE_TIMEOUT_S = 1.0

_CLOSE_TRY_AGAIN_LATER = 1013
_CLOSE_INTERNAL_ERROR = 1011


@dataclass(slots=True)
class _OutboundFrame:
    """One serialised frame, with the byte cost it is charged for.

    ``utf8_bytes`` is measured once, at enqueue, and credited back when the
    frame leaves. Measuring it again at drain time would let a rounding
    difference leak budget on every frame.
    """

    raw: str
    utf8_bytes: int
    label: str


@dataclass(slots=True)
class _ConnectionOutbound:
    """Everything one connection's delivery needs, and nothing shared.

    ``closing`` is the single latch that makes teardown idempotent: it is set
    before any await in every retirement path, so a second path finds it already
    set and does nothing rather than closing a socket twice or starting a second
    writer for a connection that is going away.
    """

    queue: asyncio.Queue[_OutboundFrame]
    buffered_bytes: int = 0
    writer: asyncio.Task[None] | None = None
    closing: bool = False


class OutboundDelivery:
    """Own the per-connection send queues, their writer tasks, and retirement.

    Constructed with a *retire* callback rather than a reference to the channel.
    The callback is the channel's own connection cleanup, and keeping it a
    callback is what lets this module stay ignorant of subscription sets,
    upload sessions and WebUI token bookkeeping -- it knows that a connection is
    finished and that somebody else knows what that means.
    """

    def __init__(
        self,
        *,
        logger: Any,
        retire: Callable[[ServerConnection], Awaitable[None]],
    ) -> None:
        self._logger = logger
        self._retire = retire
        self._state: dict[ServerConnection, _ConnectionOutbound] = {}
        self._retire_tasks: set[asyncio.Task[None]] = set()
        #: Connections that have been retired and must not come back.
        #:
        #: A ``WeakSet`` because the alternative is a set that grows for the
        #: lifetime of the process: every connection a gateway ever closed would
        #: stay named here. Weak references let a closed socket leave on
        #: collection, which is exactly when the question "may this object be
        #: registered again" stops being askable.
        self._retired: WeakSet[ServerConnection] = WeakSet()

    # -- Registration --------------------------------------------------------

    def register(self, connection: ServerConnection) -> bool:
        """Give *connection* a queue, unless it has already been retired.

        Returns False for a retired connection, and the caller must then treat
        it as gone. This is the guard that stops a closing socket being revived:
        teardown and a fanout can be in flight at the same time -- the writer
        retires the connection while another task is still subscribing it to a
        chat -- and without the check the subscription would recreate the state
        the retirement had just dropped, leaving a queue with no writer and a
        subscriber set naming a dead socket.
        """
        if connection in self._retired:
            return False
        self._state.setdefault(
            connection,
            _ConnectionOutbound(asyncio.Queue(maxsize=_OUTBOUND_QUEUE_MAX_FRAMES)),
        )
        return True

    def readmit(self, connection: ServerConnection) -> None:
        """Forget that *connection* was retired, at the start of its loop.

        Only the connection handler may call this, and only before the socket
        has been used. It exists so retirement is a property of a socket rather
        than a permanent ban on an object identity -- which matters for any
        transport that hands back a pooled connection object, and for tests that
        reuse a stub.
        """
        self._retired.discard(connection)

    def tracks(self, connection: ServerConnection) -> bool:
        """Whether *connection* still has delivery state."""
        return connection in self._state

    def retiring(self) -> bool:
        """Whether any retirement is still in flight."""
        return bool(self._retire_tasks)

    def has_work(self) -> bool:
        """Whether anything here would leak if the channel stopped now."""
        return bool(self._state) or bool(self._retire_tasks)

    def connections(self) -> tuple[ServerConnection, ...]:
        """A snapshot of the tracked connections, safe to iterate while closing."""
        return tuple(self._state)

    def queue_depth(self, connection: ServerConnection) -> int:
        """How many frames are waiting for *connection*. For tests and logs."""
        state = self._state.get(connection)
        return 0 if state is None else state.queue.qsize()

    # -- Enqueue -------------------------------------------------------------

    async def fanout(
        self,
        connections: Iterable[ServerConnection],
        raw: str,
        *,
        label: str = "",
    ) -> None:
        """Hand one frame to every connection in *connections*.

        This is the loop the defect lived in, and it is now a loop over
        enqueues. It still returns in bounded time for every caller, because
        ``enqueue`` does no socket I/O.
        """
        for connection in connections:
            await self.enqueue(connection, raw, label=label)

    async def enqueue(
        self,
        connection: ServerConnection,
        raw: str,
        *,
        label: str = "",
    ) -> None:
        """Queue one frame without letting this connection block other clients.

        An untracked or closing connection is a no-op rather than an error: a
        fanout snapshots its subscriber set, and a connection can retire between
        the snapshot and this call. Recreating state here would resurrect it.
        """
        state = self._state.get(connection)
        if state is None or state.closing:
            return
        utf8_bytes = len(raw.encode("utf-8"))
        if state.queue.full() or state.buffered_bytes + utf8_bytes > _OUTBOUND_QUEUE_MAX_BYTES:
            self._logger.warning(
                "disconnecting slow WebSocket connection: outbound queue full "
                "({} frames, {} bytes){}",
                state.queue.qsize(),
                state.buffered_bytes,
                label,
            )
            self._schedule_retirement(
                connection,
                state,
                close_connection=True,
                close_code=_CLOSE_TRY_AGAIN_LATER,
                close_reason="outbound queue full",
            )
            await asyncio.sleep(0)
            return
        try:
            state.queue.put_nowait(_OutboundFrame(raw, utf8_bytes, label))
        except asyncio.QueueFull:
            # Unreachable through the check above while this coroutine holds the
            # loop, and kept anyway: `put_nowait` raising is the queue's own
            # answer, and losing a frame because two producers agreed the queue
            # had room would be exactly the silent corruption this design
            # refuses.
            self._schedule_retirement(
                connection,
                state,
                close_connection=True,
                close_code=_CLOSE_TRY_AGAIN_LATER,
                close_reason="outbound queue full",
            )
            await asyncio.sleep(0)
            return
        state.buffered_bytes += utf8_bytes
        self._start_writer(connection, state)
        # Give an idle writer a chance to start without waiting for physical
        # I/O. One yield is enough: `create_task` schedules the writer before
        # this coroutine's own resumption, so a peer that is reading normally
        # has its frame on the socket by the time the producer continues.
        await asyncio.sleep(0)

    # -- The writer ----------------------------------------------------------

    def _start_writer(
        self,
        connection: ServerConnection,
        state: _ConnectionOutbound,
    ) -> None:
        """Ensure exactly one writer task is draining *connection*.

        "Exactly one" is the ordering guarantee. A second writer would take the
        next frame off the queue while the first was still awaiting the previous
        one, and two concurrent ``send`` calls may complete in either order --
        which on a stream of deltas means a transcript the model never produced.
        """
        if state.closing or (state.writer is not None and not state.writer.done()):
            return
        state.writer = asyncio.create_task(
            self._drain(connection, state),
            name=f"websocket-outbound-{id(connection):x}",
        )

    async def _drain(
        self,
        connection: ServerConnection,
        state: _ConnectionOutbound,
    ) -> None:
        """Write queued frames to one socket until the queue is empty.

        Returning on an empty queue rather than awaiting ``queue.get()`` is
        deliberate: an idle connection then owns no task, so there is nothing to
        leak between turns and nothing to cancel on shutdown. The ``finally``
        closes the race that creates -- a frame enqueued after the emptiness
        check but before this task is done is picked up by a fresh writer.

        Every failure mode ends the same way: retire this connection and return.
        None of them re-raise, because this task's exception would be nobody's
        to catch, and an unretrieved task exception is reported at collection
        time with no connection named.
        """
        current = asyncio.current_task()
        cancelled = False
        try:
            while not state.closing:
                try:
                    frame = state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    async with asyncio.timeout(_OUTBOUND_SEND_TIMEOUT_S):
                        await connection.send(frame.raw)
                except asyncio.CancelledError:
                    # Teardown cancelled this writer. The queue is discarded by
                    # whoever cancelled; re-raise so the cancellation is not
                    # swallowed and the awaiting closer actually completes.
                    raise
                except TimeoutError:
                    self._logger.warning("connection send timed out{}", frame.label)
                    self._schedule_retirement(
                        connection,
                        state,
                        close_connection=True,
                        close_code=_CLOSE_TRY_AGAIN_LATER,
                        close_reason="outbound send timeout",
                    )
                    return
                except ConnectionClosed:
                    self._logger.warning("connection gone{}", frame.label)
                    self._schedule_retirement(
                        connection,
                        state,
                        close_connection=False,
                    )
                    return
                except Exception:
                    self._logger.exception("send failed{}", frame.label)
                    self._schedule_retirement(
                        connection,
                        state,
                        close_connection=True,
                        close_code=_CLOSE_INTERNAL_ERROR,
                        close_reason="outbound send failed",
                    )
                    return
                finally:
                    state.buffered_bytes = max(0, state.buffered_bytes - frame.utf8_bytes)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if state.writer is current:
                state.writer = None
            # A cancelled writer must not resurrect itself. `close()` sets
            # `closing` before it cancels, so the usual teardown is already
            # covered by the check below -- but a cancellation from outside this
            # module (a task group tearing down the loop) would otherwise start
            # a fresh writer from inside a shutdown, and that task would try to
            # send on a socket nobody is holding open any more.
            if not cancelled and not state.closing and not state.queue.empty():
                self._start_writer(connection, state)

    # -- Retirement and teardown --------------------------------------------

    def _schedule_retirement(
        self,
        connection: ServerConnection,
        state: _ConnectionOutbound,
        *,
        close_connection: bool,
        close_code: int = 1000,
        close_reason: str = "",
    ) -> None:
        """Start closing *connection* out of band.

        Out of band because the caller is usually the writer task itself, and a
        writer that awaited its own teardown would be awaiting its own
        cancellation. The task is held in ``_retire_tasks`` so ``stop()`` can
        wait for it -- a retirement dropped on the floor is a connection that
        stays in the channel's subscriber sets after its socket is gone.
        """
        if state.closing:
            return
        state.closing = True
        task = asyncio.create_task(
            self._retire_connection(
                connection,
                close_connection=close_connection,
                close_code=close_code,
                close_reason=close_reason,
            ),
            name=f"websocket-retire-{id(connection):x}",
        )
        self._retire_tasks.add(task)
        task.add_done_callback(self._retire_tasks.discard)

    async def _retire_connection(
        self,
        connection: ServerConnection,
        *,
        close_connection: bool,
        close_code: int,
        close_reason: str,
    ) -> None:
        """Close the socket if it is still open, then hand it to the channel.

        The cleanup is in a ``finally`` and its own failure is contained: a
        close that hangs, a close that raises, or a channel cleanup that raises
        must all still end with this connection out of the channel's tables.
        Anything less turns one bad socket into a permanent entry in every
        fanout.
        """
        try:
            if close_connection:
                try:
                    async with asyncio.timeout(_OUTBOUND_CLOSE_TIMEOUT_S):
                        await connection.close(code=close_code, reason=close_reason)
                except TimeoutError:
                    self._logger.warning("timed out closing slow WebSocket connection")
                    with suppress(Exception):
                        connection.transport.abort()
                except ConnectionClosed:
                    pass
                except Exception:
                    self._logger.exception("failed to close WebSocket connection")
                    with suppress(Exception):
                        connection.transport.abort()
        finally:
            try:
                await self._retire(connection)
            except Exception:
                self._logger.exception("failed to clean up WebSocket connection")

    async def close(self, connection: ServerConnection) -> None:
        """Retire *connection*'s delivery state. Safe to call repeatedly.

        Called from the channel's connection cleanup, which is itself reached
        from the connection loop's ``finally``, from a retirement, and from
        ``stop()``. Marking the connection retired first is what makes the
        ordering safe: from this line on, ``register`` refuses it, so nothing
        that is still in flight can put back what the next lines take away.
        """
        self._retired.add(connection)
        state = self._state.get(connection)
        if state is None:
            return
        state.closing = True
        await self._stop_writer(state)
        # Only drop the mapping if it is still the state we just tore down. A
        # concurrent retirement could have replaced it, and popping a live entry
        # would orphan its writer.
        if self._state.get(connection) is state:
            self._state.pop(connection, None)

    @staticmethod
    async def _stop_writer(state: _ConnectionOutbound) -> None:
        """Cancel the writer, wait for it, and discard the frames it was owed.

        Awaiting the cancellation is the point. Cancel-and-forget would let the
        task outlive the connection it is writing to and surface later as a send
        on a closed socket, which is the leak this whole path exists to prevent.

        The queue is drained rather than left to the garbage collector so
        ``buffered_bytes`` and the queue agree afterwards: a retirement can be
        followed by a readmission of the same object, and a stale byte count
        would silently shrink the next connection's budget.
        """
        task = state.writer
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        while True:
            try:
                state.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        state.buffered_bytes = 0

    async def drain_retirements(self) -> None:
        """Wait for every in-flight retirement. Called from the channel's stop.

        A shutdown that returns while a retirement is still running leaves a
        task holding a socket after the channel says it has stopped, which in a
        test run is a warning against the *next* test and in production is a
        close that never completes.
        """
        tasks = tuple(self._retire_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
