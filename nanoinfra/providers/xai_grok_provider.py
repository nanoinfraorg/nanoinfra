"""xAI subscription provider with capability-gated hosted X Search."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
from loguru import logger

from nanoinfra import __version__
from nanoinfra.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ToolCallRequest,
    prefix_cache_key,
    resolve_stream_idle_timeout_s,
)
from nanoinfra.providers.openai_responses import (
    consume_sse_with_reasoning,
    convert_messages,
    convert_tools,
)
from nanoinfra.providers.proxy_url import normalize_proxy_url
from nanoinfra.providers.xai_oauth import (
    XAI_CLIENT_VERSION,
    XAIToken,
    get_xai_oauth_token,
)

DEFAULT_XAI_GROK_URL = "https://cli-chat-proxy.grok.com/v1/responses"
DEFAULT_XAI_GROK_MODELS_URL = "https://cli-chat-proxy.grok.com/v1/models"
DEFAULT_XAI_GROK_MODEL = "xai-grok/grok-4.5"
_MODEL_CAPABILITIES_TTL_S = 5 * 60
_MAX_ERROR_BODY_CHARS = 1000
_SENSITIVE_ERROR_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "idtoken",
    "refreshtoken",
}
#: The output item type xAI emits for the hosted search we request as `{"type": "x_search"}`.
#: Named separately from the `custom_tool_call` shape below because the two arrive on different
#: events: this one opens on `response.output_item.added` like OpenAI's `web_search_call`, while a
#: custom tool call announces itself with `response.custom_tool_call_input.done`.
_HOSTED_SEARCH_ITEM_TYPE = "x_search_call"
#: Terminal reasons that claim the response is whole. An unclosed hosted-search item under one of
#: these is the failure this guards: `length` is excluded because it already tells the caller the
#: answer was cut off and re-running the request would only hit the same output limit, and
#: `refusal` / `content_filter` / `error` are decisions rather than truncation.
_WHOLE_RESPONSE_FINISH_REASONS = frozenset({"stop", "tool_calls"})
#: Item statuses that mean the item was still running when the response ended.
_UNSETTLED_ITEM_STATUSES = frozenset({"in_progress", "queued", "searching"})
_TRUNCATED_HOSTED_SEARCH_MESSAGE = (
    "xAI ended the response while a hosted search was still open, so the answer was written "
    "without its result."
)


def _is_hosted_x_search_tool(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return cast(dict[object, object], value).get("type") == "x_search"


def _is_named_x_search_tool(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    record = cast(dict[object, object], value)
    return record.get("type") == "function" and record.get("name") == "x_search"


class XAIGrokProvider(LLMProvider):
    """Call xAI's subscription proxy and expose supported hosted tools."""

    supports_progress_deltas = True
    #: A truncated hosted search is discovered *after* the answer has streamed, so
    #: `_run_with_retry`'s "content already reached the user" guard would otherwise skip the
    #: retry. Declaring the callback lets this provider tell the runner the segment is being
    #: abandoned, which is exactly what it is.
    supports_stream_recover_callback = True

    def __init__(
        self,
        default_model: str = DEFAULT_XAI_GROK_MODEL,
        proxy: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model
        # Normalised here rather than at each httpx call site: the same attribute feeds the
        # streaming request, the model catalog lookup and the OAuth client, and all three build
        # their own `httpx` client from it.
        self.proxy = normalize_proxy_url(proxy) or None
        self._extra_body = dict(extra_body or {})
        self._model_capabilities: dict[str, bool] | None = None
        self._model_capabilities_fetched_at = 0.0

    async def _supports_backend_search(self, token: XAIToken, model: str) -> bool:
        now = time.monotonic()
        capabilities = self._model_capabilities
        if (
            capabilities is None
            or now - self._model_capabilities_fetched_at >= _MODEL_CAPABILITIES_TTL_S
        ):
            try:
                capabilities = await _fetch_xai_model_capabilities(
                    DEFAULT_XAI_GROK_MODELS_URL,
                    _build_model_headers(token),
                    proxy=self.proxy,
                )
            except Exception as exc:
                logger.warning(
                    "xAI model capability lookup failed; hosted X Search disabled for model {}: "
                    "type={} error={}",
                    model,
                    type(exc).__name__,
                    str(exc).strip() or "unexpected error",
                )
                capabilities = {}
                self._model_capabilities = capabilities
                self._model_capabilities_fetched_at = now
            else:
                self._model_capabilities = capabilities
                self._model_capabilities_fetched_at = now
        return capabilities.get(model, False)

    async def _call_xai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_stream_recover: Callable[[], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        wire_model = _strip_model_prefix(model or self.default_model)
        # No item ids: this provider keeps no conversation state and sends `store: false`, so
        # every request re-converts the whole transcript. A `call_id|item_id` in it was issued by
        # an earlier response, and xAI rejects a request that replays one it does not own with
        # "input item ID does not belong to this connection".
        system_prompt, input_items = convert_messages(messages, include_item_ids=False)

        stage = "oauth_token"
        try:
            token = await asyncio.to_thread(get_xai_oauth_token, proxy=self.proxy)
            configured_tools = self._extra_body.get("tools")
            tools_are_explicit = "tools" in self._extra_body
            configured_hosted_search = (
                isinstance(configured_tools, list)
                and any(
                    _is_hosted_x_search_tool(tool)
                    for tool in cast(list[object], configured_tools)
                )
            )
            supports_backend_search = False
            if not tools_are_explicit:
                stage = "model_capabilities"
                supports_backend_search = await self._supports_backend_search(token, wire_model)
            converted_tools = convert_tools(tools or [])
            if isinstance(configured_tools, list):
                converted_tools.extend(cast(list[dict[str, Any]], configured_tools))
            if supports_backend_search or configured_hosted_search:
                converted_tools = [
                    tool for tool in converted_tools if not _is_named_x_search_tool(tool)
                ]
            if supports_backend_search:
                converted_tools.append({"type": "x_search"})

            body: dict[str, Any] = {
                "model": wire_model,
                "store": False,
                "stream": True,
                "instructions": system_prompt,
                "input": input_items,
                "include": ["reasoning.encrypted_content"],
                "tools": converted_tools,
                "tool_choice": tool_choice or "auto",
                "parallel_tool_calls": True,
                "stream_tool_calls": True,
                "max_output_tokens": max_tokens,
                "temperature": temperature,
                "reasoning": _build_reasoning_options(reasoning_effort),
            }
            if self._extra_body:
                body.update({
                    key: value
                    for key, value in self._extra_body.items()
                    if key != "tools"
                })
                if tools_are_explicit and not isinstance(configured_tools, list):
                    body["tools"] = configured_tools

            headers = _build_headers(token.access, wire_model)
            stage = "xai_request"
            try:
                result = await _request_xai(
                    DEFAULT_XAI_GROK_URL,
                    headers,
                    body,
                    proxy=self.proxy,
                    on_content_delta=on_content_delta,
                    on_thinking_delta=on_thinking_delta,
                    on_tool_call_delta=on_tool_call_delta,
                    on_stream_recover=on_stream_recover,
                )
            except _XAIHTTPError as exc:
                if exc.status_code != 401:
                    raise
                stage = "oauth_refresh"
                token = await asyncio.to_thread(
                    get_xai_oauth_token,
                    proxy=self.proxy,
                    force_refresh=True,
                )
                self._model_capabilities = None
                self._model_capabilities_fetched_at = 0.0
                headers = _build_headers(token.access, wire_model)
                stage = "xai_request_retry"
                result = await _request_xai(
                    DEFAULT_XAI_GROK_URL,
                    headers,
                    body,
                    proxy=self.proxy,
                    on_content_delta=on_content_delta,
                    on_thinking_delta=on_thinking_delta,
                    on_tool_call_delta=on_tool_call_delta,
                    on_stream_recover=on_stream_recover,
                )

            content, tool_calls, finish_reason, usage, reasoning_content = result
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                reasoning_content=reasoning_content,
            )
        except Exception as exc:
            response = _xai_error_response(exc)
            logger.warning(
                "xAI subscription request failed: stage={} type={} retryable={} status={} "
                "error_type={} error_code={} response_body={}",
                stage,
                type(exc).__name__,
                response.error_should_retry,
                response.error_status_code,
                response.error_type,
                response.error_code,
                getattr(exc, "response_body", None),
            )
            return response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._call_xai(
            messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_stream_recover: Callable[[], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        return await self._call_xai(
            messages,
            tools,
            model,
            max_tokens,
            temperature,
            reasoning_effort,
            tool_choice,
            on_content_delta,
            on_thinking_delta,
            on_tool_call_delta,
            on_stream_recover,
        )

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if model.startswith("xai-grok/") or model.startswith("xai_grok/"):
        return model.split("/", 1)[1]
    return model


def _build_reasoning_options(reasoning_effort: str | None) -> dict[str, str]:
    options = {"summary": "concise"}
    if reasoning_effort and reasoning_effort.lower() != "none":
        options["effort"] = reasoning_effort
    return options


#: Namespace for deriving a stable `x-grok-conv-id` from a session key. A UUID5 rather than the key
#: itself, so a chat id never travels in a request header.
_CONV_NAMESPACE = uuid.UUID("6f9b1f4e-0d3a-5c1b-9f2e-7a4c8d1b6e30")

def _conversation_id() -> str:
    """A conversation id that is stable for the length of a chat.

    xAI caches the longest matching prefix automatically, and its docs are explicit that
    `x-grok-conv-id` is what routes a request to the server holding that prefix. This was
    `str(uuid.uuid4())` inline, which made every call a new conversation on a new server with a cold
    cache. See `prefix_cache_key`, which OpenAI's `prompt_cache_key` uses for the same reason.
    """
    return prefix_cache_key(_CONV_NAMESPACE)


def _build_headers(token: str, model: str) -> dict[str, str]:
    conversation_id = _conversation_id()
    return {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-authenticateresponse": "authenticate-response",
        "x-grok-client-version": XAI_CLIENT_VERSION,
        "x-grok-client-identifier": "nanoinfra",
        "x-grok-client-mode": "headless",
        "x-grok-conv-id": conversation_id,
        "x-grok-req-id": str(uuid.uuid4()),
        "x-grok-model-override": model,
        "x-grok-session-id": conversation_id,
        "x-grok-agent-id": str(uuid.uuid4()),
        "User-Agent": f"nanoinfra/{__version__} (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


def _build_model_headers(token: XAIToken) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token.access}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": XAI_CLIENT_VERSION,
        "x-grok-client-identifier": "nanoinfra",
        "x-grok-client-mode": "headless",
        "User-Agent": f"nanoinfra/{__version__} (python)",
        "accept": "application/json",
    }
    claims = _decode_access_token_claims(token.access)
    user_id = claims.get("sub")
    if claims.get("principal_type") == "Team":
        user_id = claims.get("principal_id") or user_id
    if isinstance(user_id, str) and user_id:
        headers["x-userid"] = user_id
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        email = token.account_id if token.account_id and "@" in token.account_id else None
    if email:
        headers["x-email"] = email
    return headers


def _decode_access_token_claims(token: str) -> dict[str, Any]:
    """Read identity hints from the signed token; the server still authenticates it."""
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (ValueError, TypeError):
        return {}
    return cast(dict[str, Any], claims) if isinstance(claims, dict) else {}


class _XAIStreamTruncatedError(RuntimeError):
    """A hosted-tool item opened and the stream ended on a whole-looking status without it.

    Raised instead of returning the answer, because the answer is the problem: the model wrote it
    without the search result it asked for, and nothing else in the response says so -- no error
    event, no `incomplete` status, and no citations to notice missing. `should_retry` is an
    attribute rather than a decision `_xai_error_response` derives, since there is no status code
    to derive it from and one more attempt is exactly the right response to a dropped item.
    """

    should_retry = True


class _XAIHTTPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: float | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        should_retry: bool | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.error_type = error_type
        self.error_code = error_code
        self.should_retry = should_retry
        self.response_body = response_body


async def _fetch_xai_model_capabilities(
    url: str,
    headers: dict[str, str],
    *,
    proxy: str | None = None,
) -> dict[str, bool]:
    client_kwargs: dict[str, Any] = {"timeout": 10.0, "follow_redirects": False}
    if proxy:
        client_kwargs.update(proxy=proxy, trust_env=False)
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get(url, headers=headers)
    if response.status_code != 200:
        raw = response.content.decode("utf-8", "ignore")
        raise _build_xai_http_error(response.status_code, response.headers, raw)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("xAI model catalog returned invalid JSON.") from exc
    return _parse_xai_model_capabilities(payload)


def _parse_xai_model_capabilities(payload: Any) -> dict[str, bool]:
    if isinstance(payload, dict):
        payload = cast(dict[str, Any], payload)
        rows: object = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    else:
        rows = payload
    if not isinstance(rows, list):
        return {}

    capabilities: dict[str, bool] = {}
    for row_value in cast(list[object], rows):
        if not isinstance(row_value, dict):
            continue
        row = cast(dict[str, Any], row_value)
        meta_value = row.get("_meta")
        meta = cast(dict[str, Any], meta_value) if isinstance(meta_value, dict) else {}
        support_value = row.get("supportsBackendSearch")
        if not isinstance(support_value, bool):
            support_value = row.get("supports_backend_search")
        if not isinstance(support_value, bool):
            support_value = meta.get("supportsBackendSearch")
        if not isinstance(support_value, bool):
            support_value = meta.get("supports_backend_search")
        supports_backend_search = support_value if isinstance(support_value, bool) else False

        identifiers = (
            row.get("model"),
            row.get("modelId"),
            row.get("id"),
            meta.get("model"),
            meta.get("modelId"),
        )
        for identifier in identifiers:
            if isinstance(identifier, str) and identifier.strip():
                capabilities[_strip_model_prefix(identifier.strip())] = supports_backend_search
    return capabilities


async def _request_xai(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    proxy: str | None = None,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_stream_recover: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str, LLMUsage | None, str | None]:
    # Hosted-tool items that opened and have not closed, keyed by call id. xAI can end a stream
    # on a terminal status without the `response.output_item.done` that pairs with an
    # `output_item.added`, and the answer it streamed alongside was then written without that
    # item's result.
    open_hosted_tools: dict[str, dict[str, Any]] = {}

    async def _dispatch_hosted_event(event: dict[str, Any]) -> None:
        hosted_event = _xai_hosted_tool_event(event)
        if hosted_event is None:
            return
        call_id = str(hosted_event["call_id"])
        if hosted_event["phase"] == "start":
            open_hosted_tools[call_id] = hosted_event
        else:
            open_hosted_tools.pop(call_id, None)
        if on_tool_call_delta is not None:
            await on_tool_call_delta(hosted_event)

    async def _on_response_event(event: dict[str, Any]) -> None:
        await _dispatch_hosted_event(event)
        if event.get("type") not in {"response.completed", "response.incomplete"}:
            return
        # The terminal payload carries the server's own record of every output item, so an item
        # settled there closed even though its `output_item.done` never arrived. Only the event
        # pair was lost, not the search -- close the row and do not retry a response that is
        # whole.
        for item in _settled_terminal_output_items(event):
            settled_id = str(item.get("id") or item.get("call_id") or "")
            if settled_id in open_hosted_tools:
                await _dispatch_hosted_event({
                    "type": "response.output_item.done",
                    "item": item,
                })

    client_kwargs: dict[str, Any] = {"timeout": resolve_stream_idle_timeout_s()}
    if proxy:
        client_kwargs.update(proxy=proxy, trust_env=False)
    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                content = await response.aread()
                raw = content.decode("utf-8", "ignore")
                raise _build_xai_http_error(response.status_code, response.headers, raw)
            # Always observed, not only when someone is watching progress: a `chat()` call with no
            # callbacks must still not report a search that never landed as a finished answer.
            result = await consume_sse_with_reasoning(
                response,
                on_content_delta=on_content_delta,
                on_tool_call_delta=on_tool_call_delta,
                on_reasoning_delta=on_thinking_delta,
                on_response_event=_on_response_event,
            )
    if open_hosted_tools and result[2] in _WHOLE_RESPONSE_FINISH_REASONS:
        await _fail_truncated_hosted_search(
            open_hosted_tools,
            finish_reason=result[2],
            on_tool_call_delta=on_tool_call_delta,
            on_stream_recover=on_stream_recover,
        )
    return result


def _settled_terminal_output_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Output items a terminal response reports as no longer running."""
    response_value = event.get("response")
    if not isinstance(response_value, dict):
        return []
    output = cast(dict[str, Any], response_value).get("output")
    if not isinstance(output, list):
        return []
    settled: list[dict[str, Any]] = []
    for raw_item in cast(list[object], output):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, Any], raw_item)
        status = item.get("status")
        # Absent status counts as settled: the item is in the response's final record. An
        # explicitly unfinished one is the signature this guard is looking for.
        if isinstance(status, str) and status in _UNSETTLED_ITEM_STATUSES:
            continue
        settled.append(item)
    return settled


async def _fail_truncated_hosted_search(
    open_hosted_tools: dict[str, dict[str, Any]],
    *,
    finish_reason: str,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
    on_stream_recover: Callable[[], Awaitable[None]] | None,
) -> None:
    """Close the abandoned activity rows, drop the partial answer, and raise for a retry."""
    logger.warning(
        "xAI ended a response with {} unclosed hosted-tool item(s) on finish_reason={}; "
        "discarding the answer and retrying: call_ids={}",
        len(open_hosted_tools),
        finish_reason,
        ",".join(sorted(open_hosted_tools)),
    )
    # The runner only fails its still-open hosted calls once the provider's *final* answer is an
    # error, so a retry that then succeeds would leave these rows spinning forever. Close them
    # here, where we know which attempt owned them.
    if on_tool_call_delta is not None:
        for hosted_event in list(open_hosted_tools.values()):
            await on_tool_call_delta({
                **hosted_event,
                "phase": "error",
                "result": None,
                "error": _TRUNCATED_HOSTED_SEARCH_MESSAGE,
            })
    if on_stream_recover is not None:
        await on_stream_recover()
    raise _XAIStreamTruncatedError(_TRUNCATED_HOSTED_SEARCH_MESSAGE)


def _xai_hosted_tool_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type == "response.custom_tool_call_input.done":
        call_id = event.get("item_id") or event.get("call_id") or event.get("id")
        if not call_id:
            return None
        return {
            "kind": "hosted_tool",
            "phase": "start",
            "call_id": str(call_id),
            "name": "x_search",
            "arguments": _xai_hosted_tool_arguments(
                event.get("input", event.get("arguments"))
            ),
            "result": None,
        }

    if event_type not in {"response.output_item.added", "response.output_item.done"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item = cast(dict[str, Any], item)
    phase = "start" if event_type == "response.output_item.added" else "end"

    if item.get("type") == _HOSTED_SEARCH_ITEM_TYPE:
        # The hosted search we ask for at `{"type": "x_search"}` comes back as its own output
        # item and never as a `custom_tool_call`, which is why the branch below never matched it
        # and its activity row was opened and never closed.
        call_id = item.get("id") or item.get("call_id") or event.get("item_id")
        if not call_id:
            return None
        status = item.get("status")
        return {
            "kind": "hosted_tool",
            "phase": phase,
            "call_id": str(call_id),
            "name": "x_search",
            "arguments": _xai_hosted_search_arguments(item),
            # Status only. The hosted result is large and the model answer already carries the
            # citations, the same reason the custom-tool branch below keeps only the subtype.
            "result": (
                None
                if phase == "start"
                else {"status": status if isinstance(status, str) else "completed"}
            ),
        }

    if phase != "end" or item.get("type") != "custom_tool_call":
        return None
    tool_name = item.get("name")
    if not isinstance(tool_name, str) or not tool_name.startswith("x_"):
        return None
    call_id = item.get("id") or item.get("call_id") or event.get("item_id")
    if not call_id:
        return None
    return {
        "kind": "hosted_tool",
        "phase": "end",
        "call_id": str(call_id),
        "name": "x_search",
        "arguments": _xai_hosted_tool_arguments(
            item.get("input", item.get("arguments"))
        ),
        # Keep the useful search subtype, but do not persist large hosted results
        # in WebUI activity messages. The model answer already carries citations.
        "result": {"name": tool_name},
    }


def _xai_hosted_search_arguments(item: dict[str, Any]) -> dict[str, Any]:
    """Recover the search terms from a hosted-search item, however it spells them.

    The `input` / `arguments` string is the custom-tool spelling; `action.queries` is the shape
    the official web-search item uses (`openai_responses.parsing._hosted_web_search_event`), and
    the hosted item follows it. Read both rather than guess which one this account gets.
    """
    arguments = _xai_hosted_tool_arguments(item.get("input", item.get("arguments")))
    if arguments:
        return arguments
    action_value = item.get("action")
    if not isinstance(action_value, dict):
        return {}
    action = cast(dict[str, Any], action_value)
    raw_queries = action.get("queries")
    queries = (
        [
            query.strip()
            for query in cast(list[object], raw_queries)
            if isinstance(query, str) and query.strip()
        ][:4]
        if isinstance(raw_queries, list)
        else []
    )
    query = " · ".join(queries) or next(
        (
            value.strip()
            for key in ("query", "pattern", "url")
            if isinstance((value := action.get(key)), str) and value.strip()
        ),
        "",
    )
    return {"query": query[:1000]} if query else {}


def _xai_hosted_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def _build_xai_http_error(
    status_code: int,
    headers: httpx.Headers,
    raw: str,
) -> _XAIHTTPError:
    retry_after = LLMProvider._extract_retry_after_from_headers(headers)  # pyright: ignore[reportPrivateUsage]
    error_type, error_code = LLMProvider._extract_error_type_code(raw)  # pyright: ignore[reportPrivateUsage]
    response_body = _bounded_error_body(raw)
    return _XAIHTTPError(
        _friendly_error(status_code, response_body),
        status_code=status_code,
        retry_after=retry_after,
        error_type=error_type,
        error_code=error_code,
        should_retry=_should_retry_status(status_code, error_type, error_code, raw),
        response_body=response_body,
    )


def _bounded_error_body(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        text = json.dumps(
            _redact_error_payload(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_BODY_CHARS:
        return f"{text[:_MAX_ERROR_BODY_CHARS]}…"
    return text


def _redact_error_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        payload_mapping: dict[str, Any] = cast(dict[str, Any], payload)
        for key in payload_mapping:
            value = payload_mapping[key]
            redacted[key] = (
                "[REDACTED]" if _is_sensitive_error_key(key) else _redact_error_payload(value)
            )
        return redacted
    if isinstance(payload, list):
        return [_redact_error_payload(value) for value in cast(list[Any], payload)]
    return payload


def _is_sensitive_error_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in _SENSITIVE_ERROR_KEYS


def _friendly_error(status_code: int, response_body: str | None = None) -> str:
    if status_code == 401:
        message = "xAI rejected the login. Sign in again with `nanoinfra provider login xai-grok`."
    elif status_code == 403:
        message = "This xAI account or subscription cannot access the Grok subscription endpoint."
    elif status_code == 426:
        message = "xAI requires a newer Grok client version. Update nanoinfra and try again."
    elif status_code == 429:
        message = "xAI usage quota or rate limit reached. Please try again later."
    else:
        message = f"xAI subscription endpoint returned HTTP {status_code}."
    if response_body:
        return f"{message} Response body: {response_body}"
    return message


def _xai_error_response(exc: Exception) -> LLMResponse:
    status_code = getattr(exc, "status_code", None)
    should_retry = getattr(exc, "should_retry", None)
    error_kind: str | None = None
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        error_kind = "timeout"
        should_retry = True if should_retry is None else should_retry
    elif isinstance(exc, (httpx.NetworkError, httpx.TransportError)):
        error_kind = "connection"
        should_retry = True if should_retry is None else should_retry
    elif isinstance(exc, _XAIStreamTruncatedError):
        error_kind = "truncated"
    elif isinstance(exc, _XAIHTTPError):
        error_kind = "http"
    if status_code is not None and should_retry is None:
        should_retry = _should_retry_status(
            int(status_code),
            getattr(exc, "error_type", None),
            getattr(exc, "error_code", None),
            None,
        )
    message = str(exc).strip() or "unexpected error"
    retry_after = getattr(exc, "retry_after", None)
    return LLMResponse(
        content=f"Error calling xAI ({type(exc).__name__}): {message}",
        finish_reason="error",
        retry_after=retry_after,
        error_status_code=int(status_code) if status_code is not None else None,
        error_kind=error_kind,
        error_type=getattr(exc, "error_type", None),
        error_code=getattr(exc, "error_code", None),
        error_retry_after_s=retry_after,
        error_should_retry=should_retry,
    )


def _should_retry_status(
    status_code: int,
    error_type: str | None,
    error_code: str | None,
    content: str | None,
) -> bool:
    if status_code == 429:
        return LLMProvider._is_retryable_429_response(  # pyright: ignore[reportPrivateUsage]
            LLMResponse(
                content=content or "",
                finish_reason="error",
                error_status_code=status_code,
                error_type=error_type,
                error_code=error_code,
            )
        )
    return status_code in LLMProvider._RETRYABLE_STATUS_CODES or status_code >= 500  # pyright: ignore[reportPrivateUsage]
