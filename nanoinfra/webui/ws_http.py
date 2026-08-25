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
from urllib.parse import quote, unquote

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanoinfra.automations.delivery import DELIVERY_POLICIES
from nanoinfra.automations.state import AutomationStateError, AutomationStateStore
from nanoinfra.command.builtin import builtin_command_palette
from nanoinfra.config.loader import load_config
from nanoinfra.cron.session_turns import is_bound_cron_job
from nanoinfra.cron.types import CronJob, CronSchedule
from nanoinfra.diagrams.normalize import DiagramValidationError
from nanoinfra.diagrams.seed import seed_example_diagram_if_new_workspace
from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.runtime_context import public_history_messages
from nanoinfra.secrets.crypto import SecretsNotConfiguredError
from nanoinfra.secrets.normalize import SecretValidationError
from nanoinfra.secrets.postgres_backend import PostgresSecretsNotConfiguredError
from nanoinfra.secrets.store import SecretsStoreUnreadableError, SecretStore
from nanoinfra.security.workspace_access import WorkspaceScope, build_workspace_scope
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.normalize import ServerValidationError
from nanoinfra.servers.store import ServerStore
from nanoinfra.triggers.local_store import TriggerDisabledError, TriggerNotFoundError
from nanoinfra.triggers.local_types import LocalTrigger
from nanoinfra.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanoinfra.webui.approvals_api import (
    APPROVALS_ANSWER_PATH,
    APPROVALS_READ_PATH,
    ApprovalAnswerError,
    ApprovalsOperatorSurface,
    approval_values_from_request,
)
from nanoinfra.webui.assertion_identity import (
    TrustedProxyAuthenticator,
    build_trusted_proxy_authenticator,
)
from nanoinfra.webui.audit_api import (
    AUDIT_READ_PATH,
    AuditReadSurface,
)
from nanoinfra.webui.commissioning_api import (
    CommissioningOperatorSurface,
    PromotionRefusedError,
)
from nanoinfra.webui.diagrams_api import (
    create_webui_diagram,
    delete_webui_diagram,
    update_webui_diagram,
    webui_diagram_catalog_payload,
    webui_diagram_detail_payload,
    webui_diagrams_payload,
)
from nanoinfra.webui.file_browser import (
    WebUIFileBrowserError,
    create_directory,
    delete_entry,
    directory_listing_payload,
    move_entry,
    read_file_for_download,
    rename_entry,
    resolve_within_workspace,
)
from nanoinfra.webui.file_preview import (
    WebUIFilePreviewError,
    file_preview_availability_payload,
    file_preview_payload,
)
from nanoinfra.webui.gateway_tokens import (
    OPERATE_SCOPE,
    GatewayTokenStore,
    TokenScope,
    token_response_payload,
)
from nanoinfra.webui.http_utils import (
    TRUSTED_PROXY_AUTHENTICATED_ATTR,
    TRUSTED_PROXY_IDENTITY_ATTR,
    bearer_token,
)
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
from nanoinfra.webui.resource_mentions import normalize_resource_mentions
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
from nanoinfra.webui.workspace_roots import (
    create_workspace,
    resolve_client_workspace,
    workspaces_payload_for_root,
)
from nanoinfra.webui.workspaces import WebUIWorkspaceController

_SLOW_WEBUI_HTTP_LOG_MS = 1_000
_AUTOMATION_VALUES_HEADER = "X-Nanoinfra-Automation-Values"
_DIAGRAM_VALUES_HEADER = "X-Nanoinfra-Diagram-Values"

#: A diagram body is larger than one HTTP header line allows, so it travels in numbered chunks (#92).
#:
#: ``websockets`` drops a request line over ``MAX_LINE_LENGTH`` (8192 bytes) with no status code and no
#: error body: the connection closes and the browser shows a network error. The limit is bytes and not
#: nodes, so an operator could not predict where their canvas became unsavable, and the 11-node seeded
#: example already used 60% of it.
#:
#: ``process_request`` in ``websockets`` exposes the request line and the headers and never a body, so
#: a POST body is not available on this transport. Chunked headers are what it can carry.
_DIAGRAM_CHUNK_COUNT_HEADER = "X-Nanoinfra-Diagram-Chunks"

#: Bytes per chunk. A header line is ``name: value\r\n``, the names here are under 50 bytes, and 6000
#: leaves room for that plus the safety margin. ``MAX_NUM_HEADERS`` is 128, so this carries about
#: 700 KB of body -- far past any diagram, and still a bound.
_DIAGRAM_CHUNK_BYTES = 6000


def diagram_values_headers(payload: dict[str, Any]) -> dict[str, str]:
    """The headers that carry one diagram body, split so no line exceeds the limit (#92).

    Kept beside the reader so the two cannot drift, and exported for the WebUI's own client to mirror.
    """
    encoded = quote(json.dumps(payload, ensure_ascii=False), safe="")
    chunks = [
        encoded[index : index + _DIAGRAM_CHUNK_BYTES]
        for index in range(0, len(encoded), _DIAGRAM_CHUNK_BYTES)
    ] or [""]
    headers = {_DIAGRAM_CHUNK_COUNT_HEADER: str(len(chunks))}
    for index, chunk in enumerate(chunks):
        headers[f"{_DIAGRAM_VALUES_HEADER}-{index}"] = chunk
    return headers
#: One workspace mutation's arguments (a parent path, a name, a new name). A header
#: rather than a body because this transport never exposes one (see the note above),
#: and no chunking because these three fields are far short of one header line --
#: unlike a diagram body, which is why that one is split.
_WORKSPACE_VALUES_HEADER = "X-Nanoinfra-Workspace-Values"
_SECRET_VALUES_HEADER = "X-Nanoinfra-Secret-Values"
_SERVER_VALUES_HEADER = "X-Nanoinfra-Server-Values"
#: One trigger's message, sent by whatever fired it. A header rather than a body because this
#: transport never exposes one (see the note above), and a header rather than the query string
#: because a query string is logged by every proxy it passes and this content becomes a prompt.
_TRIGGER_MESSAGE_HEADER = "X-Nanoinfra-Trigger-Message"
#: An optional caller-supplied key so a monitor that retries a timed-out POST does not run the
#: automation twice. A retrying caller is the normal case, not the exceptional one.
_TRIGGER_IDEMPOTENCY_HEADER = "X-Nanoinfra-Trigger-Idempotency-Key"
#: Generous for an alert line, and still a bound on what one caller can push into a prompt.
#:
#: Deliberately well under ``MAX_LINE_LENGTH`` (8192). Past that the transport drops the connection
#: with no status code and no error body, so a caller learns nothing. The point of a cap here is
#: that it is reached *first* and answers with a reason. 6000 is the same figure the diagram
#: chunking uses for the same reason.
_TRIGGER_MESSAGE_MAX_BYTES = 6000
#: How long a seen idempotency key is remembered. Long enough to cover a caller's own retry
#: window, short enough that the set cannot grow without limit.
_TRIGGER_IDEMPOTENCY_TTL_S = 900.0
#: Minimum seconds between fires of one trigger. A rate limit, not a queue: an over-rate caller is
#: told so rather than silently buffered.
_TRIGGER_MIN_INTERVAL_S = 1.0

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
        automation_state_store: AutomationStateStore | None = None,
        cron_pending_job_ids: Callable[[str], set[str]] | None = None,
        local_trigger_pending_ids: Callable[[str], set[str]] | None = None,
        channel_feature_action: Callable[..., Any] | None = None,
        channel_runtime_status: Callable[[], dict[str, Any]] | None = None,
        skill_state_action: Callable[[set[str]], None] | None = None,
        default_llm_runtime: Callable[[], LLMRuntime | None] | None = None,
        log: Any = logger,
    ) -> None:
        self.config = config
        # The seam that turns a proxy assertion into an identity (#62), built on first use and
        # kept while the config block stays the same object. The WebSocket handshake reads it
        # through this handler as well, so both admission points share one JWKS cache and one
        # rate limit. A replaced block rebuilds it, because a stale authenticator would keep
        # verifying against the issuer an operator has already changed.
        self._trusted_proxy: TrustedProxyAuthenticator | None = None
        self._trusted_proxy_block: Any = None
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
        # The inbox that answers a suspended action (#27). Attached after boot for the same
        # reason: a route with no surface answers 503, and never an empty queue.
        self.approvals: ApprovalsOperatorSurface | None = None
        # Rehearse an automation, and promote what the rehearsal found (#186). Attached after
        # boot: it needs both the cron service and a way to run a turn, and a deployment without
        # either answers 503 rather than pretending a rehearsal happened.
        self.commissioning: CommissioningOperatorSurface | None = None
        self.skill_state_action = skill_state_action
        self._skill_install_lock = asyncio.Lock()
        self._title_retry_in_flight: set[str] = set()
        self.cron_service = cron_service
        self.local_trigger_store = local_trigger_store
        self.automation_state_store = automation_state_store
        #: (trigger id, caller key) -> when it was first seen.
        self._trigger_idempotency: dict[tuple[str, str], float] = {}
        #: trigger id -> when it last fired, for the per-trigger rate limit.
        self._trigger_last_fire: dict[str, float] = {}
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
            # The posture the gate panel shows (#85). It is a read of the live block rather
            # than a copy, because an operator can replace that block while the process runs.
            trusted_proxy_auth=lambda: getattr(self.config, "trusted_proxy_auth", None),
        )

    def attach_latch_surface(self, surface: LatchOperatorSurface) -> None:
        """Take the operator half of the denial latch (#28). Only the gateway calls this."""
        self.latch = surface

    def attach_audit_surface(self, surface: AuditReadSurface) -> None:
        """Take the read of the gate audit log (#29). Only the gateway calls this."""
        self.audit = surface

    def attach_approvals_surface(self, surface: ApprovalsOperatorSurface) -> None:
        """Take the inbox that answers a suspended action (#27). Only the gateway calls this."""
        self.approvals = surface

    def attach_commissioning_surface(self, surface: CommissioningOperatorSurface) -> None:
        """Take the rehearse-and-promote surface (#186). Only the gateway calls this."""
        self.commissioning = surface

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    # -- Token management ---------------------------------------------------

    def trusted_proxy_authenticator(self) -> TrustedProxyAuthenticator | None:
        """The identity seam for the current config block, or None when no proxy is configured."""
        proxy = cast(Any, getattr(self.config, "trusted_proxy_auth", None))
        if proxy is None:
            self._trusted_proxy = None
            self._trusted_proxy_block = None
            return None
        if self._trusted_proxy_block is not proxy:
            self._trusted_proxy = build_trusted_proxy_authenticator(self.config, log=self._log)
            self._trusted_proxy_block = proxy
        return self._trusted_proxy

    def check_api_token(
        self,
        request: WsRequest,
        *,
        scope: TokenScope = OPERATE_SCOPE,
    ) -> bool:
        if getattr(request, TRUSTED_PROXY_AUTHENTICATED_ATTR, False):
            # A trusted proxy asserted a named identity, which this gateway does not scope: the
            # proxy is the authority there, and narrowing it here would be a second, disagreeing
            # opinion about the same request.
            return True
        return self.tokens.check_api_token(request, scope=scope)

    # -- Main dispatch ------------------------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)
        started = time.perf_counter()
        response: Any | None = None
        # One evaluation per request, before any route reads it. A `jwt` assertion needs a key
        # and therefore an await, so this is the only place that can decide it, and every route
        # below reads the two attributes rather than repeating the check.
        identity = ""
        authenticator = self.trusted_proxy_authenticator()
        if authenticator is not None:
            identity = await authenticator.authenticate(connection, request.headers)
        # The flag is derived from the identity rather than decided beside it, so the two can
        # never disagree. A request this gateway admitted with no name would reach every route
        # below as the shared ``webui`` actor, and a forged assertion would then buy whatever
        # the shared token holds (#63).
        setattr(request, TRUSTED_PROXY_AUTHENTICATED_ATTR, bool(identity))
        setattr(request, TRUSTED_PROXY_IDENTITY_ATTR, identity)

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

        # Workspace explorer routes
        response = self._dispatch_workspace_routes(request, got)
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
        response = self._dispatch_approval_routes(request, got)
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
        # ``dispatch`` decided this already. Reading the flag rather than repeating the check
        # keeps one evaluation per request: a second one would verify the same signature twice
        # and log a refusal twice, and the two could disagree if either call site drifted.
        is_proxy_authenticated = bool(getattr(request, TRUSTED_PROXY_AUTHENTICATED_ATTR, False))
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
        m = re.match(r"^/api/webui/automations/([^/]+)/state$", got)
        if m:
            return self._handle_automation_state_get(request, m.group(1))
        m = re.match(r"^/api/webui/automations/([^/]+)/state/reset$", got)
        if m:
            return self._handle_automation_state_reset(request, m.group(1))
        m = re.match(r"^/api/triggers/([^/]+)/fire$", got)
        if m:
            return self._handle_trigger_fire(request, m.group(1))
        m = re.match(r"^/api/webui/automations/([^/]+)/commission$", got)
        if m:
            return await self._handle_automation_commission(request, m.group(1))
        m = re.match(r"^/api/webui/automations/([^/]+)/grant$", got)
        if m:
            return self._handle_automation_grant(request, m.group(1))
        m = re.match(r"^/api/webui/automations/([^/]+)/key$", got)
        if m:
            return self._handle_trigger_key_issue(request, m.group(1))
        m = re.match(r"^/api/webui/automations/([^/]+)/key/revoke$", got)
        if m:
            return self._handle_trigger_key_revoke(request, m.group(1))
        if got == "/api/webui/automations/failed":
            return self._handle_failed_deliveries(request)
        m = re.match(r"^/api/webui/automations/failed/([^/]+)/replay$", got)
        if m:
            return self._handle_replay_failed_delivery(request, m.group(1))
        return None

    def _handle_failed_deliveries(self, request: WsRequest) -> Response:
        """Dead-lettered deliveries, newest first. Content is included: it is what the operator
        needs in order to decide whether replaying is safe."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        deliveries = self.local_trigger_store.list_failed_deliveries()
        return _http_json_response(
            {
                "deliveries": [
                    {
                        "id": delivery.id,
                        "trigger_id": delivery.trigger_id,
                        "content": delivery.content,
                        "attempts": delivery.attempts,
                        "last_error": delivery.last_error,
                        "created_at_ms": delivery.created_at_ms,
                        "replay_of": delivery.replay_of or None,
                    }
                    for delivery in deliveries
                ]
            }
        )

    def _handle_replay_failed_delivery(self, request: WsRequest, delivery_id: str) -> Response:
        """Requeue one dead-lettered delivery.

        A replay is a new execution with a known provenance, not a recording being played back, so
        it passes the same gates as the original. Nothing about having run once before is a reason
        to skip an approval.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        try:
            replay = self.local_trigger_store.replay_failed_delivery(delivery_id)
        except TriggerDisabledError:
            return _http_error(409, "trigger is disabled")
        except TriggerNotFoundError:
            return _http_error(409, "the trigger this delivery belongs to no longer exists")
        if replay is None:
            return _http_error(404, "delivery not found")
        return _http_json_response(
            {"queued": True, "delivery_id": replay.id, "replay_of": replay.replay_of}
        )

    async def _handle_automation_commission(
        self, request: WsRequest, automation_id: str
    ) -> Response:
        """Rehearse one automation now. It previews every gated action and takes none (#183)."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.commissioning is None or not self.commissioning.can_commission:
            return _http_error(503, "commissioning is unavailable in this deployment")
        try:
            result = await self.commissioning.commission(automation_id)
        except PromotionRefusedError as exc:
            return _http_error(409, str(exc))
        return _http_json_response(result)

    def _handle_automation_grant(self, request: WsRequest, automation_id: str) -> Response:
        """Write the grant a rehearsal proposed, and record who asked for it (#186, #188).

        The request body names nothing. The grant comes off the automation's own commissioning
        finding, because a grant supplied by the caller would let whatever composed the request
        choose its own authority.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.commissioning is None:
            return _http_error(503, "commissioning is unavailable in this deployment")
        try:
            result = self.commissioning.promote(
                automation_id,
                actor=operator_actor(request),
                origin_path="webui",
            )
        except PromotionRefusedError as exc:
            return _http_error(409, str(exc))
        except OSError as exc:
            return _http_error(500, f"the grant could not be written: {exc}")
        return _http_json_response(result)

    def _handle_trigger_key_issue(self, request: WsRequest, trigger_id: str) -> Response:
        """Mint a key and return it once. Operator-authenticated, unlike the fire route."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        key = self.local_trigger_store.issue_key(trigger_id)
        if key is None:
            return _http_error(404, "automation not found")
        # The only response that ever carries the plaintext. Nothing stores it, so an operator who
        # loses it issues another -- which is also how rotation works.
        return _http_json_response({"id": trigger_id, "key": key})

    def _handle_trigger_key_revoke(self, request: WsRequest, trigger_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        if self.local_trigger_store.get(trigger_id) is None:
            return _http_error(404, "automation not found")
        return _http_json_response(
            {"id": trigger_id, "revoked": self.local_trigger_store.revoke_key(trigger_id)}
        )

    def _handle_trigger_fire(self, request: WsRequest, trigger_id: str) -> Response:
        """Fire one trigger, authenticated only by that trigger's own key.

        This route does not run a turn. Its whole job is ``store.enqueue``, so HTTP ingress
        inherits the retry, the dead-letter, the crash recovery and the run records that the CLI
        path already has -- one delivery path, not two.

        A wrong id and a wrong key return the same 401, so the endpoint is not a trigger
        directory. Nothing here reads a gateway token: a key authorises exactly one trigger, and
        the gateway's own tokens live in memory and would not survive a restart anyway.
        """
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        presented = bearer_token(request.headers) or ""
        if not self.local_trigger_store.verify_key(trigger_id, presented):
            return _http_error(401, "Unauthorized")

        raw = _case_insensitive_header(request.headers, _TRIGGER_MESSAGE_HEADER) or ""
        # The raw form is what the transport measured, and percent-encoding inflates it, so both
        # halves are checked: the wire length so the answer is a 413 rather than a closed socket,
        # and the decoded length because that is what reaches the prompt.
        if len(raw.encode("utf-8")) > _TRIGGER_MESSAGE_MAX_BYTES:
            return _http_error(413, "trigger message is too large")
        message = unquote(raw).strip()
        if not message:
            return _http_error(400, f"{_TRIGGER_MESSAGE_HEADER} is required")
        if len(message.encode("utf-8")) > _TRIGGER_MESSAGE_MAX_BYTES:
            # Refused rather than truncated: a caller told its payload was rejected can adapt, and
            # one whose alert was silently cut in half cannot.
            return _http_error(413, "trigger message is too large")

        idempotency = (
            _case_insensitive_header(request.headers, _TRIGGER_IDEMPOTENCY_HEADER) or ""
        ).strip()
        now = time.monotonic()
        self._purge_trigger_fire_state(now)
        if idempotency:
            seen = self._trigger_idempotency.get((trigger_id, idempotency))
            if seen is not None:
                # Deliberately 200, not 409. The caller asked for exactly-once and got it; a
                # retry that reports failure would make it retry again.
                return _http_json_response({"queued": False, "duplicate": True})
        last = self._trigger_last_fire.get(trigger_id)
        if last is not None and now - last < _TRIGGER_MIN_INTERVAL_S:
            return _http_error(429, "trigger fired too recently")

        try:
            delivery = self.local_trigger_store.enqueue(trigger_id, message)
        except TriggerDisabledError:
            return _http_error(409, "trigger is disabled")
        except TriggerNotFoundError:
            # Only reachable if the trigger vanished between verify and enqueue. Same shape as a
            # bad key, so the race does not become a way to probe for ids.
            return _http_error(401, "Unauthorized")
        except ValueError as exc:
            return _http_error(400, str(exc))

        self._trigger_last_fire[trigger_id] = now
        if idempotency:
            self._trigger_idempotency[(trigger_id, idempotency)] = now
        return _http_json_response({"queued": True, "delivery_id": delivery.id})

    def _purge_trigger_fire_state(self, now: float) -> None:
        """Drop expired idempotency keys and stale rate-limit stamps.

        Both maps are per-process and bounded by this sweep. Nothing here needs to survive a
        restart: a restart is already a window in which a duplicate could land, and the delivery
        queue is what makes that survivable.
        """
        expired = [
            key
            for key, seen in self._trigger_idempotency.items()
            if now - seen > _TRIGGER_IDEMPOTENCY_TTL_S
        ]
        for key in expired:
            self._trigger_idempotency.pop(key, None)
        stale = [
            trigger_id
            for trigger_id, seen in self._trigger_last_fire.items()
            if now - seen > _TRIGGER_IDEMPOTENCY_TTL_S
        ]
        for trigger_id in stale:
            self._trigger_last_fire.pop(trigger_id, None)

    def _handle_automation_state_get(self, request: WsRequest, automation_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.automation_state_store is None:
            return _http_error(503, "automation state unavailable")
        if not self._automation_exists(automation_id):
            # A state document outlives the automation that wrote it if a delete ever misses, so
            # existence is checked against the automation rather than against the file.
            return _http_error(404, "automation not found")
        try:
            values = self.automation_state_store.snapshot(automation_id)
        except AutomationStateError as exc:
            return _http_error(400, str(exc))
        return _http_json_response({"id": automation_id, "values": values})

    def _handle_automation_state_reset(self, request: WsRequest, automation_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.automation_state_store is None:
            return _http_error(503, "automation state unavailable")
        if not self._automation_exists(automation_id):
            return _http_error(404, "automation not found")
        try:
            cleared = self.automation_state_store.clear(automation_id)
        except AutomationStateError as exc:
            return _http_error(400, str(exc))
        return _http_json_response({"id": automation_id, "cleared": bool(cleared)})

    def _automation_exists(self, automation_id: str) -> bool:
        if self.cron_service is not None and self.cron_service.get_job(automation_id) is not None:
            return True
        if self.local_trigger_store is None:
            return False
        return self.local_trigger_store.get(automation_id) is not None

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

    # -- Workspace explorer routes ---------------------------------------------

    def _dispatch_workspace_routes(self, request: WsRequest, got: str) -> Response | None:
        # Scoped as a group, the way the secret routes are: a workspace route added
        # later cannot forget its own auth check.
        if got.startswith("/api/webui/workspace/") and not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        bare = got.split("?", 1)[0]
        if bare == "/api/webui/workspace/projects":
            return self._handle_workspace_projects(request)
        if bare == "/api/webui/workspace/projects/create":
            return self._handle_workspace_project_create(request)
        if bare == "/api/webui/workspace/list":
            return self._handle_workspace_list(request)
        if bare == "/api/webui/workspace/preview":
            return self._handle_workspace_preview(request)
        if bare == "/api/webui/workspace/download":
            return self._handle_workspace_download(request)
        if bare == "/api/webui/workspace/mkdir":
            return self._handle_workspace_mkdir(request)
        if bare == "/api/webui/workspace/rename":
            return self._handle_workspace_rename(request)
        if bare == "/api/webui/workspace/move":
            return self._handle_workspace_move(request)
        if bare == "/api/webui/workspace/delete":
            return self._handle_workspace_delete(request)
        return None

    def workspace_scope_for(self, raw: str | None) -> Any:
        """The scope for a workspace a client named, or the configured one.

        The single gate for that choice (`workspace_roots.resolve_client_workspace`):
        the workspaces root, something under it, or the configured workspace. The
        access mode is the operator's, never the client's -- picking which workspace
        to look at is not a way to widen what may be done inside it.
        """
        default = self.workspaces.default_scope()
        root = Path(load_config().tools.workspaces_root).expanduser()
        resolved = resolve_client_workspace(
            raw, root=root, default_workspace=Path(default.project_path)
        )
        if resolved == Path(default.project_path):
            return default
        return build_workspace_scope(
            resolved,
            default.access_mode,
            source_channel=default.source_channel,
        )

    def _handle_workspace_projects(self, request: WsRequest) -> Response:
        """The workspaces a client may choose between."""
        default = self.workspaces.default_scope()
        root = Path(load_config().tools.workspaces_root).expanduser()
        return _http_json_response(
            workspaces_payload_for_root(root, Path(default.project_path))
        )

    def _handle_workspace_project_create(self, request: WsRequest) -> Response:
        values = _workspace_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid workspace payload")
        root = Path(load_config().tools.workspaces_root).expanduser()
        try:
            payload = create_workspace(root, str(values.get("name") or ""))
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_workspace_list(self, request: WsRequest) -> Response:
        """One directory of the active workspace, for the Workspaces explorer."""
        query = _parse_query(request.path)
        path = _query_first(query, "path")
        try:
            payload = directory_listing_payload(
                path,
                scope=self.workspace_scope_for(_query_first(query, "workspace")),
                include_hidden=_query_first(query, "hidden") == "1",
            )
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_workspace_preview(self, request: WsRequest) -> Response:
        """One file's text, for the explorer's preview pane.

        Reuses ``file_preview_payload`` rather than reading the file again: the
        binary refusal, the byte cap and the language mapping are decisions that
        belong to one module, and the session-keyed preview route already answers
        them. Only the scope differs -- the explorer is not attached to a chat.
        """
        query = _parse_query(request.path)
        path = _query_first(query, "path")
        try:
            scope = self.workspace_scope_for(_query_first(query, "workspace"))
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        try:
            # Resolved by the browser module first, so a path the explorer may not
            # enumerate cannot be read through the preview route either. Its
            # containment is the narrower of the two (no media root).
            resolved = resolve_within_workspace(path, scope=scope, must_exist=True)
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        try:
            payload = file_preview_payload(str(resolved), scope=scope)
        except WebUIFilePreviewError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_workspace_download(self, request: WsRequest) -> Response:
        """One file's bytes. Fetched as a blob by the client, because this route needs
        the bearer token and a plain link cannot carry one."""
        query = _parse_query(request.path)
        try:
            resolved, body = read_file_for_download(
                _query_first(query, "path"),
                scope=self.workspace_scope_for(_query_first(query, "workspace")),
            )
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        ctype, _ = mimetypes.guess_type(resolved.name)
        return _http_response(
            body,
            status=200,
            # Deliberately not the guessed text/html or image/svg+xml: this body is
            # workspace content served from the gateway's own origin, so letting a
            # browser render it would put operator-authored (or agent-authored) markup
            # inside the WebUI's origin. A download is what was asked for anyway.
            content_type="application/octet-stream",
            extra_headers=[
                ("Content-Disposition", _content_disposition(resolved.name)),
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"),
                # Kept for a client that wants to label the file, without being the
                # type the browser is told to interpret it as.
                ("X-Nanoinfra-Content-Type", ctype or "application/octet-stream"),
            ],
        )

    def _handle_workspace_mkdir(self, request: WsRequest) -> Response:
        values = _workspace_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid workspace payload")
        try:
            payload = create_directory(
                _optional_str(values.get("parent")),
                str(values.get("name") or ""),
                scope=self.workspace_scope_for(_optional_str(values.get("workspace"))),
                include_hidden=values.get("includeHidden") is True,
            )
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_workspace_rename(self, request: WsRequest) -> Response:
        values = _workspace_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid workspace payload")
        try:
            payload = rename_entry(
                _optional_str(values.get("parent")),
                str(values.get("name") or ""),
                str(values.get("newName") or ""),
                scope=self.workspace_scope_for(_optional_str(values.get("workspace"))),
                include_hidden=values.get("includeHidden") is True,
            )
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_workspace_move(self, request: WsRequest) -> Response:
        values = _workspace_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid workspace payload")
        try:
            payload = move_entry(
                _optional_str(values.get("parent")),
                str(values.get("name") or ""),
                _optional_str(values.get("destination")),
                scope=self.workspace_scope_for(_optional_str(values.get("workspace"))),
                include_hidden=values.get("includeHidden") is True,
            )
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_workspace_delete(self, request: WsRequest) -> Response:
        values = _workspace_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid workspace payload")
        try:
            payload = delete_entry(
                _optional_str(values.get("parent")),
                str(values.get("name") or ""),
                # Absent reads as false: a client that has not said it means to remove a
                # tree does not get to remove one because the field was missing.
                recursive=values.get("recursive") is True,
                scope=self.workspace_scope_for(_optional_str(values.get("workspace"))),
                include_hidden=values.get("includeHidden") is True,
            )
        except WebUIFileBrowserError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    # -- Secret routes ---------------------------------------------------------

    def _dispatch_secret_routes(self, request: WsRequest, got: str) -> Response | None:
        # Scoped as a group rather than per handler, so a secret route added later cannot forget.
        if got.startswith("/api/webui/secrets") and not self.check_api_token(
            request, scope="secrets"
        ):
            return _http_error(401, "Unauthorized")
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
        try:
            return _http_json_response(webui_secrets_payload(self.secrets))
        except SecretsStoreUnreadableError as exc:
            # 409 and not 500: the deployment is coherent, this process simply is not the one
            # that may read the store. An empty list here was the old answer, and it read as
            # "no secrets yet" about a store holding a credential.
            return _http_error(409, str(exc))

    def _handle_webui_secret_detail(self, request: WsRequest, secret_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            payload = webui_secret_detail_payload(self.secrets, secret_id)
        except SecretsStoreUnreadableError as exc:
            return _http_error(409, str(exc))
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
        except (
            SecretsNotConfiguredError,
            PostgresSecretsNotConfiguredError,
            SecretsStoreUnreadableError,
        ) as exc:
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
        except (
            SecretsNotConfiguredError,
            PostgresSecretsNotConfiguredError,
            SecretsStoreUnreadableError,
        ) as exc:
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
            cleared = self.latch.clear(values, actor=operator_actor(request))
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

    def _dispatch_approval_routes(self, request: WsRequest, got: str) -> Response | None:
        """The read and the answer for one suspended action (#27).

        Both routes need the API token, because the answer authorizes a remote command. A
        missing surface answers 503 and never an empty queue: a WebUI that cannot reach the
        executor must not read as "no action waits".

        Each route refuses a method that is not its own. The ``websockets`` request carries no
        method, so an absent value reads as the route's own method rather than as a violation.
        """
        if got not in (APPROVALS_READ_PATH, APPROVALS_ANSWER_PATH):
            return None
        # Scoped: answering authorises a remote command, so a narrow token must not reach it even
        # when that token can read everything else. This is where the TUI's exclusion lives.
        if not self.check_api_token(request, scope="approve"):
            return _http_error(401, "Unauthorized")
        # The websockets Request carries no method, so a method guard is defence for a transport
        # that one day does. It must never be the only control that keeps a write out of a read
        # route. Over this transport every request arrives as a GET, and a POST reaches no route at
        # all: the server closes the connection with no response. The answer route required a POST
        # and was therefore unreachable, so the inbox could answer nothing.
        method = str(getattr(request, "method", "") or "").upper()
        if got == APPROVALS_READ_PATH:
            if method and method not in ("GET", "HEAD"):
                return _http_error(405, "this route only reads the pending approvals")
            if self.approvals is None:
                return _http_error(503, "the executor is not available on this gateway")
            return _http_json_response(self.approvals.pending())
        if self.approvals is None:
            return _http_error(503, "the executor is not available on this gateway")
        values = approval_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid approval payload")
        try:
            answered = self.approvals.answer(values, actor=operator_actor(request))
        except ApprovalAnswerError as exc:
            return _http_error(400, str(exc))
        return _http_json_response(answered)

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


def _diagram_values_raw(request: WsRequest) -> str:
    """Reassemble the diagram body from its chunk headers (#92).

    An unchunked ``X-Nanoinfra-Diagram-Values`` still reads, because a client from before this change
    sends one and a small diagram is the case where it worked.
    """
    count_raw = _case_insensitive_header(request.headers, _DIAGRAM_CHUNK_COUNT_HEADER)
    if not count_raw:
        return _case_insensitive_header(request.headers, _DIAGRAM_VALUES_HEADER) or ""
    try:
        count = int(count_raw)
    except ValueError:
        return ""
    if count < 1 or count > 512:
        return ""
    parts: list[str] = []
    for index in range(count):
        part = _case_insensitive_header(request.headers, f"{_DIAGRAM_VALUES_HEADER}-{index}")
        if not part:
            # A missing chunk is a truncated body, and half a diagram must not be saved as a whole
            # one -- the first chunk alone can be valid JSON, so a parse failure is not the guard.
            # The splitter never emits an empty chunk when there is more than one, so empty here
            # means absent. The caller treats "" as an invalid payload.
            return ""
        parts.append(part)
    return "".join(parts)


def _diagram_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    """Read the full diagram body from ``X-Nanoinfra-Diagram-Values``.

    Unlike ``_automation_values_from_request``, a missing header is *not*
    "no changes" here — create/update always need a complete diagram body,
    so a missing or malformed header is treated the same: an invalid payload.
    """
    raw = _diagram_values_raw(request)
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


def _content_disposition(name: str) -> str:
    """``attachment`` with an RFC 5987 filename, so a non-ASCII name survives.

    The plain ``filename`` fallback is stripped to ASCII rather than dropped: a
    client that ignores ``filename*`` should still get a usable name, and a raw
    quote or newline in a header value is how a header becomes two.
    """
    ascii_name = "".join(c for c in name if 32 <= ord(c) < 127 and c not in '"\\') or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _optional_str(raw: Any) -> str | None:
    """A present, non-empty string, or ``None`` -- which the browser reads as the root."""
    return raw.strip() or None if isinstance(raw, str) else None


def _workspace_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    """Read one workspace mutation's arguments from ``X-Nanoinfra-Workspace-Values``."""
    raw = _case_insensitive_header(request.headers, _WORKSPACE_VALUES_HEADER)
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
    if "delivery" in values:
        raw_delivery = values.get("delivery")
        if not isinstance(raw_delivery, str) or raw_delivery.strip().lower() not in DELIVERY_POLICIES:
            return f"delivery must be one of: {', '.join(DELIVERY_POLICIES)}"
        update["delivery"] = raw_delivery.strip().lower()
    if "skills" in values:
        raw_skills = values.get("skills")
        if not isinstance(raw_skills, list):
            return "skills must be an array"
        names: list[str] = []
        for entry in cast("list[Any]", raw_skills):
            if not isinstance(entry, str):
                return "skills must be an array of strings"
            name = entry.strip()
            if name and name not in names:
                names.append(name)
        update["skills"] = names
    if "references" in values:
        raw_references = values.get("references")
        if not isinstance(raw_references, list):
            return "references must be an array"
        # Reuse the mention normaliser rather than re-validating here, so the editor and the chat
        # composer cannot disagree about what a reference is. Existence is checked at run time,
        # not here: a reference can outlive the resource, and the run is where that must stop.
        parsed_references = normalize_resource_mentions(cast("list[object]", raw_references))
        supplied = [item for item in cast("list[object]", raw_references) if item]
        if len(parsed_references) != len(supplied):
            return "references must be objects with a known kind and an id"
        update["references"] = [
            {"kind": kind, "id": ident} for kind, ident in parsed_references
        ]
    if "schedule" in values:
        raw_schedule = values.get("schedule")
        if not isinstance(raw_schedule, dict):
            return "schedule must be an object"
        parsed_schedule = _parse_automation_schedule(cast(dict[str, Any], raw_schedule))
        if isinstance(parsed_schedule, str):
            return parsed_schedule
        # An unchanged schedule is skipped rather than revalidated, because a job whose cron
        # expression was valid when it was created must stay editable. This used to `return
        # update`, which silently dropped every field parsed after this block.
        if current_job is None or not _schedule_matches_job(parsed_schedule, current_job):
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
    if "delivery" in values:
        raw_delivery = values.get("delivery")
        if not isinstance(raw_delivery, str) or raw_delivery.strip().lower() not in DELIVERY_POLICIES:
            return f"delivery must be one of: {', '.join(DELIVERY_POLICIES)}"
        update["delivery"] = raw_delivery.strip().lower()
    if "skills" in values:
        raw_skills = values.get("skills")
        if not isinstance(raw_skills, list):
            return "skills must be an array"
        names: list[str] = []
        for entry in cast("list[Any]", raw_skills):
            if not isinstance(entry, str):
                return "skills must be an array of strings"
            name = entry.strip()
            if name and name not in names:
                names.append(name)
        update["skills"] = names
    if "references" in values:
        raw_references = values.get("references")
        if not isinstance(raw_references, list):
            return "references must be an array"
        # Reuse the mention normaliser rather than re-validating here, so the editor and the chat
        # composer cannot disagree about what a reference is. Existence is checked at run time,
        # not here: a reference can outlive the resource, and the run is where that must stop.
        parsed_references = normalize_resource_mentions(cast("list[object]", raw_references))
        supplied = [item for item in cast("list[object]", raw_references) if item]
        if len(parsed_references) != len(supplied):
            return "references must be objects with a known kind and an id"
        update["references"] = [
            {"kind": kind, "id": ident} for kind, ident in parsed_references
        ]
    forbidden = [key for key in ("message", "schedule") if key in values]
    if forbidden:
        return "local trigger updates only support name, delivery, skills and references"
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
