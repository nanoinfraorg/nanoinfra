"""`POST /v1/responses` — the same agent over the Responses wire (#211).

A client that defaults to the Responses API got a 404 from this server, because the only chat route
was `/v1/chat/completions`. This module adds the other wire shape. **It is a mirror, not a second
product:** nanoinfra runs its own loop, its own tools and its own gate, and the client sends input
and receives an answer. That is what the chat route does today and it stays the contract.

Two request fields are therefore accepted and ignored, and the reason is the same for both:

- **`tools`.** Returning `function_call` items for the *caller* to execute would make nanoinfra a
  model proxy and route around the capability gate, the confined executor and the audit log. On a
  local CLI run by the owner of the machine that is one thing; on an exposed API where the caller
  chooses the tools and the deployment's identity signs the request it is another. If it is ever
  wanted, the gate gets designed first.
- **`instructions`.** The system prompt belongs to the deployment. A caller that could replace it
  could ask for a different agent than the one the operator configured.

Both are logged once per process rather than dropped in silence, so a client waiting for behaviour
it asked for can find out why it never came.

The history lives here, not in the request. `input` therefore carries **one** user message, the
same restriction the chat route enforces, and a follow-up names its conversation with
`previous_response_id` (or `session_id`, this server's own field). Sending a full transcript back
would replay it on top of the session that already holds it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import time
import uuid
from collections import OrderedDict
from typing import Any, cast

from aiohttp import web
from loguru import logger

from nanoinfra.config.paths import get_media_dir
from nanoinfra.providers.base import LLMUsage
from nanoinfra.utils.media_decode import FileSizeExceeded, save_base64_data_url
from nanoinfra.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

__all__ = (
    "ResponseSessions",
    "handle_responses",
    "parse_responses_input",
    "response_object",
)

_REMOTE_IMAGE = (
    "Remote image URLs are not supported. Use base64 data URLs, or the multipart form of "
    "/v1/chat/completions to upload files."
)


class ResponseSessions:
    """Which conversation a response id belongs to, so `previous_response_id` can resolve.

    The Responses API lets a client continue by naming the previous response rather than by
    resending the transcript, and this server already keeps the transcript. So the only thing
    missing was the mapping, and it is deliberately *only* a mapping: no response bodies are
    retained, so this cannot become a second copy of the history it points at.

    Bounded and LRU for the same reason `SessionLocks` is: the ids are minted per request, so an
    unbounded dict is a memory path a caller controls. Losing an old entry costs a client one
    404 telling it to name the session instead, which is recoverable; running out of memory is not.
    """

    __slots__ = ("_entries", "_max_entries")

    def __init__(self, *, max_entries: int = 4096) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._max_entries = max_entries

    def remember(self, response_id: str, session_key: str) -> None:
        self._entries[response_id] = session_key
        self._entries.move_to_end(response_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def session_for(self, response_id: str) -> str | None:
        session_key = self._entries.get(response_id)
        if session_key is not None:
            self._entries.move_to_end(response_id)
        return session_key

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _content_parts(content: object, media_dir: Any) -> tuple[str, list[str]]:
    """Read one message's content into (text, media_paths)."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise ValueError("input content must be a string or an array of content parts")

    text_parts: list[str] = []
    media_paths: list[str] = []
    for raw_part in cast(list[object], content):
        if not isinstance(raw_part, dict):
            continue
        part = cast(dict[str, Any], raw_part)
        part_type = part.get("type")
        # `text` is the chat spelling; a client that reuses its old builder should still work.
        if part_type in {"input_text", "text", "output_text"}:
            value = cast(object, part.get("text", ""))
            if not isinstance(value, str):
                raise ValueError("input content text must be a string")
            text_parts.append(value)
        elif part_type == "input_image":
            # `image_url` is a bare string here, unlike the chat route's nested object.
            raw_url = cast(object, part.get("image_url", ""))
            if isinstance(raw_url, dict):
                raw_url = cast(object, cast(dict[str, Any], raw_url).get("url", ""))
            if not isinstance(raw_url, str):
                raise ValueError("input_image.image_url must be a string")
            if raw_url.startswith("data:"):
                saved = save_base64_data_url(raw_url, media_dir)
                if saved:
                    media_paths.append(saved)
            elif raw_url:
                raise ValueError(_REMOTE_IMAGE)
        elif part_type in {"input_file", "input_audio"}:
            raise ValueError(f"{part_type} content is not supported")

    return " ".join(p for p in text_parts if p), media_paths


def parse_responses_input(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Read the `input` field into (text, media_paths).

    **The turn is everything after the last thing this server said.** A client that keeps its own
    transcript resends it on every request, and the first version of this parser refused any
    multi-item input to stop that transcript being replayed on top of the session that already
    holds it. It refused too much: the very first request the Codex CLI makes carries its
    environment block *and* the user's prompt as two `user` items, and that is one prompt in
    pieces, not a history.

    So the tail rule. Items up to and including the last `assistant` are the client's copy of what
    we already said and are dropped; the `user` items after it are joined into this turn. With no
    `assistant` at all, every message is joined -- a first turn arriving in pieces.

    The trade is that when the two copies of the history disagree, ours wins: a client that pruned
    its transcript gets an answer informed by what this server kept. That is inherent to holding
    the session here, and it is recorded in `proposals/client-transcript-input.md`.

    Tool-call items are refused exactly as before. Nothing here changes what the caller may run.
    """
    raw_input = cast(object, body.get("input"))
    media_dir = get_media_dir("api")

    if isinstance(raw_input, str):
        if not raw_input.strip():
            raise ValueError("input must not be empty")
        return raw_input, []

    if not isinstance(raw_input, list):
        raise ValueError("input must be a string or an array")

    items = cast(list[object], raw_input)
    if not items:
        raise ValueError("input must not be empty")

    messages: list[dict[str, Any]] = []
    loose_parts: list[object] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("input items must be objects")
        item = cast(dict[str, Any], raw_item)
        item_type = item.get("type")
        if item_type in {"function_call", "function_call_output", "reasoning"}:
            raise ValueError(
                "This endpoint runs its own tools, so tool-call items are not accepted as input"
            )
        if "role" in item or item_type == "message":
            messages.append(item)
        else:
            loose_parts.append(item)

    if messages and loose_parts:
        # Still ambiguous: it could be one message or two, and guessing would sometimes drop text.
        raise ValueError("input mixes message items with bare content parts")

    if loose_parts:
        return _content_parts(loose_parts, media_dir)

    return _join_turn_messages(messages, media_dir)


#: Roles that carry prompt text rather than a past answer. `developer` is the Responses spelling of
#: a system message; both are the caller's framing of the same turn, so both join it.
_TURN_ROLES = frozenset({"user", "system", "developer"})


def _join_turn_messages(
    messages: list[dict[str, Any]],
    media_dir: Any,
) -> tuple[str, list[str]]:
    """Apply the tail rule to a list of message items."""
    last_answer = -1
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            last_answer = index
    tail = messages[last_answer + 1 :]

    if not tail:
        raise ValueError(
            "input ends with an assistant message, so it carries no new turn to answer"
        )
    # Instructions frame a turn, they are not one: a request carrying only a `system` or
    # `developer` item has nobody asking anything, and answering the instruction as if it were the
    # question is worse than saying so.
    if not any(message.get("role", "user") == "user" for message in tail):
        raise ValueError("input carries no user message to answer")

    texts: list[str] = []
    media: list[str] = []
    for message in tail:
        role = message.get("role", "user")
        if role not in _TURN_ROLES:
            raise ValueError(f"input carries an unsupported role after the last answer: {role}")
        text, paths = _content_parts(cast(object, message.get("content", "")), media_dir)
        if text.strip():
            texts.append(text)
        media.extend(paths)

    if not texts and not media:
        raise ValueError("input must not be empty")
    # Blank line between parts: a context block and a question are two things to read, and running
    # them together changes where one ends.
    return "\n\n".join(texts), media


_IGNORED_FIELDS_WARNED: set[str] = set()


def _warn_ignored_fields(body: dict[str, Any]) -> None:
    """Say once, per field, that the deployment owns what the caller tried to set."""
    for field, why in (
        ("tools", "nanoinfra executes its own tools behind the capability gate"),
        ("instructions", "the system prompt belongs to the deployment"),
        ("tool_choice", "nanoinfra decides its own tool use"),
    ):
        if body.get(field) in (None, [], "", {}):
            continue
        if field in _IGNORED_FIELDS_WARNED:
            continue
        _IGNORED_FIELDS_WARNED.add(field)
        logger.warning("/v1/responses ignores the request's `{}` field: {}", field, why)


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _usage_object(usage: LLMUsage | None) -> dict[str, Any]:
    prompt = usage.input_tokens if usage else 0
    completion = usage.output_tokens if usage else 0
    total = (usage.total_tokens if usage else 0) or prompt + completion
    cached = usage.cache_read_tokens if usage else None
    return {
        "input_tokens": prompt,
        # A `None` cache count means the provider never reported one; the wire field is an int, so
        # it becomes 0 here and the distinction stays in the telemetry that can carry it.
        "input_tokens_details": {"cached_tokens": cached or 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total,
    }


def _message_item(item_id: str, text: str | None, *, done: bool) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"type": "output_text", "text": text, "annotations": []})
    return {
        "id": item_id,
        "type": "message",
        "status": "completed" if done else "in_progress",
        "role": "assistant",
        "content": content,
    }


def response_object(
    *,
    response_id: str,
    model: str,
    status: str,
    created_at: int,
    output: list[dict[str, Any]] | None = None,
    usage: LLMUsage | None = None,
    previous_response_id: str | None = None,
    store: bool = True,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The `response` object, filled out enough for a strict client to parse it.

    Several of these fields are constants rather than settings, and each one is a decision recorded
    elsewhere in this file: `tools` is empty and `tool_choice` is `"none"` because the caller does
    not choose them, and `instructions` is null because the prompt is the deployment's.
    """
    body: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output or [],
        "error": error,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": False,
        "previous_response_id": previous_response_id,
        "store": store,
        "temperature": None,
        "tool_choice": "none",
        "tools": [],
        "top_p": None,
    }
    if status == "completed":
        # An SDK computes this from `output`; a hand-written client usually reads it directly.
        body["output_text"] = "".join(
            part.get("text", "")
            for item in body["output"]
            for part in cast(list[dict[str, Any]], item.get("content", []))
        )
    if usage is not None or status == "completed":
        body["usage"] = _usage_object(usage)
    return body


def _sse_event(event_type: str, payload: dict[str, Any], sequence: int) -> bytes:
    """One Responses SSE event.

    Both the `event:` line and the `type` field are written, because clients read one or the other
    -- our own `iter_sse` reads only `data:`, and a browser `EventSource` dispatches on `event:`.
    """
    body = {"type": event_type, "sequence_number": sequence, **payload}
    return f"event: {event_type}\ndata: {_json.dumps(body)}\n\n".encode()


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


async def handle_responses(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/responses — JSON only, streaming and non-streaming."""
    # Deferred because `server` imports this module to register the route; by the time a request
    # arrives both modules are loaded and the cycle is gone.
    from nanoinfra.api.server import (
        API_CHAT_ID,
        API_SESSION_KEY,
        api_request_state,
        error_json,
        response_text,
    )

    state = api_request_state(request.app)
    agent_loop = state.agent_loop
    timeout_s = state.request_timeout
    model_name = state.model_name
    session_locks = state.session_locks
    response_sessions: ResponseSessions = state.response_sessions

    try:
        body = await request.json()
    except Exception:
        return error_json(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return error_json(400, "Invalid JSON body")
    body = cast(dict[str, Any], body)

    requested_model = cast(object, body.get("model"))
    if requested_model and requested_model != model_name:
        return error_json(400, f"Only configured model '{model_name}' is available")

    stream = bool(body.get("stream", False))
    store = body.get("store", True) is not False
    _warn_ignored_fields(body)

    try:
        text, media_paths = parse_responses_input(body)
    except ValueError as e:
        return error_json(400, str(e))
    except FileSizeExceeded as e:
        return error_json(413, str(e))
    except Exception:
        logger.exception("Error parsing /v1/responses input")
        return error_json(400, "Invalid input")

    # -- which conversation --
    raw_session_id = cast(object, body.get("session_id"))
    session_id = raw_session_id.strip() if isinstance(raw_session_id, str) else ""
    raw_previous = cast(object, body.get("previous_response_id"))
    previous_response_id = raw_previous if isinstance(raw_previous, str) and raw_previous else None

    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    if previous_response_id:
        remembered = response_sessions.session_for(previous_response_id)
        if remembered is None:
            return error_json(
                404,
                f"Previous response with id '{previous_response_id}' not found. "
                "It may have aged out of this server's index; name the conversation with "
                "session_id instead.",
                err_type="invalid_request_error",
            )
        if session_id and remembered != session_key:
            return error_json(
                400,
                "session_id and previous_response_id name different conversations",
            )
        session_key = remembered

    response_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created_at = int(time.time())
    if store:
        response_sessions.remember(response_id, session_key)

    logger.info(
        "Responses API request session_key={} media={} text={} stream={}",
        session_key, len(media_paths), text[:80], stream,
    )

    def _shape(status: str, out: list[dict[str, Any]] | None, usage: Any, err: Any = None) -> Any:
        return response_object(
            response_id=response_id,
            model=model_name,
            status=status,
            created_at=created_at,
            output=out,
            usage=usage,
            previous_response_id=previous_response_id,
            store=store,
            error=err,
        )

    if stream:
        return await _stream_response(
            request,
            agent_loop=agent_loop,
            session_locks=session_locks,
            session_key=session_key,
            text=text,
            media_paths=media_paths,
            timeout_s=timeout_s,
            api_chat_id=API_CHAT_ID,
            response_id=response_id,
            item_id=item_id,
            shape=_shape,
            response_text_of=response_text,
        )

    try:
        async with session_locks.acquire(session_key):
            try:
                result = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=timeout_s,
                )
                answer = response_text(result)
                if not answer or not answer.strip():
                    logger.warning("Empty response for session {}, using fallback", session_key)
                    answer = EMPTY_FINAL_RESPONSE_MESSAGE
            except asyncio.TimeoutError:
                return error_json(504, f"Request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error processing /v1/responses for session {}", session_key)
                return error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected API lock error for session {}", session_key)
        return error_json(500, "Internal server error", err_type="server_error")

    return web.json_response(
        _shape(
            "completed",
            [_message_item(item_id, answer, done=True)],
            getattr(agent_loop, "_last_usage", None),
        )
    )


async def _stream_response(
    request: web.Request,
    *,
    agent_loop: Any,
    session_locks: Any,
    session_key: str,
    text: str,
    media_paths: list[str],
    timeout_s: float,
    api_chat_id: str,
    response_id: str,
    item_id: str,
    shape: Any,
    response_text_of: Any,
) -> web.StreamResponse:
    """The event sequence for one text answer.

    A Responses stream is a state machine, not a token feed: an item opens, a content part opens,
    deltas arrive, and each level closes in turn. A client that renders on `content_part.added`
    breaks if the deltas come first, so the frame events are written even when the agent produced
    nothing to stream.

    There is no `[DONE]` sentinel here, unlike the chat route. `response.completed` *is* the
    terminator in this protocol, and a client that also waits for `[DONE]` would hang.
    """
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    await resp.prepare(request)

    sequence = 0

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        nonlocal sequence
        await resp.write(_sse_event(event_type, payload, sequence))
        sequence += 1

    await emit("response.created", {"response": shape("in_progress", [], None)})
    await emit("response.in_progress", {"response": shape("in_progress", [], None)})
    await emit(
        "response.output_item.added",
        {"output_index": 0, "item": _message_item(item_id, None, done=False)},
    )
    await emit(
        "response.content_part.added",
        {
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    emitted_content = False
    failure: str | None = None

    async def _on_stream(token: str) -> None:
        nonlocal emitted_content
        if token:
            emitted_content = True
        await queue.put(token)

    async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
        # A segment boundary, not the end of the turn: a tool-backed request continues afterwards.
        return None

    async def _run() -> None:
        nonlocal failure
        try:
            async with session_locks.acquire(session_key):
                result = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=api_chat_id,
                        on_stream=_on_stream,
                        on_stream_end=_on_stream_end,
                    ),
                    timeout=timeout_s,
                )
                if not emitted_content:
                    answer = response_text_of(result)
                    if answer.strip():
                        await queue.put(answer)
        except asyncio.TimeoutError:
            failure = f"Request timed out after {timeout_s}s"
        except Exception:
            failure = "Internal server error"
            logger.exception("Streaming error for session {}", session_key)
        finally:
            await queue.put(None)

    task = asyncio.create_task(_run())
    collected: list[str] = []
    try:
        while True:
            token = await queue.get()
            if token is None:
                break
            if not token:
                continue
            collected.append(token)
            await emit(
                "response.output_text.delta",
                {
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": token,
                    "logprobs": [],
                },
            )
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    if failure is not None:
        await emit(
            "response.failed",
            {
                "response": shape(
                    "failed", None, None, {"code": "server_error", "message": failure}
                )
            },
        )
        return resp

    answer = "".join(collected) or EMPTY_FINAL_RESPONSE_MESSAGE
    await emit(
        "response.output_text.done",
        {"item_id": item_id, "output_index": 0, "content_index": 0, "text": answer},
    )
    await emit(
        "response.content_part.done",
        {
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": answer, "annotations": []},
        },
    )
    await emit(
        "response.output_item.done",
        {"output_index": 0, "item": _message_item(item_id, answer, done=True)},
    )
    await emit(
        "response.completed",
        {
            "response": shape(
                "completed",
                [_message_item(item_id, answer, done=True)],
                getattr(agent_loop, "_last_usage", None),
            )
        },
    )
    return resp
