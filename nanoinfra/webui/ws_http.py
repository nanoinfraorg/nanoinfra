"""HTTP API handler extracted from WebSocketChannel.

Handles all non-WebSocket HTTP routes: bootstrap, sessions, settings,
media, commands, sidebar state, static file serving, and token management.

Also houses shared HTTP utility functions used by both this module and
``websocket.py`` to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanoinfra.command.builtin import builtin_command_palette
from nanoinfra.cron.session_turns import is_bound_cron_job
from nanoinfra.cron.types import CronJob, CronSchedule
from nanoinfra.diagrams.normalize import DiagramValidationError
from nanoinfra.diagrams.seed import seed_example_diagram_if_new_workspace
from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.runtime_context import public_history_messages
from nanoinfra.secrets.crypto import SecretsNotConfiguredError
from nanoinfra.secrets.normalize import SecretValidationError
from nanoinfra.secrets.postgres_backend import PostgresSecretsNotConfiguredError
from nanoinfra.secrets.store import SecretStore
from nanoinfra.security.workspace_access import WorkspaceScope
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.normalize import ServerValidationError
from nanoinfra.servers.store import ServerStore
from nanoinfra.triggers.local_types import LocalTrigger
from nanoinfra.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanoinfra.webui.audit_api import (
    AUDIT_READ_PATH,
    AuditReadSurface,
)
from nanoinfra.webui.diagrams_api import (
    create_webui_diagram,
    delete_webui_diagram,
    update_webui_diagram,
    webui_diagram_catalog_payload,
    webui_diagram_detail_payload,
    webui_diagrams_payload,
)
from nanoinfra.webui.file_preview import (
    WebUIFilePreviewError,
    file_preview_availability_payload,
    file_preview_payload,
)
from nanoinfra.webui.gateway_tokens import GatewayTokenStore, token_response_payload
from nanoinfra.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,
)
from nanoinfra.webui.http_utils import (
    combined_list_header as _combined_list_header,
)
from nanoinfra.webui.http_utils import (
    host_for_url as _host_for_url,
)
from nanoinfra.webui.http_utils import (
    http_error as _http_error,
)
from nanoinfra.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanoinfra.webui.http_utils import (
    http_response as _http_response,
)
from nanoinfra.webui.http_utils import (
    is_local_browser_request as _is_local_browser_request,
)
from nanoinfra.webui.http_utils import (
    is_localhost as _is_localhost,
)
from nanoinfra.webui.http_utils import (
    is_trusted_proxy_authenticated_request as _is_trusted_proxy_authenticated_request,
)
from nanoinfra.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,
)
from nanoinfra.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanoinfra.webui.http_utils import (
    parse_query as _parse_query,
)
from nanoinfra.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanoinfra.webui.http_utils import (
    query_first as _query_first,
)
from nanoinfra.webui.http_utils import (
    safe_host_header as _safe_host_header,
)
from nanoinfra.webui.ingress_policy import WebUIIngressPolicy
from nanoinfra.webui.latch_api import (
    LATCH_CLEAR_PATH,
    LATCH_READ_PATH,
    LatchClearError,
    LatchOperatorSurface,
    latch_values_from_request,
    operator_actor,
)
from nanoinfra.webui.media_gateway import WebUIMediaGateway
from nanoinfra.webui.secrets_api import (
    create_webui_secret,
    delete_webui_secret,
    update_webui_secret,
    webui_secret_detail_payload,
    webui_secrets_payload,
)
from nanoinfra.webui.servers_api import (
    create_webui_server,
    delete_webui_server,
    update_webui_server,
    webui_server_detail_payload,
    webui_servers_payload,
)
from nanoinfra.webui.session_automations import (
    all_automations_payload,
    serialize_automation_jobs,
    session_automation_jobs,
    session_automations_payload,
)
from nanoinfra.webui.session_list_index import (
    WEBUI_SESSION_INDEX_INTERNAL_FIELDS,
    indexed_workspace_scope,
    list_webui_sessions,
)
from nanoinfra.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanoinfra.webui.skills_api import (
    SkillManagementError,
    delete_webui_skill,
    set_webui_skill_enabled,
    webui_skill_detail_payload,
    webui_skills_payload,
)
from nanoinfra.webui.skills_marketplace import (
    SkillsMarketplaceError,
    install_marketplace_skill,
    marketplace_skill_trends,
    search_marketplace_skills,
    trending_marketplace_skills,
)
from nanoinfra.webui.thread_disk import delete_webui_thread
from nanoinfra.webui.transcript import build_webui_thread_response
from nanoinfra.webui.workspaces import WebUIWorkspaceController

_SLOW_WEBUI_HTTP_LOG_MS = 1_000
_AUTOMATION_VALUES_HEADER = "X-Nanoinfra-Automation-Values"
_DIAGRAM_VALUES_HEADER = "X-Nanoinfra-Diagram-Values"
_SECRET_VALUES_HEADER = "X-Nanoinfra-Secret-Values"
_SERVER_VALUES_HEADER = "X-Nanoinfra-Server-Values"

# Fix for #5190: On Windows, mimetypes.guess_type() reads the registry key
# HKEY_CLASSES_ROOT\.js\Content Type, which is commonly set to 'text/plain'
# because .js is associated with Windows Script Host rather than web JavaScript.
# That registry value overrides Python's built-in mapping and causes browsers to
# reject ES module scripts with:
#   Failed to load module script: Expected a JavaScript-or-Wasm module script
#   but the server responded with a MIME type of "text/plain".
# We explicitly register correct MIME types for common web static assets here
# (module-import time) so all callers of mimetypes.guess_type() in this process
# benefit, regardless of host registry configuration.
_MIME_FIXES: dict[str, str] = {
    ".js":    "application/javascript",
    ".mjs":   "application/javascript",
    ".css":   "text/css",
    ".html":  "text/html",
    ".json":  "application/json",
    ".svg":   "image/svg+xml",
    ".wasm":  "application/wasm",
}

for _ext, _ctype in _MIME_FIXES.items():
    mimetypes.add_type(_ctype, _ext, strict=True)


if TYPE_CHECKING:
    from nanoinfra.bus.queue import MessageBus
    from nanoinfra.channels.websocket.runtime import WebSocketConfig
    from nanoinfra.cron.service import CronService
    from nanoinfra.session.manager import SessionManager
    from nanoinfra.triggers.local_store import LocalTriggerStore
    from nanoinfra.utils.llm_runtime import LLMRuntime

def _decode_api_key(raw_key: str) -> str | None:
    key = unquote(raw_key)
    _api_key_re = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
    if _api_key_re.match(key) is None:
        return None
    return key


def _default_model_name_from_config() -> str | None:
    try:
        from nanoinfra.config.loader import load_config
        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str:
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config() or ""


# ---------------------------------------------------------------------------
# GatewayHTTPHandler
# ---------------------------------------------------------------------------


class GatewayHTTPHandler:
    """Handles all HTTP routes served alongside the WebSocket endpoint.

    Routes HTTP requests and delegates stateful work to explicit gateway
    services owned by the composition layer.
    """

    def __init__(
        self,
        *,
        config: WebSocketConfig,
        session_manager: SessionManager | None,
        static_dist_path: Path | None,
        runtime_model_name: Callable[[], str | None] | None,
        runtime_surface: str,
        runtime_capabilities_overrides: dict[str, Any] | None,
        bus: MessageBus,
        tokens: GatewayTokenStore,
        media: WebUIMediaGateway,
        ingress: WebUIIngressPolicy,
        workspaces: WebUIWorkspaceController,
        skills_workspace_path: Path,
        disabled_skills: set[str] | None = None,
        nanoinfra_skills_base_url: str = "https://skills.nanoinfra.org",
        cron_service: CronService | None = None,
        local_trigger_store: LocalTriggerStore | None = None,
        cron_pending_job_ids: Callable[[str], set[str]] | None = None,
        local_trigger_pending_ids: Callable[[str], set[str]] | None = None,
        channel_feature_action: Callable[..., Any] | None = None,
        channel_runtime_status: Callable[[], dict[str, Any]] | None = None,
        skill_state_action: Callable[[set[str]], None] | None = None,
        default_llm_runtime: Callable[[], LLMRuntime | None] | None = None,
        log: Any = logger,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.static_dist_path = static_dist_path
        self.runtime_model_name = runtime_model_name
        self.default_llm_runtime = default_llm_runtime
        self.bus = bus
        self.tokens = tokens
        self.media = media
        self.ingress = ingress
        self.workspaces = workspaces
        self.skills_workspace_path = skills_workspace_path
        self.disabled_skills: set[str] = (
            disabled_skills if disabled_skills is not None else set()
        )
        self.nanoinfra_skills_base_url = nanoinfra_skills_base_url
        # Diagrams are workspace-scoped the same way skills are — reuse the
        # already-threaded workspace path rather than adding a duplicate
        # constructor parameter for what is always the same value.
        self.diagrams = DiagramStore(skills_workspace_path)
        seed_example_diagram_if_new_workspace(self.diagrams)
        self.secrets = SecretStore(skills_workspace_path)
        self.servers = ServerStore(skills_workspace_path)
        self.jobs = JobStore(skills_workspace_path)
        reconciled = self.jobs.reconcile_interrupted_jobs()
        if reconciled:
            logger.warning(
                "Reconciled {} interrupted server job(s) from a prior restart",
                reconciled,
            )
        # The operator half of the denial latch (#28). The gateway attaches it after boot, and
        # nothing on the agent side can reach it. None means no gate runtime, so the two
        # latch routes answer 503 rather than an empty list.
        self.latch: LatchOperatorSurface | None = None
        # The read of the gate audit log (#29). Attached after boot for the same reason: a route
        # with no surface answers 503, and never an empty log.
        self.audit: AuditReadSurface | None = None
        self.skill_state_action = skill_state_action
        self._skill_install_lock = asyncio.Lock()
        self._title_retry_in_flight: set[str] = set()
        self.cron_service = cron_service
        self.local_trigger_store = local_trigger_store
        self.cron_pending_job_ids = cron_pending_job_ids
        self.local_trigger_pending_ids = local_trigger_pending_ids
        self._log = log
        self._runtime_surface = runtime_surface

        from nanoinfra.webui.settings_api import runtime_capabilities as _rc
        from nanoinfra.webui.settings_routes import WebUISettingsRouter

        self._capabilities = _rc(runtime_surface, runtime_capabilities_overrides or {})
        self.settings_routes = WebUISettingsRouter(
            bus=bus,
            logger=self._log,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            runtime_surface=runtime_surface,
            runtime_capabilities=self._capabilities,
            channel_feature_action=channel_feature_action,
            channel_runtime_status=channel_runtime_status,
        )

    def attach_latch_surface(self, surface: LatchOperatorSurface) -> None:
        """Take the operator half of the denial latch (#28). Only the gateway calls this."""
        self.latch = surface

    def attach_audit_surface(self, surface: AuditReadSurface) -> None:
        """Take the read of the gate audit log (#29). Only the gateway calls this."""
        self.audit = surface

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    # -- Token management ---------------------------------------------------

    def check_api_token(self, request: WsRequest) -> bool:
        if getattr(request, "_nanoinfra_trusted_proxy_authenticated", False):
            return True
        return self.tokens.check_api_token(request)

    # -- Main dispatch ------------------------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)
        started = time.perf_counter()
        response: Any | None = None
        setattr(
            request,
            "_nanoinfra_trusted_proxy_authenticated",
            _is_trusted_proxy_authenticated_request(connection, request.headers, self.config),
        )

        try:
            response = await self._dispatch_resolved(connection, request, got)
            return response
        finally:
            self._log_slow_http(got, response, started)

    async def _dispatch_resolved(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
    ) -> Any | None:
        # Token issue endpoint
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue(connection, request)

        # Bootstrap
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # Settings routes (delegated)
        response = await self.settings_routes.dispatch(connection, request, got)
        if response is not None:
            return response

        # Session routes
        response = await self._dispatch_session_routes(request, got)
        if response is not None:
            return response

        # Media routes
        response = self._dispatch_media_routes(request, got)
        if response is not None:
            return response

        # Automation routes
        response = await self._dispatch_automation_routes(request, got)
        if response is not None:
            return response

        # Diagram routes
        response = self._dispatch_diagram_routes(request, got)
        if response is not None:
            return response

        # Secret routes
        response = self._dispatch_secret_routes(request, got)
        if response is not None:
            return response

        # Server routes
        response = self._dispatch_server_routes(request, got)
        if response is not None:
            return response

        # Latch routes
        response = self._dispatch_latch_routes(request, got)
        if response is not None:
            return response
        response = self._dispatch_audit_routes(request, got)
        if response is not None:
            return response

        # Misc routes
        response = await self._dispatch_misc_routes(connection, request, got)
        if response is not None:
            return response

        # API 404 (never serve SPA for /api/ routes)
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # Static SPA serving
        if self.static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    def _log_slow_http(self, path: str, response: Any | None, started: float) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms < _SLOW_WEBUI_HTTP_LOG_MS:
            return
        if not (path.startswith("/api/") or path == "/webui/bootstrap"):
            return
        status = getattr(response, "status_code", None)
        self._log.warning(
            "slow webui http route path={} status={} duration_ms={}",
            path,
            status if status is not None else "none",
            elapsed_ms,
        )

    # -- Token issue --------------------------------------------------------

    def _handle_token_issue(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self._log.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        if not self.tokens.can_issue():
            self._log.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self.tokens.issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = self.tokens.issue_token(self.config.token_ttl_s)
        return _http_json_response(token_response_payload(token_value, self.config.token_ttl_s))

    # -- Bootstrap ----------------------------------------------------------

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        is_local_browser = _is_local_browser_request(connection, request.headers)
        is_proxy_authenticated = _is_trusted_proxy_authenticated_request(
            connection,
            request.headers,
            self.config,
        )
        if not is_proxy_authenticated:
            if secret:
                if not _issue_route_secret_matches(request.headers, secret):
                    return _http_error(401, "Unauthorized")
            elif not is_local_browser:
                return _http_error(403, "bootstrap is localhost-only")

        if is_proxy_authenticated:
            payload = {
                "ws_path": _normalize_config_path(self.config.path),
                "ws_url": self._bootstrap_ws_url(request),
                "limits": self.ingress.bootstrap_limits(
                    max_frame_bytes=self.config.max_message_bytes,
                ),
                "model_name": _resolve_bootstrap_model_name(self.runtime_model_name),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._capabilities,
            }
            return _http_json_response(payload)

        api_token_allowed = bool(secret) or is_local_browser
        if not self.tokens.can_issue(include_api_token=api_token_allowed):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = self.tokens.issue_token(self.config.token_ttl_s, audience="webui")
        api_token = (
            self.tokens.issue_api_token(self.config.token_ttl_s)
            if api_token_allowed
            else None
        )

        ws_url = self._bootstrap_ws_url(request)
        expected_path = _normalize_config_path(self.config.path)
        payload = {
            "token": token,
            "ws_path": expected_path,
            "ws_url": ws_url,
            "expires_in": self.config.token_ttl_s,
            "limits": self.ingress.bootstrap_limits(
                max_frame_bytes=self.config.max_message_bytes,
            ),
            "model_name": _resolve_bootstrap_model_name(self.runtime_model_name),
            "runtime_surface": self._runtime_surface,
            "runtime_capabilities": self._capabilities,
        }
        if api_token is not None:
            payload["api_token"] = api_token
        return _http_json_response(payload)

    def _bootstrap_ws_url(self, request: Any) -> str:
        headers = getattr(request, "headers", {}) or {}
        if self.config.public_ws_url:
            return self.config.public_ws_url
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)
        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        expected_path = _normalize_config_path(self.config.path)
        return f"{scheme}://{host}{expected_path}"

    # -- Session routes -----------------------------------------------------

    async def _dispatch_session_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/file-preview$", got)
        if m:
            return self._handle_file_preview(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/automations$", got)
        if m:
            return self._handle_session_automations(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        return None

    async def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        payload = await asyncio.to_thread(self._sessions_list_payload)
        return _http_json_response(
            payload,
            accept_encoding=_combined_list_header(request.headers, "Accept-Encoding"),
        )

    def _sessions_list_payload(self) -> dict[str, Any]:
        assert self.session_manager is not None
        sessions = list_webui_sessions(self.session_manager)
        from nanoinfra.session.webui_turns import websocket_turn_wall_started_at

        cleaned: list[dict[str, Any]] = []
        default_scope: WorkspaceScope | None = None
        for s in sessions:
            key = s.get("key")
            if not (isinstance(key, str) and key.startswith("websocket:")):
                continue
            row = {
                k: v
                for k, v in s.items()
                if k != "path" and k not in WEBUI_SESSION_INDEX_INTERNAL_FIELDS
            }
            chat_id = key.split(":", 1)[1]
            started_at = websocket_turn_wall_started_at(chat_id)
            if started_at is not None:
                row["run_started_at"] = started_at
            if default_scope is None:
                default_scope = self.workspaces.default_scope()
            scope_present, raw_scope = indexed_workspace_scope(s)
            scope = self.workspaces.scope_for_indexed_metadata(
                raw_scope,
                scope_present=scope_present,
                default_scope=default_scope,
            )
            row["workspace_scope"] = scope.payload()
            cleaned.append(row)
        return {"sessions": cleaned}

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self.session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            session_messages = cast(list[dict[str, Any]], messages)
            scrub_subagent_messages_for_channel(session_messages)
            raw_session_messages = cast(list[Any], messages)
            data["messages"] = public_history_messages(
                [
                    cast(dict[str, Any], message)
                    for message in raw_session_messages
                    if isinstance(message, dict)
                ]
            )
        self.media.augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        scope = self.workspaces.scope_for_session_key(decoded_key)

        def load_session_messages() -> list[dict[str, Any]] | None:
            if self.session_manager is None:
                return None
            session_data = self.session_manager.read_session_file(decoded_key)
            raw_messages = session_data.get("messages") if isinstance(session_data, dict) else None
            if not isinstance(raw_messages, list):
                return None
            raw_session_messages = cast(list[Any], raw_messages)
            return [
                cast(dict[str, Any], raw_message)
                for raw_message in raw_session_messages
                if isinstance(raw_message, dict)
            ]

        query = _parse_query(request.path)
        raw_limit = _query_first(query, "limit")
        limit: int | None = None
        if raw_limit is not None and raw_limit.strip():
            try:
                limit = int(raw_limit)
            except ValueError:
                return _http_error(400, "invalid limit")
        direction = _query_first(query, "direction")
        if direction is not None and direction not in {"latest"}:
            return _http_error(400, "invalid direction")
        before = _query_first(query, "before")
        from nanoinfra.session.webui_turns import (
            websocket_turn_id,
            websocket_turn_transcript_persistence_failed,
            websocket_turn_wall_started_at,
        )

        chat_id = decoded_key.split(":", 1)[1]
        active_turn_started_at = websocket_turn_wall_started_at(chat_id)
        active_turn_id = websocket_turn_id(chat_id)
        active_turn_transcript_persistence_failed = (
            websocket_turn_transcript_persistence_failed(chat_id)
        )
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self.media.augment_transcript_media,
            augment_assistant_media=self.media.augment_transcript_media,
            augment_assistant_text=lambda text: self.media.rewrite_local_markdown_images(
                text,
                workspace_path=scope.project_path,
            ),
            session_messages_loader=load_session_messages,
            active_turn_started_at=active_turn_started_at,
            active_turn_id=active_turn_id,
            active_turn_transcript_persistence_failed=(
                active_turn_transcript_persistence_failed
            ),
            limit=limit,
            direction=direction,
            before=before,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        data["workspace_scope"] = scope.payload()
        self._maybe_retry_webui_title(decoded_key, chat_id)
        return _http_json_response(
            data,
            accept_encoding=_combined_list_header(request.headers, "Accept-Encoding"),
        )

    def _maybe_retry_webui_title(self, session_key: str, chat_id: str) -> None:
        """Opportunistically retry title generation whenever a thread is opened.

        Title generation after a turn (``_schedule_title_update_from_event``)
        is fire-and-forget, so a failure there leaves the session titleless
        forever with nothing to retry it. ``maybe_generate_webui_title`` is
        itself a no-op once a usable title already exists, so it's safe to
        call again every time the WebUI opens/reopens this thread.
        """
        sessions = self.session_manager
        if sessions is None or self.default_llm_runtime is None:
            return
        if session_key in self._title_retry_in_flight:
            return
        runtime = self.default_llm_runtime()
        if runtime is None:
            return
        self._title_retry_in_flight.add(session_key)

        async def _retry() -> None:
            from nanoinfra.bus.outbound_events import (
                SessionUpdatedEvent,
                outbound_message_for_event,
            )
            from nanoinfra.session.webui_turns import maybe_generate_webui_title

            try:
                generated = await maybe_generate_webui_title(
                    sessions=sessions,
                    session_key=session_key,
                    provider=runtime.provider,
                    model=runtime.model,
                )
                if generated:
                    await self.bus.publish_outbound(
                        outbound_message_for_event(
                            channel="websocket",
                            chat_id=chat_id,
                            event=SessionUpdatedEvent(scope="metadata"),
                        )
                    )
            except Exception:
                self._log.warning(
                    "WebUI title retry-on-open failed for {}", session_key, exc_info=True
                )
            finally:
                self._title_retry_in_flight.discard(session_key)

        asyncio.create_task(_retry())

    def _handle_file_preview(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        query = _parse_query(request.path)
        path = _query_first(query, "path")
        is_probe = _query_first(query, "probe") == "1"
        try:
            scope = self.workspaces.scope_for_session_key(decoded_key)
            if is_probe:
                payload = file_preview_availability_payload(path, scope=scope)
            else:
                payload = file_preview_payload(path, scope=scope)
        except WebUIFilePreviewError as e:
            if is_probe and e.status in {400, 403, 404, 415}:
                return _http_json_response({"available": False})
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_session_automations(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        pending_job_ids = self._pending_automation_ids_for_session(decoded_key)
        return _http_json_response(
            session_automations_payload(
                self.cron_service,
                decoded_key,
                local_trigger_store=self.local_trigger_store,
                pending_job_ids=pending_job_ids,
            )
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        query = _parse_query(request.path)
        delete_automations = (_query_first(query, "delete_automations") or "").lower()
        automation_jobs = session_automation_jobs(
            self.cron_service,
            decoded_key,
            local_trigger_store=self.local_trigger_store,
        )
        if automation_jobs and delete_automations not in {"1", "true", "yes"}:
            return _http_json_response(
                {
                    "deleted": False,
                    "blocked_by_automations": True,
                    "automations": serialize_automation_jobs(automation_jobs),
                }
            )
        if automation_jobs:
            for job in automation_jobs:
                if isinstance(job, LocalTrigger):
                    if self.local_trigger_store is not None:
                        self.local_trigger_store.delete(job.id)
                elif self.cron_service is not None:
                    self.cron_service.remove_job(job.id)
        deleted = self.session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    # -- Automation routes --------------------------------------------------

    async def _dispatch_automation_routes(
        self,
        request: WsRequest,
        got: str,
    ) -> Response | None:
        if got == "/api/webui/automations":
            return self._handle_webui_automations(request)
        m = re.match(r"^/api/webui/automations/(enable|disable|delete|run|update)$", got)
        if m:
            return await self._handle_webui_automation_action(request, m.group(1))
        return None

    def _pending_cron_job_ids_for_all(self) -> set[str]:
        if self.cron_service is None or self.cron_pending_job_ids is None:
            return set()
        pending: set[str] = set()
        for job in self.cron_service.list_jobs(include_disabled=True):
            session_key = job.payload.session_key
            if not session_key and job.payload.origin_channel and job.payload.origin_chat_id:
                session_key = f"{job.payload.origin_channel}:{job.payload.origin_chat_id}"
            if session_key:
                pending.update(self.cron_pending_job_ids(session_key))
        return pending

    def _pending_local_trigger_ids_for_all(self) -> set[str]:
        if self.local_trigger_store is None or self.local_trigger_pending_ids is None:
            return set()
        pending: set[str] = set()
        for trigger in self.local_trigger_store.list_triggers(include_disabled=True):
            session_key = trigger.session_key
            if not session_key and trigger.channel and trigger.chat_id:
                session_key = f"{trigger.channel}:{trigger.chat_id}"
            if session_key:
                pending.update(self.local_trigger_pending_ids(session_key))
        return pending

    def _pending_automation_ids_for_session(self, session_key: str) -> set[str]:
        pending: set[str] = set()
        if self.cron_pending_job_ids is not None:
            pending.update(self.cron_pending_job_ids(session_key))
        if self.local_trigger_pending_ids is not None:
            pending.update(self.local_trigger_pending_ids(session_key))
        return pending

    def _handle_webui_automations(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        pending_job_ids = self._pending_cron_job_ids_for_all()
        pending_job_ids.update(self._pending_local_trigger_ids_for_all())
        return _http_json_response(
            all_automations_payload(
                self.cron_service,
                local_trigger_store=self.local_trigger_store,
                session_manager=self.session_manager,
                pending_job_ids=pending_job_ids,
            )
        )

    async def _handle_webui_automation_action(
        self,
        request: WsRequest,
        action: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.cron_service is None and self.local_trigger_store is None:
            return _http_error(503, "automation service unavailable")

        query = _parse_query(request.path)
        job_id = (_query_first(query, "id") or _query_first(query, "job_id") or "").strip()
        if not job_id:
            return _http_error(400, "missing automation id")
        trigger = self.local_trigger_store.get(job_id) if self.local_trigger_store else None
        if trigger is not None:
            return self._handle_local_trigger_action(request, action, trigger)

        if self.cron_service is None:
            return _http_error(404, "automation not found")
        job = self.cron_service.get_job(job_id)
        if job is None:
            return _http_error(404, "automation not found")
        if job.payload.kind == "system_event":
            return _http_error(403, "system automation is protected")
        if action in {"enable", "run"} and not is_bound_cron_job(job):
            return _http_error(409, "automation has no linked chat")

        if action == "enable":
            if self.cron_service.enable_job(job_id, enabled=True) is None:
                return _http_error(404, "automation not found")
        elif action == "disable":
            if self.cron_service.enable_job(job_id, enabled=False) is None:
                return _http_error(404, "automation not found")
        elif action == "delete":
            result = self.cron_service.remove_job(job_id)
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        elif action == "run":
            if not job.enabled:
                return _http_error(409, "automation is disabled")
            task = asyncio.create_task(self.cron_service.run_job(job_id, force=False))
            task.add_done_callback(self._log_automation_run_result)
        elif action == "update":
            values = _automation_values_from_request(request)
            if values is None:
                return _http_error(400, "invalid automation update payload")
            parsed = _parse_automation_update(values, current_job=job)
            if isinstance(parsed, str):
                return _http_error(400, parsed)
            try:
                result = self.cron_service.update_job(job_id, **parsed)
            except ValueError as exc:
                return _http_error(400, str(exc))
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        else:
            return _http_error(404, "unknown automation action")

        return self._handle_webui_automations(request)

    def _handle_local_trigger_action(
        self,
        request: WsRequest,
        action: str,
        trigger: LocalTrigger,
    ) -> Response:
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        if action == "enable":
            if self.local_trigger_store.enable(trigger.id, enabled=True) is None:
                return _http_error(404, "automation not found")
        elif action == "disable":
            if self.local_trigger_store.enable(trigger.id, enabled=False) is None:
                return _http_error(404, "automation not found")
        elif action == "delete":
            if not self.local_trigger_store.delete(trigger.id):
                return _http_error(404, "automation not found")
        elif action == "run":
            return _http_error(409, "local trigger requires a CLI message")
        elif action == "update":
            values = _automation_values_from_request(request)
            if values is None:
                return _http_error(400, "invalid automation update payload")
            parsed = _parse_local_trigger_update(values)
            if isinstance(parsed, str):
                return _http_error(400, parsed)
            if parsed:
                if self.local_trigger_store.update(trigger.id, **parsed) is None:
                    return _http_error(404, "automation not found")
        else:
            return _http_error(404, "unknown automation action")

        return self._handle_webui_automations(request)

    @staticmethod
    def _log_automation_run_result(task: asyncio.Task[bool]) -> None:
        try:
            ran = task.result()
        except Exception:
            logger.exception("WebUI automation run-now task failed")
            return
        if not ran:
            logger.warning("WebUI automation run-now task did not execute")

    # -- Diagram routes -------------------------------------------------------

    def _dispatch_diagram_routes(self, request: WsRequest, got: str) -> Response | None:
        if got == "/api/webui/diagrams":
            return self._handle_webui_diagrams(request)
        if got == "/api/webui/diagrams/catalog":
            return self._handle_webui_diagram_catalog(request)
        if got == "/api/webui/diagrams/create":
            return self._handle_webui_diagram_create(request)
        m = re.match(r"^/api/webui/diagrams/([^/]+)/update$", got)
        if m:
            return self._handle_webui_diagram_update(request, m.group(1))
        m = re.match(r"^/api/webui/diagrams/([^/]+)/delete$", got)
        if m:
            return self._handle_webui_diagram_delete(request, m.group(1))
        m = re.match(r"^/api/webui/diagrams/([^/]+)$", got)
        if m:
            return self._handle_webui_diagram_detail(request, m.group(1))
        return None

    def _handle_webui_diagrams(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(webui_diagrams_payload(self.diagrams))

    def _handle_webui_diagram_catalog(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            webui_diagram_catalog_payload(
                self.skills_workspace_path,
                skills_workspace_path=self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            )
        )

    def _handle_webui_diagram_detail(self, request: WsRequest, diagram_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        payload = webui_diagram_detail_payload(self.diagrams, diagram_id)
        if payload is None:
            return _http_error(404, "diagram not found")
        return _http_json_response(payload)

    def _handle_webui_diagram_create(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        values = _diagram_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid diagram payload")
        try:
            payload = create_webui_diagram(self.diagrams, values)
        except DiagramValidationError as exc:
            return _http_error(400, str(exc))
        return _http_json_response(payload)

    def _handle_webui_diagram_update(self, request: WsRequest, diagram_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        values = _diagram_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid diagram payload")
        try:
            payload = update_webui_diagram(self.diagrams, diagram_id, values)
        except DiagramValidationError as exc:
            return _http_error(400, str(exc))
        if payload is None:
            return _http_error(404, "diagram not found")
        return _http_json_response(payload)

    def _handle_webui_diagram_delete(self, request: WsRequest, diagram_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not delete_webui_diagram(self.diagrams, diagram_id):
            return _http_error(404, "diagram not found")
        return _http_json_response({"deleted": True})

    # -- Secret routes ---------------------------------------------------------

    def _dispatch_secret_routes(self, request: WsRequest, got: str) -> Response | None:
        if got == "/api/webui/secrets":
            return self._handle_webui_secrets(request)
        if got == "/api/webui/secrets/create":
            return self._handle_webui_secret_create(request)
        m = re.match(r"^/api/webui/secrets/([^/]+)/update$", got)
        if m:
            return self._handle_webui_secret_update(request, m.group(1))
        m = re.match(r"^/api/webui/secrets/([^/]+)/delete$", got)
        if m:
            return self._handle_webui_secret_delete(request, m.group(1))
        m = re.match(r"^/api/webui/secrets/([^/]+)$", got)
        if m:
            return self._handle_webui_secret_detail(request, m.group(1))
        return None

    def _handle_webui_secrets(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(webui_secrets_payload(self.secrets))

    def _handle_webui_secret_detail(self, request: WsRequest, secret_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        payload = webui_secret_detail_payload(self.secrets, secret_id)
        if payload is None:
            return _http_error(404, "secret not found")
        return _http_json_response(payload)

    def _handle_webui_secret_create(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        values = _secret_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid secret payload")
        try:
            payload = create_webui_secret(self.secrets, values)
        except SecretValidationError as exc:
            return _http_error(400, str(exc))
        except (SecretsNotConfiguredError, PostgresSecretsNotConfiguredError) as exc:
            return _http_error(409, str(exc))
        return _http_json_response(payload)

    def _handle_webui_secret_update(self, request: WsRequest, secret_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        values = _secret_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid secret payload")
        try:
            payload = update_webui_secret(self.secrets, secret_id, values)
        except SecretValidationError as exc:
            return _http_error(400, str(exc))
        except (SecretsNotConfiguredError, PostgresSecretsNotConfiguredError) as exc:
            return _http_error(409, str(exc))
        if payload is None:
            return _http_error(404, "secret not found")
        return _http_json_response(payload)

    def _handle_webui_secret_delete(self, request: WsRequest, secret_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not delete_webui_secret(self.secrets, secret_id):
            return _http_error(404, "secret not found")
        return _http_json_response({"deleted": True})

    # -- Server routes ---------------------------------------------------------

    def _dispatch_server_routes(self, request: WsRequest, got: str) -> Response | None:
        if got == "/api/webui/servers":
            return self._handle_webui_servers(request)
        if got == "/api/webui/servers/create":
            return self._handle_webui_server_create(request)
        m = re.match(r"^/api/webui/servers/([^/]+)/update$", got)
        if m:
            return self._handle_webui_server_update(request, m.group(1))
        m = re.match(r"^/api/webui/servers/([^/]+)/delete$", got)
        if m:
            return self._handle_webui_server_delete(request, m.group(1))
        m = re.match(r"^/api/webui/servers/([^/]+)$", got)
        if m:
            return self._handle_webui_server_detail(request, m.group(1))
        return None

    def _handle_webui_servers(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(webui_servers_payload(self.servers))

    def _handle_webui_server_detail(self, request: WsRequest, server_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        payload = webui_server_detail_payload(self.servers, server_id)
        if payload is None:
            return _http_error(404, "server not found")
        return _http_json_response(payload)

    def _handle_webui_server_create(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        values = _server_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid server payload")
        try:
            payload = create_webui_server(self.servers, values)
        except ServerValidationError as exc:
            return _http_error(400, str(exc))
        return _http_json_response(payload)

    def _handle_webui_server_update(self, request: WsRequest, server_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        values = _server_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid server payload")
        try:
            payload = update_webui_server(self.servers, server_id, values)
        except ServerValidationError as exc:
            return _http_error(400, str(exc))
        if payload is None:
            return _http_error(404, "server not found")
        return _http_json_response(payload)

    def _handle_webui_server_delete(self, request: WsRequest, server_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not delete_webui_server(self.servers, server_id):
            return _http_error(404, "server not found")
        return _http_json_response({"deleted": True})

    # -- Latch routes -------------------------------------------------------

    def _dispatch_latch_routes(self, request: WsRequest, got: str) -> Response | None:
        """The read and the clear for a denial latch (#28).

        Both routes need the API token, because the clear is the one control that lifts a
        terminal denial. A missing surface answers 503 and never an empty list: a WebUI that
        cannot see the gate must not read as "no session is latched".
        """
        if got not in (LATCH_READ_PATH, LATCH_CLEAR_PATH):
            return None
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.latch is None:
            return _http_error(503, "the gate runtime is not available on this gateway")
        if got == LATCH_READ_PATH:
            return _http_json_response(self.latch.payload())
        values = latch_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid latch payload")
        try:
            cleared = self.latch.clear(values, actor=operator_actor(request, self.config))
        except LatchClearError as exc:
            return _http_error(400, str(exc))
        return _http_json_response(cleared)

    def _dispatch_audit_routes(self, request: WsRequest, got: str) -> Response | None:
        """The read of the gate audit log (#29).

        The route reads. It answers 405 for every other method, so "the viewer offers no delete
        control" holds at the server and not at the layout. The API token is required, because a
        record names sessions, hosts, and actors.
        """
        if got != AUDIT_READ_PATH:
            return None
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        method = str(getattr(request, "method", "GET") or "GET").upper()
        if method not in ("GET", "HEAD"):
            return _http_error(405, "the audit log is append-only and this route only reads")
        if self.audit is None:
            return _http_error(503, "the gate runtime is not available on this gateway")
        return _http_json_response(self.audit.page(_parse_query(request.path)))

    # -- Media routes -------------------------------------------------------

    def _dispatch_media_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2), request)
        return None

    def _handle_media_fetch(
        self, sig: str, payload: str, request: WsRequest | None = None
    ) -> Response:
        return self.media.serve_signed_media(
            sig,
            payload,
            request=request,
        )

    # -- Misc routes --------------------------------------------------------

    async def _dispatch_misc_routes(
        self, connection: Any, request: WsRequest, got: str
    ) -> Response | None:
        if got == "/api/sessions":
            return await self._handle_sessions_list(request)
        if got == "/api/commands":
            return self._handle_commands(request)
        if got == "/api/workspaces":
            return self._handle_workspaces(connection, request)
        if got == "/api/webui/skills/search":
            return await self._handle_webui_skills_search(request)
        if got == "/api/webui/skills/trending":
            return await self._handle_webui_skills_trending(request)
        if got == "/api/webui/skills/trends":
            return await self._handle_webui_skill_trends(request)
        if got == "/api/webui/skills/install":
            return await self._handle_webui_skill_install(connection, request)
        if got == "/api/webui/skills/update":
            return self._handle_webui_skill_update(request)
        if got == "/api/webui/skills/delete":
            return self._handle_webui_skill_delete(connection, request)
        if got == "/api/webui/skills":
            return self._handle_webui_skills(request)
        m = re.match(r"^/api/webui/skills/([^/]+)$", got)
        if m:
            return self._handle_webui_skill_detail(request, m.group(1))
        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)
        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)
        return None

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_workspaces(self, connection: Any, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            self.workspaces.payload(
                controls_available=self.workspace_controls_available(connection)
            )
        )

    def _handle_webui_skills(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            )
        )

    async def _handle_webui_skills_search(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        params = _parse_query(request.path)
        query = _query_first(params, "q") or ""
        provider = _query_first(params, "provider") or "all"
        try:
            payload = await search_marketplace_skills(
                query,
                self.skills_workspace_path,
                provider=provider,
                nanoinfra_base_url=self.nanoinfra_skills_base_url,
            )
        except SkillsMarketplaceError as exc:
            return _http_error(exc.status, exc.message)
        except Exception:
            self._log.exception("skills marketplace search failed")
            return _http_error(500, "skills marketplace search failed")
        return _http_json_response(payload)

    async def _handle_webui_skills_trending(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        provider = _query_first(_parse_query(request.path), "provider") or "all"
        try:
            payload = await trending_marketplace_skills(
                self.skills_workspace_path,
                provider=provider,
                nanoinfra_base_url=self.nanoinfra_skills_base_url,
            )
        except SkillsMarketplaceError as exc:
            return _http_error(exc.status, exc.message)
        except Exception:
            self._log.exception("skills marketplace trending lookup failed")
            return _http_error(500, "skills marketplace trending lookup failed")
        return _http_json_response(payload)

    async def _handle_webui_skill_trends(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        skill_ids = _parse_query(request.path).get("id", [])
        try:
            payload = await marketplace_skill_trends(skill_ids)
        except Exception:
            self._log.exception("skills.sh trend history lookup failed")
            return _http_error(500, "skills.sh trend history lookup failed")
        return _http_json_response(payload)

    async def _handle_webui_skill_install(
        self,
        connection: Any,
        request: WsRequest,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not self._allow_webui_package_install(connection, request):
            return _http_error(403, "remote skill installation is disabled")
        if self._skill_install_lock.locked():
            return _http_error(409, "another skill installation is already in progress")

        query = _parse_query(request.path)
        provider = _query_first(query, "provider") or "skills_sh"
        source = _query_first(query, "source") or ""
        skill_id = _query_first(query, "skill") or ""
        version = _query_first(query, "version") or ""
        async with self._skill_install_lock:
            try:
                action = await install_marketplace_skill(
                    source,
                    skill_id,
                    self.skills_workspace_path,
                    provider=provider,
                    version=version,
                    nanoinfra_base_url=self.nanoinfra_skills_base_url,
                )
            except SkillsMarketplaceError as exc:
                return _http_error(exc.status, exc.message)
            except Exception:
                self._log.exception("skill installation failed")
                return _http_error(500, "skill installation failed")
        return _http_json_response({
            **webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            ),
            "last_action": action,
        })

    def _allow_webui_package_install(self, connection: Any, request: WsRequest) -> bool:
        if _is_local_browser_request(connection, request.headers):
            return True
        try:
            from nanoinfra.config.loader import load_config

            return bool(load_config().tools.webui_allow_remote_package_install)
        except Exception:
            self._log.exception("failed to load remote package install policy")
            return False

    def _handle_webui_skill_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        name = _query_first(query, "name") or ""
        raw_enabled = (_query_first(query, "enabled") or "").lower()
        if raw_enabled not in {"true", "false"}:
            return _http_error(400, "enabled must be true or false")
        try:
            action = set_webui_skill_enabled(
                self.skills_workspace_path,
                name,
                enabled=raw_enabled == "true",
                disabled_skills=self.disabled_skills,
            )
        except SkillManagementError as exc:
            return _http_error(exc.status, exc.message)
        self._apply_skill_state()
        return _http_json_response({
            **webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            ),
            "last_action": action,
        })

    def _handle_webui_skill_delete(
        self,
        connection: Any,
        request: WsRequest,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not _is_local_browser_request(connection, request.headers):
            return _http_error(403, "remote skill deletion is disabled")
        name = _query_first(_parse_query(request.path), "name") or ""
        try:
            action = delete_webui_skill(
                self.skills_workspace_path,
                name,
                disabled_skills=self.disabled_skills,
            )
        except SkillManagementError as exc:
            return _http_error(exc.status, exc.message)
        self._apply_skill_state()
        return _http_json_response({
            **webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            ),
            "last_action": action,
        })

    def _apply_skill_state(self) -> None:
        if self.skill_state_action is not None:
            self.skill_state_action(set(self.disabled_skills))

    def _handle_webui_skill_detail(self, request: WsRequest, raw_name: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        name = unquote(raw_name)
        if not name or "/" in name or "\\" in name:
            return _http_error(400, "invalid skill name")
        payload = webui_skill_detail_payload(
            self.skills_workspace_path,
            name,
            disabled_skills=self.disabled_skills,
        )
        if payload is None:
            return _http_error(404, "skill not found")
        return _http_json_response(payload)

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state())

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(cast(dict[str, Any], decoded))
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self._log.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    # -- Static file serving ------------------------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        assert self.static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self.static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self.static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            index = self.static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self._log.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )


def _automation_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    raw = _case_insensitive_header(request.headers, _AUTOMATION_VALUES_HEADER)
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return cast(dict[str, Any], values) if isinstance(values, dict) else None


def _diagram_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    """Read the full diagram body from ``X-Nanoinfra-Diagram-Values``.

    Unlike ``_automation_values_from_request``, a missing header is *not*
    "no changes" here — create/update always need a complete diagram body,
    so a missing or malformed header is treated the same: an invalid payload.
    """
    raw = _case_insensitive_header(request.headers, _DIAGRAM_VALUES_HEADER)
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return cast(dict[str, Any], values) if isinstance(values, dict) else None


def _secret_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    """Read the full secret body from ``X-Nanoinfra-Secret-Values`` -- same
    shape as ``_diagram_values_from_request``, a missing/malformed header is
    always an invalid payload (create/update need a complete body)."""
    raw = _case_insensitive_header(request.headers, _SECRET_VALUES_HEADER)
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return cast(dict[str, Any], values) if isinstance(values, dict) else None


def _server_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    """Read the full server body from ``X-Nanoinfra-Server-Values`` -- same
    shape as ``_diagram_values_from_request``, a missing/malformed header is
    always an invalid payload (create/update need a complete body)."""
    raw = _case_insensitive_header(request.headers, _SERVER_VALUES_HEADER)
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return cast(dict[str, Any], values) if isinstance(values, dict) else None


def _parse_automation_update(
    values: dict[str, Any],
    *,
    current_job: CronJob | None = None,
) -> dict[str, Any] | str:
    update: dict[str, Any] = {}
    if "name" in values:
        raw_name = values.get("name")
        if not isinstance(raw_name, str):
            return "name must be a string"
        name = raw_name.strip()
        if not name:
            return "name cannot be empty"
        update["name"] = name
    if "message" in values:
        raw_message = values.get("message")
        if not isinstance(raw_message, str):
            return "message must be a string"
        message = raw_message.strip()
        if not message:
            return "message cannot be empty"
        update["message"] = message
    if "schedule" in values:
        raw_schedule = values.get("schedule")
        if not isinstance(raw_schedule, dict):
            return "schedule must be an object"
        parsed_schedule = _parse_automation_schedule(cast(dict[str, Any], raw_schedule))
        if isinstance(parsed_schedule, str):
            return parsed_schedule
        if current_job is not None and _schedule_matches_job(parsed_schedule, current_job):
            return update
        schedule_error = _validate_automation_schedule(parsed_schedule)
        if schedule_error:
            return schedule_error
        update["schedule"] = parsed_schedule
        update["delete_after_run"] = parsed_schedule.kind == "at"
    return update


def _parse_local_trigger_update(values: dict[str, Any]) -> dict[str, Any] | str:
    update: dict[str, Any] = {}
    if "name" in values:
        raw_name = values.get("name")
        if not isinstance(raw_name, str):
            return "name must be a string"
        name = raw_name.strip()
        if not name:
            return "name cannot be empty"
        update["name"] = name
    forbidden = [key for key in ("message", "schedule") if key in values]
    if forbidden:
        return "local trigger updates only support name"
    return update


def _parse_automation_schedule(values: dict[str, Any]) -> CronSchedule | str:
    raw_kind = values.get("kind")
    if not isinstance(raw_kind, str):
        return "schedule kind must be a string"
    kind = raw_kind.strip()
    if kind == "every":
        every_ms = _positive_int(values.get("every_ms"))
        if every_ms is None:
            return "every schedule requires positive every_ms"
        return CronSchedule(kind="every", every_ms=every_ms)
    if kind == "cron":
        raw_expr = values.get("expr")
        if not isinstance(raw_expr, str):
            return "cron schedule requires expr"
        expr = raw_expr.strip()
        if not expr:
            return "cron schedule requires expr"
        raw_tz = values.get("tz")
        if raw_tz is not None and not isinstance(raw_tz, str):
            return "cron schedule timezone must be a string"
        tz = raw_tz.strip() if isinstance(raw_tz, str) else ""
        return CronSchedule(kind="cron", expr=expr, tz=tz or None)
    if kind == "at":
        at_ms = _positive_int(values.get("at_ms"))
        if at_ms is None:
            return "one-time schedule requires positive at_ms"
        return CronSchedule(kind="at", at_ms=at_ms)
    return "unknown schedule kind"


def _schedule_matches_job(schedule: CronSchedule, job: CronJob) -> bool:
    current = job.schedule
    if schedule.kind != current.kind:
        return False
    if schedule.kind == "at":
        return schedule.at_ms == current.at_ms
    if schedule.kind == "every":
        return schedule.every_ms == current.every_ms
    if schedule.kind == "cron":
        return (schedule.expr or "") == (current.expr or "") and (
            schedule.tz or None
        ) == (current.tz or None)
    return False


def _validate_automation_schedule(schedule: CronSchedule) -> str | None:
    if schedule.kind == "at":
        if not schedule.at_ms or schedule.at_ms <= int(time.time() * 1000):
            return "one-time schedule must be in the future"
        return None
    if schedule.kind != "cron":
        return None

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from croniter import croniter

        tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
        base = datetime.now(tz=tz)
        croniter(cast(str, schedule.expr), base).get_next(datetime)
    except Exception:
        return "cron schedule is invalid"
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _is_websocket_channel_session_key(key: str) -> bool:
    return key.startswith("websocket:")
