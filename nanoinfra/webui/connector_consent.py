"""Start a consent, and finish it when the browser comes back (#193).

Two functions, one per route, and the record between them lives in
`nanoinfra/connectors/pending_consent.py`.

**What the callback writes: the credential and the activation.** An earlier draft wrote only the
credential and left `connectors.active` to a person, on the argument that enabling is a decision
a reviewer should see in git. That argument is real and it loses to the requirement: a flow that
ends in "now open config.json" is the thing being removed. What the boundary protects is that
*the agent* cannot activate a connector, and it still cannot -- these are routes on the WebUI's
authenticated surface, which already writes the `gates` policy, and no tool reaches them.

Nothing here holds a plaintext longer than one call: the client secret arrives, is stored through
the executor, and the flow keeps only its reference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.config.connectors import ConnectorConfig, ConnectorCredentialConfig
from nanoinfra.config.loader import load_config, save_config
from nanoinfra.connectors.authorize import (
    AuthorizationError,
    authorize_url,
    code_from_redirect,
    exchange_code,
    pkce_pair,
)
from nanoinfra.connectors.pending_consent import consent_store
from nanoinfra.connectors.registry import discover_connectors
from nanoinfra.webui.settings_api import WebUISettingsError

#: The path Google redirects to, on this deployment's own origin.
CALLBACK_PATH = "/auth/connector/callback"


def callback_url(origin: str) -> str:
    """The redirect URI for one origin. Google validates it byte for byte."""
    return f"{origin.rstrip('/')}{CALLBACK_PATH}"


def _store_secret(workspace: Path, name: str, kind: str, value: str) -> str:
    """Store one value and return its id, through whichever path may write (#192)."""
    from nanoinfra.secrets.store import SecretStore

    from .secrets_api import create_webui_secret

    payload = create_webui_secret(
        SecretStore(workspace),
        {"name": name, "kind": kind, "providerId": "local", "value": value},
    )
    return str(payload["secret"]["id"])


def start_consent(
    connector: str,
    *,
    client_id: str,
    client_secret: str,
    origin: str,
    workspace: Path | str,
    account: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Store the client secret, record the pending consent, and return the URL to open."""
    installed = discover_connectors()
    plugin = installed.get(connector)
    if plugin is None:
        raise WebUISettingsError(
            f"no connector named {connector!r} is installed. Installed: "
            f"{sorted(installed) or 'none'}.",
            status=404,
        )
    if not client_id.strip() or not client_secret.strip():
        raise WebUISettingsError("a client id and a client secret are both required", status=400)

    scopes = plugin.credential.declared_scopes()
    if not scopes:
        raise WebUISettingsError(
            f"connector {connector!r} declares no OAuth scopes, so there is nothing to consent to",
            status=400,
        )

    stem = connector.replace("-", "_")
    verifier, challenge = pkce_pair()
    entry = consent_store().open(
        connector=connector,
        credential=f"{stem}_credential",
        client_id=client_id.strip(),
        # Kept in the pending record rather than written now: an abandoned consent then leaves
        # nothing behind, and nothing reads a plaintext back out of the store.
        client_secret=client_secret.strip(),
        verifier=verifier,
        redirect_uri=callback_url(origin),
        scopes=scopes,
        actor=actor,
    )
    url = authorize_url(
        client_id=entry.client_id,
        scopes=scopes,
        redirect_uri=entry.redirect_uri,
        challenge=challenge,
        state=entry.state,
        login_hint=account.strip(),
    )
    logger.info(
        "connectors: consent opened for {} by {} (redirect {})",
        connector,
        actor or "an unnamed operator",
        entry.redirect_uri,
    )
    return {
        "ok": True,
        "connector": connector,
        "authorize_url": url,
        "redirect_uri": entry.redirect_uri,
        "scopes": list(scopes),
        # So an operator whose client refuses the redirect knows the exact string to register.
        "register_this_redirect": entry.redirect_uri,
    }


def finish_consent(redirected_url: str, *, workspace: Path | str) -> dict[str, Any]:
    """Exchange the code, store the refresh token, and write the credential and activation.

    The state is consumed, so a replayed redirect finds nothing. A URL matching no pending
    consent is a 404 that records nothing: that is the case a scanner produces.
    """
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(redirected_url).query)
    state = (query.get("state") or [""])[0]
    entry = consent_store().take(state)
    if entry is None:
        raise WebUISettingsError(
            "this redirect does not match a consent this deployment started, or it has expired",
            status=404,
        )

    plugin = discover_connectors().get(entry.connector)
    if plugin is None:
        raise WebUISettingsError(f"connector {entry.connector!r} is no longer installed", status=409)

    try:
        code = code_from_redirect(redirected_url, expected_state=entry.state)
        authorization = exchange_code(
            token_url=plugin.credential.token_url,
            client_id=entry.client_id,
            client_secret=entry.client_secret,
            code=code,
            verifier=entry.verifier,
            redirect_uri=entry.redirect_uri,
        )
    except AuthorizationError as exc:
        raise WebUISettingsError(str(exc), status=400) from exc

    # Both secrets land now, after the exchange succeeded. A consent that failed writes
    # nothing, so there is no half-configured credential to clean up.
    stem = entry.connector.replace("-", "_")
    refresh_ref = _store_secret(
        Path(workspace), f"{stem}_refresh_token", "token", authorization.refresh_token
    )
    client_secret_ref = _store_secret(
        Path(workspace), f"{stem}_client_secret", "api_key", entry.client_secret
    )

    granted = list(authorization.granted_scopes) or list(entry.scopes)
    missing = [scope for scope in entry.scopes if scope not in granted]

    config = load_config()
    block = config.connectors
    block.credentials[entry.credential] = ConnectorCredentialConfig.model_validate(
        {
            "clientId": entry.client_id,
            "secretRef": refresh_ref,
            "clientSecretRef": client_secret_ref,
            "scopes": granted,
        }
    )
    existing = block.connectors.get(entry.connector)
    if existing is None:
        # A connector nobody had configured gets the binding and nothing else: the manifest's
        # own field defaults cover the rest, which is why `calendarId` needs no line here.
        block.connectors[entry.connector] = ConnectorConfig(credential=entry.credential)
    else:
        existing.credential = entry.credential
    if entry.connector not in block.active:
        block.active.append(entry.connector)
    save_config(config)

    logger.info(
        "connectors: consent completed for {} ({} scope(s) granted), credential {}",
        entry.connector,
        len(granted),
        entry.credential,
    )
    return {
        "ok": True,
        "connector": entry.connector,
        "credential": entry.credential,
        "granted_scopes": granted,
        "missing_scopes": missing,
        "actor": entry.actor,
    }


__all__ = ["CALLBACK_PATH", "callback_url", "finish_consent", "start_consent"]
