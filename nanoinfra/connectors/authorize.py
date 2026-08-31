"""The consent step: get a refresh token, once, from a person at a browser.

A connector cannot mint an access token until somebody has consented, and consent needs a
browser. So this is an operator action at a terminal, and it is deliberately not reachable
from a chat turn or from a model: the whole design is that a connector acts as the person who
authorised it, and an authorisation the agent could start would be an authorisation nobody
performed.

The flow is the loopback redirect from RFC 8252 with PKCE (RFC 7636): a local HTTP server on
127.0.0.1 receives the code, the code is exchanged for a refresh token, and the refresh token
is written to the secret store. Nothing is printed but the secret's id.

Two things are worth stating because they are easy to get wrong with Google:

- **``access_type=offline`` and ``prompt=consent``.** Without the first there is no refresh
  token at all, and without the second Google returns one only on the *first* ever consent for
  that client -- so re-authorising a connector would appear to work and produce a credential
  that dies in an hour.
- **The client secret is sent in the exchange.** Google requires it even for a desktop client,
  where it is not really a secret. It is read here and written to the store, never printed.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx

from nanoinfra.connectors.contracts import ConnectorPlugin

# Where Google sends the person to consent. A connector could carry its own, and none does yet:
# every Workspace API is one authorisation server.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# The loopback interface, and a port the operator can pin. 127.0.0.1 rather than localhost, so
# the redirect cannot resolve to something else on a machine with a creative hosts file.
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_LOOPBACK_PORT = 8765 + 1

# How long the local server waits for the person to finish. Long enough to pick an account and
# read a consent screen, short enough that a forgotten terminal does not hold a port all day.
CONSENT_TIMEOUT_S = 300.0

# Said before the URL, and again if the wait times out. A person who meets
# `redirect_uri_mismatch` is looking at a browser error this process never receives, so the
# only useful moment to name the two client types is before they click.
REDIRECT_URI_NOTE = (
    "A Desktop app client needs nothing registered for it. A Web application client must list "
    "that exact string, trailing slash included, under its authorised redirect URIs."
)

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>nanoinfra</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Authorised</h1><p>You can close this tab and go back to the terminal.</p>"
)
_FAIL_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>nanoinfra</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Not authorised</h1><p>The terminal has the reason.</p>"
)


class AuthorizationError(Exception):
    """The consent did not complete, with what the operator has to do about it."""


@dataclass(frozen=True, slots=True)
class Authorization:
    """What one completed consent produced.

    ``refresh_token`` is the only durable part, and the caller writes it straight to the secret
    store. ``granted_scopes`` is what the person actually consented to, which is not always what
    was asked for: Google drops a scope the project has not enabled, and the connector then
    refuses at activation with both sets named rather than failing on the first call.
    """

    refresh_token: str
    granted_scopes: tuple[str, ...]
    account: str = ""


def pkce_pair() -> tuple[str, str]:
    """A verifier and its S256 challenge (RFC 7636).

    Public because two flows need it: the CLI's loopback consent and the WebUI's redirect one.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    """Receives exactly one redirect and hands the code to the waiting thread."""

    code: str | None = None
    error: str | None = None
    state: str = ""
    finished: threading.Event

    def do_GET(self) -> None:  # noqa: N802 -- the stdlib names it
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        state = (query.get("state") or [""])[0]
        if state != type(self).state:
            # A redirect that does not carry our state is not our redirect. Answering it with a
            # page would tell whoever sent it that something is listening here.
            self.send_error(404)
            return
        code = (query.get("code") or [""])[0]
        error = (query.get("error") or [""])[0]
        type(self).code = code or None
        type(self).error = error or None
        body = _DONE_PAGE if code else _FAIL_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).finished.set()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib signature
        """Silence the stdlib access log: it would print the code in the URL."""


def code_from_redirect(text: str, *, expected_state: str) -> str:
    """Read the authorization code out of the URL Google redirected to.

    The manual path exists because a redirect only has to *happen*, not to be *received*: the
    browser lands on a page that fails to load, and its address bar still holds
    ``?code=...&state=...``. So a shell with no browser, or one where the browser cannot reach
    the loopback server, needs no local listener at all.

    The state is checked here as well. In the loopback flow the local server checks it; on this
    path there is no server, and dropping the check would accept a code from a page the operator
    did not start.
    """
    candidate = text.strip()
    if not candidate:
        raise AuthorizationError("nothing was pasted, so nothing was stored")

    if "?" in candidate or candidate.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(candidate).query)
        error = (query.get("error") or [""])[0]
        if error:
            raise AuthorizationError(f"the consent screen returned {error!r}, so nothing was stored")
        state = (query.get("state") or [""])[0]
        if state != expected_state:
            raise AuthorizationError(
                "the pasted URL carries a different state than this flow issued, so it is not "
                "this flow's redirect. Start again and paste the URL from the same run."
            )
        code = (query.get("code") or [""])[0]
        if not code:
            raise AuthorizationError(
                "the pasted URL carries no code. Copy the whole address bar after Google "
                "redirects, including everything after the '?'."
            )
        return code

    # A bare code, for somebody who pulled it out of the URL themselves. There is no state to
    # compare in that case, and saying so is better than pretending it was checked.
    return candidate


def authorize_url(
    *,
    client_id: str,
    scopes: tuple[str, ...],
    redirect_uri: str,
    challenge: str,
    state: str,
    login_hint: str = "",
) -> str:
    """The URL the person opens. Built here so a test can read it without a browser."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # Without this there is no refresh token, and the connector would work for an hour.
        "access_type": "offline",
        # Without this Google returns a refresh token only on the first consent ever given to
        # this client, so re-authorising would silently produce a credential that expires.
        "prompt": "consent",
        # Google's own recommendation for a desktop flow: it keeps the granted scopes explicit.
        "include_granted_scopes": "false",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> Authorization:
    """Swap the one-time code for a refresh token.

    Synchronous on purpose: this runs from a terminal command, one exchange, and an async
    client here would buy nothing but a loop to start.
    """
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        response = client.post(token_url, data=form)
    if response.status_code >= 400:
        raise AuthorizationError(
            f"the token exchange failed with HTTP {response.status_code}: {response.text[:400]}"
        )
    payload = cast(dict[str, Any], response.json())
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthorizationError(
            "the exchange returned no refresh_token. Google returns one only with "
            "access_type=offline and prompt=consent, and only to a client type that may hold "
            "one -- check that the OAuth client is a Desktop app or a Web application."
        )
    granted = payload.get("scope")
    scopes = tuple(granted.split()) if isinstance(granted, str) and granted else ()
    return Authorization(refresh_token=refresh, granted_scopes=scopes)


def _manual_consent(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    scopes: tuple[str, ...],
    redirect_uri: str,
    verifier: str,
    challenge: str,
    state: str,
    login_hint: str,
    print_fn: Callable[[str], None],
    input_fn: Callable[[str], str],
) -> Authorization:
    """Consent with no local listener: the operator pastes the redirect back.

    The redirect URI still has to be the loopback one, because Google removed the out-of-band
    redirect in 2022. The browser therefore lands on a page that will not load, and that is
    expected: the code is in the address bar either way.
    """
    url = authorize_url(
        client_id=client_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
        challenge=challenge,
        state=state,
        login_hint=login_hint,
    )
    print_fn(f"Redirect this flow uses: {redirect_uri}")
    print_fn(REDIRECT_URI_NOTE)
    print_fn(f"\n1. Open this and consent as the account the connector should act as:\n\n{url}\n")
    print_fn(
        f"2. The browser will end up on {redirect_uri}... and fail to load. That is expected on "
        "this path -- nothing is listening here."
    )
    print_fn("3. Copy the whole address bar from that failed page and paste it below.\n")
    pasted = input_fn("Redirected URL: ")
    code = code_from_redirect(pasted, expected_state=state)
    return exchange_code(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
    )


def run_consent_flow(
    plugin: ConnectorPlugin,
    *,
    client_id: str,
    client_secret: str,
    scopes: tuple[str, ...] = (),
    port: int = DEFAULT_LOOPBACK_PORT,
    login_hint: str = "",
    open_browser: bool = True,
    manual: bool = False,
    print_fn: Callable[[str], None] = print,
    input_fn: Callable[[str], str] = input,
) -> Authorization:
    """Take one person through consent and return the refresh token.

    ``scopes`` defaults to every scope the connector declares, across all its classes, because
    a credential granted less than that is refused at activation -- and being told that here,
    at the browser, is better than being told at the next boot.

    ``manual`` starts no local server. The operator opens the URL, consents, and pastes the URL
    the browser was redirected to -- which is what a shell with no browser needs, and what a
    browser on another machine needs, because the redirect there cannot reach this loopback
    address at all.
    """
    wanted = scopes or plugin.credential.declared_scopes()
    if not wanted:
        raise AuthorizationError(f"connector {plugin.name!r} declares no OAuth scopes")
    token_url = plugin.credential.token_url
    if not token_url:
        raise AuthorizationError(f"connector {plugin.name!r} declares no token endpoint")

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://{LOOPBACK_HOST}:{port}/"

    if manual:
        return _manual_consent(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=wanted,
            redirect_uri=redirect_uri,
            verifier=verifier,
            challenge=challenge,
            state=state,
            login_hint=login_hint,
            print_fn=print_fn,
            input_fn=input_fn,
        )

    handler = type("_Handler", (_CodeHandler,), {"finished": threading.Event(), "state": state})
    try:
        server = http.server.HTTPServer((LOOPBACK_HOST, port), handler)
    except OSError as exc:
        raise AuthorizationError(
            f"could not listen on {redirect_uri} ({exc}). Pass --port with a free one, and add "
            "the same address as an authorised redirect URI on the OAuth client."
        ) from exc

    url = authorize_url(
        client_id=client_id,
        scopes=wanted,
        redirect_uri=redirect_uri,
        challenge=challenge,
        state=state,
        login_hint=login_hint,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The redirect URI is printed before the URL, because it is the one value a person has
        # to reconcile with the console. A web-application client validates it exactly, and the
        # browser then shows `redirect_uri_mismatch` -- an error this process never sees, since
        # Google refuses before it redirects. So the value goes on screen up front.
        print_fn(f"Redirect this flow uses: {redirect_uri}")
        print_fn(REDIRECT_URI_NOTE)
        print_fn(f"\nOpen this and consent as the account the connector should act as:\n\n{url}\n")
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        if not handler.finished.wait(CONSENT_TIMEOUT_S):
            raise AuthorizationError(
                f"nobody completed the consent within {int(CONSENT_TIMEOUT_S)}s, so nothing was "
                f"stored.\n\nIf the browser showed 'Error 400: redirect_uri_mismatch', the OAuth "
                f"client is a Web application and does not list {redirect_uri} -- add it there "
                "exactly, with the trailing slash, or create a client of type Desktop app, which "
                "needs no redirect registered at all."
            )
    finally:
        server.shutdown()
        server.server_close()

    if handler.error or not handler.code:
        raise AuthorizationError(
            f"the consent screen returned {handler.error or 'no code'}, so nothing was stored."
        )

    authorization = exchange_code(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        code=handler.code,
        verifier=verifier,
        redirect_uri=redirect_uri,
    )
    missing = [scope for scope in wanted if scope not in authorization.granted_scopes]
    if missing and authorization.granted_scopes:
        print_fn(
            "The consent granted fewer scopes than the connector asked for. Missing: "
            f"{sorted(missing)}. Operations that need them will be refused at activation."
        )
    return authorization


__all__ = [
    "CONSENT_TIMEOUT_S",
    "DEFAULT_LOOPBACK_PORT",
    "GOOGLE_AUTHORIZE_URL",
    "REDIRECT_URI_NOTE",
    "Authorization",
    "AuthorizationError",
    "authorize_url",
    "code_from_redirect",
    "pkce_pair",
    "exchange_code",
    "run_consent_flow",
]
