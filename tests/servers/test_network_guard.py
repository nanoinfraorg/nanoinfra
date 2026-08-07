from __future__ import annotations

from nanoinfra.servers.network_guard import validate_server_target


def test_allows_rfc1918_addresses():
    for host in ("10.0.1.5", "172.16.0.1", "192.168.1.1"):
        ok, error = validate_server_target(host)
        assert ok is True, f"{host} should be allowed: {error}"


def test_allows_public_addresses():
    ok, error = validate_server_target("8.8.8.8")
    assert ok is True, error


def test_blocks_loopback():
    ok, error = validate_server_target("127.0.0.1")
    assert ok is False
    assert "loopback" in error.lower() or "127.0.0.1" in error


def test_blocks_cloud_metadata_address():
    ok, error = validate_server_target("169.254.169.254")
    assert ok is False


def test_blocks_ipv6_loopback_and_link_local():
    ok, _ = validate_server_target("::1")
    assert ok is False
    ok, _ = validate_server_target("fe80::1")
    assert ok is False


def test_resolves_hostname_and_blocks_if_metadata(monkeypatch):
    import socket

    def fake_getaddrinfo(host, *args, **kwargs):
        assert host == "metadata.internal.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, error = validate_server_target("metadata.internal.example")
    assert ok is False


def test_unresolvable_hostname_is_rejected():
    ok, error = validate_server_target("this-host-does-not-exist.invalid")
    assert ok is False
    assert "resolve" in error.lower()
