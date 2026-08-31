"""The executor writes the credential store for the account that may not (#192).

The bug this closes was shipped: `secrets/` is `drwxr-x---` owned by the executor account, with a
group read so the gateway can list metadata, and the Secrets page therefore failed with a
permission error on every deployment with the privilege split. It worked on a plain `pip install`,
where one uid owns everything, which is why nobody hit it.

The fix moves a **file write** across the wire and never a secret: the gateway holds the key, so
it encrypts, and the executor writes bytes it cannot read.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from nanoinfra.gates.executor.protocol import (
    PROTOCOL_VERSION,
    SECRET_VERBS,
    SecretWriteRequest,
    decode_request,
    encode_request,
)
from nanoinfra.gates.executor.secret_writes import MAX_CIPHERTEXT_BYTES, SecretWriteRunner
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _request(**over: object) -> SecretWriteRequest:
    fields: dict[str, object] = {
        "verb": "create",
        "secret_id": "a" * 32,
        "name": "prod-ssh",
        "secret_kind": "ssh_key",
        "provider_id": "local",
        "ciphertext_b64": base64.b64encode(crypto.encrypt("a-value")).decode("ascii"),
        "created_at": "2026-08-30T00:00:00Z",
        "updated_at": "2026-08-30T00:00:00Z",
        "origin_path": "webui",
        "origin_actor": "webui:ops@example.test",
    }
    fields.update(over)
    return SecretWriteRequest(**fields)  # pyright: ignore[reportArgumentType]


# --- the wire ---------------------------------------------------------------------------


def test_the_frame_round_trips_and_the_version_rose() -> None:
    request = _request()

    assert decode_request(encode_request(request)) == request
    assert PROTOCOL_VERSION == 6
    # The kind lives in the envelope, so the field that describes the *secret* cannot be called
    # `kind` -- the decoder strips envelope keys before matching fields, and a field of that name
    # went missing on every frame. Found by round-tripping one.
    assert "kind" not in SecretWriteRequest.__dataclass_fields__
    assert "secret_kind" in SecretWriteRequest.__dataclass_fields__


def test_the_wire_carries_ciphertext_and_no_plaintext() -> None:
    payload = json.loads(encode_request(_request()))

    assert "a-value" not in json.dumps(payload)
    assert base64.b64decode(payload["ciphertext_b64"])


def test_the_verbs_are_three_and_none_of_them_reads() -> None:
    assert SECRET_VERBS == {"create", "update", "delete"}


# --- the writes -------------------------------------------------------------------------


def test_a_create_lands_and_reads_back(tmp_path: Path) -> None:
    runner = SecretWriteRunner(workspace=tmp_path)

    response = runner.handle(_request())

    assert response.ok is True
    stored = SecretStore(tmp_path).get("a" * 32)
    assert stored is not None
    assert stored.name == "prod-ssh"
    # The executor wrote bytes it never decrypted; the value is readable with the key alone.
    assert SecretStore(tmp_path).resolve_plaintext("a" * 32) == "a-value"


def test_a_create_over_an_existing_id_is_refused(tmp_path: Path) -> None:
    runner = SecretWriteRunner(workspace=tmp_path)
    runner.handle(_request())

    response = runner.handle(_request(name="second"))

    assert response.ok is False
    assert "already exists" in response.reason
    assert SecretStore(tmp_path).get("a" * 32).name == "prod-ssh"  # pyright: ignore[reportOptionalMemberAccess]


def test_an_update_that_names_nothing_is_not_turned_into_a_create(tmp_path: Path) -> None:
    """A caller that believes it is editing must not silently add a row."""
    runner = SecretWriteRunner(workspace=tmp_path)

    response = runner.handle(_request(verb="update"))

    assert response.ok is False
    assert "nothing to update" in response.reason
    assert SecretStore(tmp_path).get("a" * 32) is None


def test_an_update_replaces_the_value_and_keeps_the_creation_time(tmp_path: Path) -> None:
    runner = SecretWriteRunner(workspace=tmp_path)
    runner.handle(_request())

    response = runner.handle(
        _request(
            verb="update",
            ciphertext_b64=base64.b64encode(crypto.encrypt("rotated")).decode("ascii"),
            created_at="",
            updated_at="2026-08-31T00:00:00Z",
        )
    )

    assert response.ok is True
    stored = SecretStore(tmp_path).get("a" * 32)
    assert stored is not None
    assert stored.created_at == "2026-08-30T00:00:00Z"
    assert stored.updated_at == "2026-08-31T00:00:00Z"
    assert SecretStore(tmp_path).resolve_plaintext("a" * 32) == "rotated"


def test_a_delete_removes_the_record_and_a_second_one_refuses(tmp_path: Path) -> None:
    runner = SecretWriteRunner(workspace=tmp_path)
    runner.handle(_request())

    first = runner.handle(_request(verb="delete"))
    second = runner.handle(_request(verb="delete"))

    assert first.ok is True
    assert second.ok is False
    assert SecretStore(tmp_path).get("a" * 32) is None


# --- what the runner refuses ------------------------------------------------------------


def test_an_unknown_verb_is_refused(tmp_path: Path) -> None:
    response = SecretWriteRunner(workspace=tmp_path).handle(_request(verb="read"))

    assert response.ok is False
    assert "not one of" in response.reason


def test_a_malformed_ciphertext_is_refused(tmp_path: Path) -> None:
    response = SecretWriteRunner(workspace=tmp_path).handle(
        _request(ciphertext_b64="not base64 at all!!")
    )

    assert response.ok is False
    assert "base64" in response.reason


def test_an_empty_ciphertext_is_refused(tmp_path: Path) -> None:
    response = SecretWriteRunner(workspace=tmp_path).handle(_request(ciphertext_b64=""))

    assert response.ok is False
    assert "nothing to store" in response.reason


def test_a_ciphertext_above_the_cap_is_refused(tmp_path: Path) -> None:
    oversized = base64.b64encode(b"x" * (MAX_CIPHERTEXT_BYTES + 1)).decode("ascii")

    response = SecretWriteRunner(workspace=tmp_path).handle(_request(ciphertext_b64=oversized))

    assert response.ok is False
    assert "above the" in response.reason


def test_a_nameless_secret_is_refused(tmp_path: Path) -> None:
    response = SecretWriteRunner(workspace=tmp_path).handle(_request(name="   "))

    assert response.ok is False
    assert "needs a name" in response.reason


def test_an_invalid_id_is_refused_by_the_store_rules(tmp_path: Path) -> None:
    """The caller names the record it writes, so the id is checked rather than trusted."""
    response = SecretWriteRunner(workspace=tmp_path).handle(_request(secret_id="../../etc/passwd"))

    assert response.ok is False
    # Nothing was written anywhere, and in particular not outside the store.
    assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == []


# --- the whole path, through a real executor --------------------------------------------


async def test_a_refused_local_write_reaches_the_executor_and_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: the gateway cannot write the store, so the Secrets page failed.

    The refusal is simulated at the store seam -- the container's ownership cannot be
    reproduced without two accounts -- and everything after it is real: the encode, the socket,
    the executor's own runner, and the file it writes.
    """
    import threading

    from nanoinfra.gates.executor.server import serve_forever
    from nanoinfra.secrets.store import SecretsStoreUnreadableError
    from nanoinfra.webui.secrets_api import create_webui_secret

    socket_path = tmp_path / "e.sock"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    thread = threading.Thread(
        target=serve_forever,
        args=(socket_path,),
        kwargs={"workspace": workspace, "max_requests": 1},
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if socket_path.exists():
            break
        await asyncio.sleep(0.02)
    assert socket_path.exists(), "the executor never bound its socket"

    monkeypatch.setenv("NANOINFRA_EXECUTOR_SOCKET", str(socket_path))

    def _refuse(secret: object) -> None:
        raise SecretsStoreUnreadableError("the secret store belongs to another account")

    # The caller's own instance, not the class: the executor runs in this process during the
    # test and builds its own store, and patching the class would refuse its write too --
    # which is what happened on the first run of this test.
    caller = SecretStore(workspace)
    monkeypatch.setattr(caller, "write_record", _refuse)

    payload = create_webui_secret(
        caller,
        {"name": "prod-ssh", "kind": "token", "providerId": "local", "value": "a-value"},
    )

    written = payload["secret"]["id"]
    for _ in range(200):
        if SecretStore(workspace).get(written) is not None:
            break
        await asyncio.sleep(0.02)

    stored = SecretStore(workspace).get(written)
    assert stored is not None, "the executor did not write the record"
    assert stored.name == "prod-ssh"
    assert SecretStore(workspace).resolve_plaintext(written) == "a-value"
    # The public payload never carries either form of the value.
    assert "a-value" not in json.dumps(payload)


async def test_a_refused_write_with_no_executor_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment fault has to read as one, not as a bad payload."""
    from nanoinfra.secrets.store import SecretsStoreUnreadableError
    from nanoinfra.webui.secrets_api import create_webui_secret

    monkeypatch.setenv("NANOINFRA_EXECUTOR_SOCKET", str(tmp_path / "absent.sock"))

    def _refuse(secret: object) -> None:
        raise SecretsStoreUnreadableError("the secret store belongs to another account")

    caller = SecretStore(tmp_path)
    monkeypatch.setattr(caller, "write_record", _refuse)

    with pytest.raises(SecretsStoreUnreadableError, match="executor is not reachable"):
        create_webui_secret(
            caller,
            {"name": "prod-ssh", "kind": "token", "providerId": "local", "value": "a-value"},
        )


def test_a_record_written_into_a_group_readable_store_stays_group_readable(
    tmp_path: Path,
) -> None:
    """The container's `secrets/` is `drwxr-s---` so the gateway can list metadata.

    A hardcoded 0600 wrote a file the gateway could not read back: the create succeeded through
    the executor and the next listing raised `SecretsStoreUnreadableError`. Found by running it
    in that layout, not by reading the code.
    """
    import stat

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_dir.chmod(0o750)

    SecretWriteRunner(workspace=tmp_path).handle(_request())

    written = secrets_dir / f"{'a' * 32}.json"
    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode == 0o640, oct(mode)


def test_a_record_in_a_private_store_stays_private(tmp_path: Path) -> None:
    """The single-uid case is unchanged: nobody else has a reason to read it."""
    import stat

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_dir.chmod(0o700)

    SecretWriteRunner(workspace=tmp_path).handle(_request())

    written = secrets_dir / f"{'a' * 32}.json"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
