from __future__ import annotations

import pytest

from nanoinfra.servers.normalize import ServerValidationError, normalize_server_input


def test_normalize_valid_ssh_server():
    server = normalize_server_input(
        {
            "name": "prod-web-01",
            "providerId": "ssh",
            "config": {"host": "10.0.1.5", "port": "22", "username": "deploy"},
            "secretRef": "b" * 32,
            "tags": ["prod", "web"],
        },
        server_id="a" * 32,
    )
    assert server.id == "a" * 32
    assert server.name == "prod-web-01"
    assert server.provider_id == "ssh"
    assert server.config == {"host": "10.0.1.5", "port": "22", "username": "deploy"}
    assert server.secret_ref == "b" * 32
    assert server.tags == ["prod", "web"]


def test_normalize_defaults_missing_optional_fields():
    server = normalize_server_input({"name": "n", "providerId": "api"}, server_id="a" * 32)
    assert server.config == {}
    assert server.secret_ref is None
    assert server.tags == []


def test_normalize_rejects_missing_name():
    with pytest.raises(ServerValidationError, match="name"):
        normalize_server_input({"providerId": "ssh"}, server_id="a" * 32)


def test_normalize_rejects_unknown_provider():
    with pytest.raises(ServerValidationError, match="providerId"):
        normalize_server_input({"name": "n", "providerId": "telnet"}, server_id="a" * 32)


def test_normalize_rejects_non_dict_payload():
    with pytest.raises(ServerValidationError):
        normalize_server_input("not a dict", server_id="a" * 32)  # type: ignore[arg-type]
