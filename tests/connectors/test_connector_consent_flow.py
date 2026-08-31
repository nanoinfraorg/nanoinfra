"""The consent that completes itself (#193).

The requirement, in the user's words: *"La accion de copiar y pegar enlace y el redirect luego de
que se acepta deberia funcionar y el proceso deberia ser seamless, es algo que no debes hacer a
mano."* So the test is about what nobody has to carry: the code comes back through the callback,
the secrets are stored, and config gains the credential **and** the activation.

Nothing here reaches Google. The token endpoint is the one thing stubbed, because it is the one
thing on the other side of a browser.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.config.schema import Config
from nanoinfra.connectors.pending_consent import PendingConsentStore, consent_store
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.webui.connector_consent import (
    CALLBACK_PATH,
    callback_url,
    finish_consent,
    start_consent,
)
from nanoinfra.webui.settings_api import WebUISettingsError

CALENDAR = "google-calendar"
ORIGIN = "https://demo.example.test"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


@pytest.fixture(autouse=True)
def _clean_store() -> Any:
    """The pending store is process-wide, so a leaked entry would cross tests."""
    store = consent_store()
    for entry in store.pending():
        store.take(entry.state)
    yield
    for entry in store.pending():
        store.take(entry.state)


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    """A config that loads from memory and records what a save would have written."""
    loaded = Config()
    loaded.connectors = ConnectorRuntimeConfig()
    loaded.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr("nanoinfra.webui.connector_consent.load_config", lambda: loaded)
    monkeypatch.setattr("nanoinfra.webui.connector_consent.save_config", lambda cfg: None)
    return loaded


def _token_endpoint(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], status: int = 200):
    seen: dict[str, Any] = {}

    def _post(self: object, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        seen["url"] = url
        seen["data"] = dict(data or {})
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(httpx.Client, "post", _post)
    return seen


def _redirect(state: str, code: str = "4/code-1") -> str:
    query = urllib.parse.urlencode({"state": state, "code": code})
    return f"{ORIGIN}{CALLBACK_PATH}?{query}"


# --- starting ---------------------------------------------------------------------------


def test_starting_returns_a_url_and_stores_nothing_yet(config: Config, tmp_path: Path) -> None:
    result = start_consent(
        CALENDAR,
        client_id="cid.apps.googleusercontent.test",
        client_secret="cs-1",
        origin=ORIGIN,
        workspace=tmp_path,
        account="ops@example.test",
        actor="webui:ops@example.test",
    )

    assert result["redirect_uri"] == f"{ORIGIN}{CALLBACK_PATH}"
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(result["authorize_url"]).query))
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == result["redirect_uri"]
    assert params["login_hint"] == "ops@example.test"
    # The scopes come from the manifest, not from the caller.
    assert "calendar.readonly" in params["scope"]
    assert "calendar.events" in params["scope"]

    # Nothing is stored yet: an abandoned consent leaves no half-configured credential, and the
    # secret never goes back through the store to be read again.
    assert SecretStore(tmp_path).list_secrets() == []
    assert "cs-1" not in json.dumps(result)


def test_the_state_is_recorded_and_carries_the_verifier(config: Config, tmp_path: Path) -> None:
    result = start_consent(
        CALENDAR,
        client_id="cid",
        client_secret="cs-1",
        origin=ORIGIN,
        workspace=tmp_path,
    )
    state = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(result["authorize_url"]).query)
    )["state"]

    entry = consent_store().peek(state)
    assert entry is not None
    assert entry.connector == CALENDAR
    assert entry.verifier
    # The verifier never leaves the process.
    assert entry.verifier not in json.dumps(result)


def test_a_connector_that_is_not_installed_is_a_404(config: Config, tmp_path: Path) -> None:
    with pytest.raises(WebUISettingsError) as raised:
        start_consent(
            "gmail", client_id="cid", client_secret="cs", origin=ORIGIN, workspace=tmp_path
        )
    assert raised.value.status == 404


def test_starting_without_both_halves_is_a_400(config: Config, tmp_path: Path) -> None:
    with pytest.raises(WebUISettingsError) as raised:
        start_consent(
            CALENDAR, client_id="cid", client_secret="  ", origin=ORIGIN, workspace=tmp_path
        )
    assert raised.value.status == 400


# --- finishing --------------------------------------------------------------------------


def _start(config: Config, tmp_path: Path) -> str:
    result = start_consent(
        CALENDAR,
        client_id="cid.apps.googleusercontent.test",
        client_secret="cs-1",
        origin=ORIGIN,
        workspace=tmp_path,
        actor="webui:ops@example.test",
    )
    return dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(result["authorize_url"]).query)
    )["state"]


def test_the_callback_writes_the_credential_and_the_activation(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: config.json was not opened by a human at any point."""
    state = _start(config, tmp_path)
    seen = _token_endpoint(
        monkeypatch,
        {
            "refresh_token": "rt-1",
            "scope": (
                "https://www.googleapis.com/auth/calendar.readonly "
                "https://www.googleapis.com/auth/calendar.events"
            ),
        },
    )

    result = finish_consent(_redirect(state), workspace=tmp_path)

    assert result["ok"] is True
    assert result["missing_scopes"] == []
    # The exchange used the verifier and the same redirect Google validated.
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code_verifier"]
    assert seen["data"]["redirect_uri"] == f"{ORIGIN}{CALLBACK_PATH}"

    block = config.connectors
    credential = block.credentials["google_calendar_credential"]
    assert credential.client_id == "cid.apps.googleusercontent.test"
    assert credential.secret_ref
    assert credential.client_secret_ref
    # Both halves: the binding and the activation. Anything less leaves a hand-edit.
    assert block.connectors[CALENDAR].credential == "google_calendar_credential"
    assert block.active == [CALENDAR]

    # Both secrets land after the exchange succeeded, and neither value is in config.
    names = {s.name for s in SecretStore(tmp_path).list_secrets()}
    assert names == {"google_calendar_refresh_token", "google_calendar_client_secret"}
    serialised = json.dumps(block.model_dump(by_alias=True))
    assert "rt-1" not in serialised
    assert "cs-1" not in serialised


def test_the_state_is_single_use(config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _start(config, tmp_path)
    _token_endpoint(monkeypatch, {"refresh_token": "rt-1", "scope": "a"})

    finish_consent(_redirect(state), workspace=tmp_path)

    with pytest.raises(WebUISettingsError) as raised:
        finish_consent(_redirect(state), workspace=tmp_path)
    assert raised.value.status == 404


def test_a_redirect_matching_no_consent_is_a_404_that_records_nothing(
    config: Config, tmp_path: Path
) -> None:
    """The case a scanner produces."""
    with pytest.raises(WebUISettingsError) as raised:
        finish_consent(_redirect("not-a-state-we-issued"), workspace=tmp_path)

    assert raised.value.status == 404
    assert config.connectors.active == []
    assert SecretStore(tmp_path).list_secrets() == []


def test_fewer_scopes_than_asked_for_are_named(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row shows which classes are unavailable, so the difference has to travel."""
    state = _start(config, tmp_path)
    _token_endpoint(
        monkeypatch,
        {
            "refresh_token": "rt-1",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        },
    )

    result = finish_consent(_redirect(state), workspace=tmp_path)

    assert result["granted_scopes"] == ["https://www.googleapis.com/auth/calendar.readonly"]
    assert result["missing_scopes"] == ["https://www.googleapis.com/auth/calendar.events"]
    # Config records what was granted, not what was asked, so activation refuses the write later.
    assert config.connectors.credentials["google_calendar_credential"].scopes == [
        "https://www.googleapis.com/auth/calendar.readonly"
    ]


def test_a_failed_exchange_is_a_400_and_writes_nothing(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _start(config, tmp_path)
    _token_endpoint(monkeypatch, {"error": "invalid_grant"}, status=400)

    with pytest.raises(WebUISettingsError) as raised:
        finish_consent(_redirect(state), workspace=tmp_path)

    assert raised.value.status == 400
    assert config.connectors.active == []
    assert config.connectors.credentials == {}
    # And nothing was stored, so a failed consent leaves the deployment exactly as it was.
    assert SecretStore(tmp_path).list_secrets() == []


def test_an_exchange_with_no_refresh_token_says_what_to_check(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _start(config, tmp_path)
    _token_endpoint(monkeypatch, {"access_token": "at-1"})

    with pytest.raises(WebUISettingsError) as raised:
        finish_consent(_redirect(state), workspace=tmp_path)

    assert "access_type=offline" in raised.value.message


# --- the record -------------------------------------------------------------------------


def test_the_pending_record_expires() -> None:
    store = PendingConsentStore(ttl_s=0.0)
    entry = store.open(
        connector=CALENDAR,
        credential="c",
        client_id="cid",
        client_secret="cs-1",
        verifier="v",
        redirect_uri=callback_url(ORIGIN),
        scopes=("a",),
    )

    assert store.take(entry.state) is None


def test_the_pending_record_is_bounded() -> None:
    """A person can click twice, and an unbounded map is an unbounded map."""
    from nanoinfra.connectors.pending_consent import MAX_PENDING

    store = PendingConsentStore()
    for _ in range(MAX_PENDING + 4):
        store.open(
            connector=CALENDAR,
            credential="c",
            client_id="cid",
            client_secret="cs-1",
            verifier="v",
            redirect_uri=callback_url(ORIGIN),
            scopes=("a",),
        )

    assert len(store.pending()) <= MAX_PENDING
