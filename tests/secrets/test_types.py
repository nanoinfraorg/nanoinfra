from __future__ import annotations

from nanoinfra.secrets.types import Secret


def test_to_storage_dict_includes_ciphertext_to_public_dict_does_not():
    secret = Secret(
        id="a" * 32,
        name="prod-db-password",
        kind="password",
        provider_id="local",
        ciphertext=b"encrypted-bytes",
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )

    storage = secret.to_storage_dict()
    assert storage["ciphertext"] == "ZW5jcnlwdGVkLWJ5dGVz"  # base64.b64encode(b"encrypted-bytes")

    public = secret.to_public_dict()
    assert public == {
        "id": "a" * 32,
        "name": "prod-db-password",
        "kind": "password",
        "providerId": "local",
        "createdAt": "2026-08-06T00:00:00+00:00",
        "updatedAt": "2026-08-06T00:00:00+00:00",
    }
    assert "ciphertext" not in public
    assert "value" not in public


def test_from_storage_dict_round_trips():
    secret = Secret(
        id="b" * 32,
        name="api-token",
        kind="api_key",
        provider_id="postgres",
        ciphertext=b"\x01\x02\x03",
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )
    round_tripped = Secret.from_storage_dict(secret.to_storage_dict())
    assert round_tripped == secret
