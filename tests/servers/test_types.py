from __future__ import annotations

from nanoinfra.servers.types import Server, ServerSummary


def test_to_dict_round_trips():
    server = Server(
        id="a" * 32,
        name="prod-web-01",
        provider_id="ssh",
        config={"host": "10.0.1.5", "port": "22", "username": "deploy"},
        secret_ref="b" * 32,
        tags=["prod", "web"],
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )
    assert Server.from_dict(server.to_dict()) == server


def test_to_dict_uses_camel_case_keys():
    server = Server(
        id="a" * 32,
        name="n",
        provider_id="api",
        config={"baseUrl": "https://example.com"},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )
    data = server.to_dict()
    assert data["providerId"] == "api"
    assert data["secretRef"] is None
    assert data["createdAt"] == "t"
    assert data["updatedAt"] == "t"


def test_server_summary_to_dict():
    summary = ServerSummary(id="a" * 32, name="n", provider_id="ssh", tags=["prod"], updated_at="t")
    assert summary.to_dict() == {
        "id": "a" * 32,
        "name": "n",
        "providerId": "ssh",
        "tags": ["prod"],
        "updatedAt": "t",
    }
