"""OpenAI-compatible HTTP API server for a fixed nanoinfra session.

Provides /v1/chat/completions and /v1/models endpoints.
All requests route to a single persistent API session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json as _json
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Awaitable, Callable, NamedTuple, cast

from aiohttp import web
from loguru import logger

from nanoinfra.api.responses import ResponseSessions, handle_responses
from nanoinfra.config.paths import get_media_dir
from nanoinfra.providers.base import LLMUsage
from nanoinfra.utils.helpers import safe_filename
from nanoinfra.utils.media_decode import (
    MAX_FILE_SIZE,
)
from nanoinfra.utils.media_decode import (
    FileSizeExceeded as _FileSizeExceeded,
)
from nanoinfra.utils.media_decode import (
    save_base64_data_url as _save_base64_data_url,
)
from nanoinfra.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanoinfra.agent.loop import AgentLoop

__all__ = (
    "MAX_FILE_SIZE",
    "_FileSizeExceeded",
    "_save_base64_data_url",
    "api_request_state",
    "create_app",
    "drain_outbound",
    "handle_chat_completions",
    "handle_responses",
)


API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"
_AGENT_LOOP_KEY = web.AppKey[Any]("agent_loop")
_MODEL_NAME_KEY = web.AppKey[str]("model_name")
_REQUEST_TIMEOUT_KEY = web.AppKey[float]("request_timeout")
_SESSION_LOCKS_KEY = web.AppKey["SessionLocks"]("session_locks")
_RESPONSE_SESSIONS_KEY = web.AppKey[Any]("response_sessions")

class SessionLocks:
    """Per-session locks with a bound on how many are kept.

    ``session_id`` arrives in the request body, so a caller can name as many sessions as it likes.
    A plain dict here grows one ``asyncio.Lock`` per distinct id and never shrinks, which is a
    memory-exhaustion path an unauthenticated caller controls (upstream HKUDS/nanobot#4883).

    Eviction is reference-counted rather than plain LRU, and that distinction is the whole point.
    A lock may only be dropped while no request holds it *or is waiting on it*. Dropping one that
    is in use would hand the next request for that session a different lock object, and the two
    would stop being serialized -- turning a memory fix into a concurrency bug. When every lock is
    busy the bound is exceeded on purpose; correctness wins over the cap.
    """

    __slots__ = ("_locks", "_max_idle", "_users")

    def __init__(self, *, max_idle: int = 1024) -> None:
        if max_idle <= 0:
            raise ValueError("max_idle must be positive")
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._users: dict[str, int] = {}
        self._max_idle = max_idle

    @contextlib.asynccontextmanager
    async def acquire(self, key: str) -> AsyncGenerator[None]:
        """Hold this session's lock for the duration of the block."""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._locks.move_to_end(key)
        # Counted before the await, so nothing can evict this entry while we wait for it.
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._users[key] - 1
            if remaining:
                self._users[key] = remaining
            else:
                del self._users[key]
                self._evict_idle()

    def _evict_idle(self) -> None:
        """Drop the oldest locks nobody is using, until the bound is met."""
        if len(self._locks) <= self._max_idle:
            return
        for key in list(self._locks):
            if len(self._locks) <= self._max_idle:
                return
            if key not in self._users:
                del self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)

    def in_use(self, key: str) -> bool:
        """Whether any request currently holds or awaits this session's lock."""
        return key in self._users


_MISSING = object()


def _app_value(
    app: Any,
    key: web.AppKey[Any],
    legacy_key: str,
    default: Any = _MISSING,
) -> Any:
    """Read typed aiohttp state while accepting lightweight dict test doubles."""
    try:
        return app[key]
    except KeyError:
        if default is _MISSING:
            return app[legacy_key]
        return app.get(legacy_key, default)


class ApiState(NamedTuple):
    """What every API route needs from the app, read in one place (#211)."""

    agent_loop: Any
    model_name: str
    request_timeout: float
    session_locks: "SessionLocks"
    response_sessions: Any


def api_request_state(app: Any) -> ApiState:
    """Read this app's API state, accepting the dict test doubles `_app_value` accepts."""
    return ApiState(
        agent_loop=_app_value(app, _AGENT_LOOP_KEY, "agent_loop"),
        model_name=_app_value(app, _MODEL_NAME_KEY, "model_name", "nanoinfra"),
        request_timeout=_app_value(app, _REQUEST_TIMEOUT_KEY, "request_timeout", 120.0),
        session_locks=_app_value(app, _SESSION_LOCKS_KEY, "session_locks"),
        response_sessions=_response_sessions(app),
    )


def _response_sessions(app: Any) -> "ResponseSessions":
    """This app's response index, or a throwaway for a test double that has none.

    Checked against `None` rather than for truthiness: an empty index is falsy, and `or` here
    handed every request a fresh one, so `previous_response_id` never resolved.
    """
    existing = _app_value(app, _RESPONSE_SESSIONS_KEY, "response_sessions", None)
    if existing is None:
        return ResponseSessions()
    return cast("ResponseSessions", existing)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(
    content: str,
    model: str,
    usage: LLMUsage | None = None,
) -> dict[str, Any]:
    # The OpenAI-compatible shape is a wire format somebody else's client reads, so this stays
    # `prompt_tokens`/`completion_tokens` however the type spells them internally (#175).
    prompt = usage.input_tokens if usage else 0
    completion = usage.output_tokens if usage else 0
    total = (usage.total_tokens if usage else 0) or prompt + completion
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
    }


def response_text(value: Any) -> str:
    """Normalize process_direct output to plain assistant text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


#: The private spellings predate a second route sharing them (#211).
_error_json = error_json
_response_text = response_text


def _as_str(value: object) -> str:
    """Return *value* when it is text, otherwise an empty string."""
    return value if isinstance(value, str) else ""


def _require_json_object(value: object, field: str) -> dict[str, Any]:
    """Validate an object-valued field from an untrusted JSON request."""
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _require_json_string(value: object, field: str) -> str:
    """Validate a string-valued field from an untrusted JSON request."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_chunk(delta: str, model: str, chunk_id: str, finish_reason: str | None = None) -> bytes:
    """Format a single OpenAI-compatible SSE chunk."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {_json.dumps(payload)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


#: Roles that carry prompt text rather than a past answer, on this wire's spelling.
_TURN_ROLES = frozenset({"user", "system", "developer"})


def _parse_json_content(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Parse JSON request body. Returns (text, media_paths).

    The same tail rule `/v1/responses` applies (#211, and
    `proposals/client-transcript-input.md`): items up to and including the last `assistant` message
    are the client's copy of what we already said and are dropped, and what follows is this turn.
    This route needed it for the same reason -- a client with `wire_api = "chat"` sends its
    instructions as a `system` message beside the prompt, so demanding exactly one `user` message
    rejected its first request.
    """
    messages_value = cast(object, body.get("messages"))
    if not isinstance(messages_value, list):
        raise ValueError("messages must be an array")
    raw_messages = cast(list[object], messages_value)
    if not raw_messages:
        raise ValueError("messages must not be empty")

    messages: list[dict[str, Any]] = []
    for entry in raw_messages:
        if not isinstance(entry, dict):
            raise ValueError("each message must be an object")
        messages.append(cast(dict[str, Any], entry))

    last_answer = -1
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            last_answer = index
    tail = messages[last_answer + 1 :]
    if not tail:
        raise ValueError(
            "messages end with an assistant reply, so they carry no new turn to answer"
        )
    # A `system` message frames a turn without being one. See the note in `responses.py`.
    if not any(message.get("role", "user") == "user" for message in tail):
        raise ValueError("messages carry no user message to answer")
    for message in tail:
        role = message.get("role", "user")
        if role not in _TURN_ROLES:
            raise ValueError(f"unsupported role after the last reply: {role}")

    media_dir = get_media_dir("api")
    media_paths: list[str] = []
    joined: list[str] = []
    for message in tail:
        text, paths = _message_content(cast(object, message.get("content", "")), media_dir)
        if text.strip():
            joined.append(text)
        media_paths.extend(paths)

    # Blank line between parts: an instruction block and a question are two things to read, and
    # running them together changes where one ends.
    return "\n\n".join(joined), media_paths


def _message_content(content: object, media_dir: Any) -> tuple[str, list[str]]:
    """Read one message's content into (text, media_paths).

    Its own function rather than a recursive call into `_parse_json_content`: the first attempt at
    this reused that entry point for each message ahead of the turn, which re-applied the "there
    must be a user message" rule to a lone `system` item and rejected the very shape it was meant
    to accept.
    """
    media_paths: list[str] = []
    if isinstance(content, str):
        return content, media_paths
    if not isinstance(content, list):
        raise ValueError("Invalid content format")

    text_parts: list[str] = []
    for part_value in cast(list[object], content):
        if not isinstance(part_value, dict):
            continue
        part = cast(dict[str, Any], part_value)
        if part.get("type") == "text":
            text_parts.append(
                _require_json_string(
                    cast(object, part.get("text", "")),
                    "messages[].content[].text",
                )
            )
        elif part.get("type") == "image_url":
            image_url = _require_json_object(
                cast(object, part.get("image_url", {})),
                "messages[].content[].image_url",
            )
            url = _require_json_string(
                cast(object, image_url.get("url", "")),
                "messages[].content[].image_url.url",
            )
            if url.startswith("data:"):
                saved = _save_base64_data_url(url, media_dir)
                if saved:
                    media_paths.append(saved)
            elif url:
                raise ValueError(
                    "Remote image URLs are not supported. "
                    "Use base64 data URLs or upload files via multipart/form-data."
                )
    return " ".join(text_parts), media_paths


async def _parse_multipart(request: web.Request) -> tuple[str, list[str], str | None, str | None]:
    """Parse multipart/form-data. Returns (text, media_paths, session_id, model)."""
    media_dir = get_media_dir("api")
    reader = await request.multipart()
    text = ""
    session_id = None
    model = None
    media_paths: list[str] = []

    while True:
        part: Any = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "model":
            model = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            raw = await part.read()
            if len(raw) > MAX_FILE_SIZE:
                raise _FileSizeExceeded(
                    f"File '{part.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                )
            base = safe_filename(part.filename or "upload.bin")
            filename = f"{uuid.uuid4().hex[:12]}_{base}"
            dest = media_dir / filename
            dest.write_bytes(raw)
            media_paths.append(str(dest))

    if not text:
        text = "请分析上传的文件"

    return text, media_paths, session_id, model


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def handle_chat_completions(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/chat/completions — supports JSON and multipart/form-data."""
    content_type = _as_str(cast(object, request.content_type or ""))

    agent_loop = _app_value(request.app, _AGENT_LOOP_KEY, "agent_loop")
    timeout_s: float = _app_value(
        request.app,
        _REQUEST_TIMEOUT_KEY,
        "request_timeout",
        120.0,
    )
    model_name: str = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "nanoinfra")

    stream = False
    try:
        if content_type.startswith("multipart/"):
            text, media_paths, session_id, requested_model = await _parse_multipart(request)
        else:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            if not isinstance(body, dict):
                return _error_json(400, "Invalid JSON body")
            body = cast(dict[str, Any], body)
            stream = body.get("stream", False)
            requested_model = body.get("model")
            text, media_paths = _parse_json_content(body)
            session_id = body.get("session_id")
    except ValueError as e:
        return _error_json(400, str(e))
    except _FileSizeExceeded as e:
        return _error_json(413, str(e), err_type="invalid_request_error")
    except Exception:
        logger.exception("Error parsing upload")
        return _error_json(413, "File too large or invalid upload")

    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    session_locks: SessionLocks = _app_value(
        request.app,
        _SESSION_LOCKS_KEY,
        "session_locks",
    )

    logger.info(
        "API request session_key={} media={} text={} stream={}",
        session_key, len(media_paths), text[:80], stream,
    )
    # -- streaming path --
    if stream:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stream_failed = False
        emitted_content = False

        async def _on_stream(token: str) -> None:
            nonlocal emitted_content
            if token:
                emitted_content = True
            await queue.put(token)

        async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
            # Agent stream-end callbacks mark generation segment boundaries.
            # Tool-backed requests may continue after a segment ends, so the
            # HTTP SSE stream is closed only when process_direct returns.
            return None

        async def _run() -> None:
            nonlocal stream_failed
            try:
                async with session_locks.acquire(session_key):
                    response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                        ),
                        timeout=timeout_s,
                    )
                    if not emitted_content:
                        response_text = _response_text(response)
                        if response_text.strip():
                            await queue.put(response_text)
            except Exception:
                stream_failed = True
                logger.exception("Streaming error for session {}", session_key)
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        try:
            while True:
                token = await queue.get()
                if token is None:
                    break
                await resp.write(_sse_chunk(token, model_name, chunk_id))
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if not stream_failed:
            await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
            await resp.write(_SSE_DONE)
        return resp

    # -- non-streaming path (original logic) --
    try:
        async with session_locks.acquire(session_key):
            try:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=timeout_s,
                )
                response_text = _response_text(response)
                if not response_text or not response_text.strip():
                    logger.warning("Empty response for session {}, using fallback", session_key)
                    response_text = EMPTY_FINAL_RESPONSE_MESSAGE

            except asyncio.TimeoutError:
                return _error_json(504, f"Request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error processing request for session {}", session_key)
                return _error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected API lock error for session {}", session_key)
        return _error_json(500, "Internal server error", err_type="server_error")

    return web.json_response(
        _chat_completion_response(response_text, model_name, getattr(agent_loop, "_last_usage", None))
    )


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models"""
    model_name = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "nanoinfra")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "nanoinfra",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({"status": "ok"})


async def drain_outbound(bus: Any) -> None:
    """Consume and discard the agent's outbound events for the lifetime of the API server.

    `bus.outbound` is bounded at 1000 and `publish_outbound` *awaits* a full queue -- safe, says
    its docstring, "because the consumer is the channel manager, a different task". Under `serve`
    there is no channel manager: `gateway` drains through `ChannelManager` and `nanoinfra agent`
    runs its own consumer, and this entry point did neither. So a turn that emitted more than a
    thousand progress, stream or trace events stopped mid-flight and its HTTP request hung until
    the timeout. Short answers never reached the bound, which is why it stayed hidden.

    Discarding is the right handling rather than a shortcut: this route's answer comes from
    `process_direct`'s return value and its `on_stream` callback, so the bus copy is already
    delivered. And an event addressed to another channel could not be delivered anyway -- `serve`
    starts no channels, which is what the documented "the `message` tool does not deliver from an
    API session" already says.
    """
    discarded = 0
    try:
        while True:
            await bus.consume_outbound()
            discarded += 1
            if discarded % 1000 == 0:
                logger.debug("API server discarded {} outbound events", discarded)
    except asyncio.CancelledError:
        logger.debug("API server outbound drain stopped after {} events", discarded)
        raise


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop: "AgentLoop",
    model_name: str = "nanoinfra",
    request_timeout: float = 120.0,
    api_key: str = "",
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
        api_key: Optional API key for Bearer-token authentication on API routes.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for base64 images
    app[_AGENT_LOOP_KEY] = agent_loop
    app[_MODEL_NAME_KEY] = model_name
    app[_REQUEST_TIMEOUT_KEY] = request_timeout
    app[_SESSION_LOCKS_KEY] = SessionLocks()  # per-session locks, bounded (#4883)
    app[_RESPONSE_SESSIONS_KEY] = ResponseSessions()  # previous_response_id -> session (#211)

    if not api_key:
        # Both start paths already refuse a non-loopback bind without a key, so reaching here
        # normally means loopback. An embedder calling this factory directly has no such check,
        # and an agent API that answers anybody is worth one line in the log either way.
        logger.warning(
            "API server has no api_key configured: every request is served unauthenticated. "
            "Set api.api_key before exposing this beyond loopback."
        )

    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        # Allow unauthenticated health checks.
        if request.path == "/health":
            return await handler(request)
        if not api_key:
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _error_json(401, "Missing Authorization header. Use: Bearer <api_key>")
        if not hmac.compare_digest(auth[len("Bearer "):], api_key):
            return _error_json(401, "Invalid API key")
        return await handler(request)

    app.middlewares.append(auth_middleware)

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app
