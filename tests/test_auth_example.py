# tests/test_auth_example.py
"""The example in examples/auth must not become an open agent -- nanoinfraorg/nanoinfra#78.

`examples/auth/` is the primary example of an identity in front of the gateway (#75). A reader
copies it, edits it, and runs it. Four edits open it, and each one is silent:

1. A published port loses its `127.0.0.1:` prefix, and the gateway or the login form answers the
   whole network.
2. `allowAnyVerifiedIdentity` appears, and every account the provider will authenticate becomes an
   operator of this agent.
3. `allowedIdentities` empties, which is the same outcome written a different way.
4. The oauth2-proxy allowlist becomes `*`, and the proxy admits whoever completes the flow. The
   gateway's approver list gives no warning, because it was never the list for that job.

A fifth edit is not an edit at all: a placeholder that stays a placeholder in a deployment. So this
file also reads every secret and requires the `REPLACE-ME` marker, which keeps the copy that a
reader edits distinct from the copy this repository ships.

The test reads the files. It runs no container, and it starts no process. A test that needed Docker
would not run in CI, and the fact under test is a fact about the text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "auth"

# The addresses a published port may bind to. Anything else answers a client that ran no OIDC flow.
LOOPBACK_HOST_IPS = frozenset({"127.0.0.1", "::1"})

# The marker that names a value as a placeholder. It is one string in one place, because two
# spellings would let one file drift out of the check.
PLACEHOLDER_MARKER = "REPLACE-ME"

# Environment keys that carry a secret. A key that matches must carry the marker.
SECRET_KEY_PATTERN = re.compile(r"SECRET|TOKEN|PASSWORD|KEY|CREDENTIAL", re.IGNORECASE)

# Where the compose file mounts the oauth2-proxy files. The test maps a container path back to the
# file in this directory, so it reads what the proxy reads.
CONTAINER_CONFIG_DIR = "/etc/oauth2-proxy/"


def _load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((EXAMPLE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return _load_yaml("compose.yaml")


@pytest.fixture(scope="module")
def gateway_config() -> dict[str, Any]:
    return json.loads((EXAMPLE_DIR / "config.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def trusted_proxy_auth(gateway_config: dict[str, Any]) -> dict[str, Any]:
    websocket = gateway_config["channels"]["websocket"]
    return websocket["trustedProxyAuth"]


def _host_ip(entry: Any) -> str:
    """Return the host address of one compose port entry, or "" for every interface."""
    if isinstance(entry, dict):
        return str(entry.get("host_ip", ""))
    text = str(entry)
    if text.startswith("["):
        # The long form of an IPv6 host address, such as "[::1]:5556:5556".
        return text[1 : text.index("]")]
    parts = text.split(":")
    if len(parts) < 3:
        # "8765" or "8765:8765" names no address, so Docker binds every interface.
        return ""
    return parts[0]


def _published_ports(compose: dict[str, Any]) -> list[tuple[str, Any]]:
    published: list[tuple[str, Any]] = []
    for name, service in compose["services"].items():
        for entry in service.get("ports", []) or []:
            published.append((name, entry))
    return published


def _environment_items(service: dict[str, Any]) -> list[tuple[str, str]]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return [(str(key), str(value)) for key, value in environment.items()]
    items: list[tuple[str, str]] = []
    for entry in environment:
        key, _, value = str(entry).partition("=")
        items.append((key, value))
    return items


def _find_key(node: Any, wanted: set[str]) -> list[Any]:
    """Every value stored under one of ``wanted``, at any depth."""
    found: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in wanted:
                found.append(value)
            found.extend(_find_key(value, wanted))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_key(item, wanted))
    return found


def _cfg_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def test_no_service_publishes_a_port_outside_loopback(compose: dict[str, Any]) -> None:
    published = _published_ports(compose)
    assert published, "the compose file publishes nothing, so this test asserts nothing"
    for name, entry in published:
        host_ip = _host_ip(entry)
        assert host_ip in LOOPBACK_HOST_IPS, (
            f"service {name!r} publishes {entry!r} on {host_ip or 'every interface'}. "
            "An example binds to loopback."
        )


def test_the_gateway_publishes_no_port_at_all(compose: dict[str, Any]) -> None:
    """oauth2-proxy is the only way in. A published gateway port carries no identity."""
    gateway = compose["services"]["nanoinfra-gateway"]
    assert not gateway.get("ports"), (
        "the gateway publishes a port, so a browser reaches it without running the OIDC flow"
    )


def test_allow_any_verified_identity_is_absent_or_false(gateway_config: dict[str, Any]) -> None:
    values = _find_key(gateway_config, {"allowAnyVerifiedIdentity", "allow_any_verified_identity"})
    assert all(value is False for value in values), (
        "the example opts out of the identity list. A verified token is not an invitation."
    )


def test_allowed_identities_names_somebody(trusted_proxy_auth: dict[str, Any]) -> None:
    identities = trusted_proxy_auth["allowedIdentities"]
    assert identities, "allowedIdentities is empty, so the gateway admits nobody or everybody"
    assert all(identity.strip() for identity in identities)
    assert "*" not in identities


def test_the_oauth2_proxy_allowlist_is_not_a_wildcard() -> None:
    cfg = (EXAMPLE_DIR / "oauth2-proxy.cfg").read_text(encoding="utf-8")

    domains = _cfg_value(cfg, "email_domains")
    assert domains is None or "*" not in domains, (
        "email_domains holds a wildcard, so the proxy admits whoever the provider authenticates"
    )

    emails_file = _cfg_value(cfg, "authenticated_emails_file")
    assert emails_file or domains, "oauth2-proxy names no allowlist, and the allowlist is not optional"
    if emails_file is None:
        return

    container_path = emails_file.strip('"')
    assert container_path.startswith(CONTAINER_CONFIG_DIR)
    local_path = EXAMPLE_DIR / container_path[len(CONTAINER_CONFIG_DIR) :]
    lines = [line.strip() for line in local_path.read_text(encoding="utf-8").splitlines()]
    entries = [line for line in lines if line]
    assert entries, f"{local_path.name} is empty, which admits nobody and teaches nothing"
    for entry in entries:
        assert "*" not in entry, f"{local_path.name} holds the wildcard {entry!r}"
        assert "@" in entry, f"{local_path.name} holds {entry!r}, which is not an address"


def test_every_secret_in_the_compose_file_is_a_placeholder(compose: dict[str, Any]) -> None:
    checked = 0
    for name, service in compose["services"].items():
        for key, value in _environment_items(service):
            if not SECRET_KEY_PATTERN.search(key):
                continue
            checked += 1
            assert PLACEHOLDER_MARKER in value, (
                f"service {name!r} sets {key} to a value that is not a named placeholder"
            )
    assert checked >= 2, "the compose file names fewer secrets than the example has, so a key moved"


def test_every_secret_in_the_other_files_is_a_placeholder() -> None:
    """The client secret and the password hash live outside the compose file, and the rule is one rule."""
    dex = _load_yaml("dex.yaml")
    client = dex["staticClients"][0]
    password = dex["staticPasswords"][0]
    proxy_provider = _load_yaml("oauth2-proxy.alpha.yaml")["providers"][0]

    assert PLACEHOLDER_MARKER in client["secret"]
    assert PLACEHOLDER_MARKER in proxy_provider["clientSecret"]
    # The hash keeps bcrypt shape, so Dex starts and no password matches it.
    assert password["hash"].startswith("$2y$10$")
    assert "REPLACEME" in password["hash"]


def test_the_gateway_config_names_no_secret(gateway_config: dict[str, Any]) -> None:
    """A reader copies config.json. The provider key reaches the gateway as an environment variable."""
    text = json.dumps(gateway_config)
    assert "apiKey" not in text
    assert "api_key" not in text


def test_the_three_identity_lists_name_the_same_person(
    gateway_config: dict[str, Any], trusted_proxy_auth: dict[str, Any]
) -> None:
    """A person the proxy admits and the gateway refuses is a misconfiguration nobody sees until an approval fails."""
    identities = set(trusted_proxy_auth["allowedIdentities"])
    emails = {
        line.strip()
        for line in (EXAMPLE_DIR / "oauth2-proxy.emails").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    dex_users = {entry["email"] for entry in _load_yaml("dex.yaml")["staticPasswords"]}
    assert identities == emails == dex_users

    approvers = gateway_config["gates"]["approvers"]
    assert approvers, "the example shows an approval, so it names an approver"
    for approver in approvers:
        assert approver["channel"] == "webui"
        prefix, _, identity = approver["sender"].partition(":")
        assert prefix == "webui", "the prefix names the path that authenticated the person"
        assert identity in identities, f"{approver['sender']!r} names nobody the gateway admits"


def test_the_assertion_is_verified_and_not_merely_trusted(
    trusted_proxy_auth: dict[str, Any],
) -> None:
    """`plain` trusts a bare string. The example verifies a signature, so it declares `jwt`."""
    assert trusted_proxy_auth["assertionFormat"] == "jwt"
    assert trusted_proxy_auth["assertionHeader"].casefold() != "authorization"
    assert trusted_proxy_auth["identityClaim"]
    assert trusted_proxy_auth["issuer"]
    assert trusted_proxy_auth["audience"]
    assert trusted_proxy_auth["jwksUrl"]


def test_the_trusted_peer_is_one_address_on_the_compose_network(
    compose: dict[str, Any], trusted_proxy_auth: dict[str, Any]
) -> None:
    """A CIDR wider than the proxy would trust another container as an asserting proxy."""
    proxy_address = compose["services"]["oauth2-proxy"]["networks"]["auth"]["ipv4_address"]
    assert trusted_proxy_auth["trustedPeerCidrs"] == [f"{proxy_address}/32"]


def test_the_proxy_sets_the_header_the_gateway_reads(
    compose: dict[str, Any], trusted_proxy_auth: dict[str, Any]
) -> None:
    """Two files name one header. A rename in one file authenticates nobody."""
    del compose
    injected = _load_yaml("oauth2-proxy.alpha.yaml")["injectRequestHeaders"]
    names = {header["name"] for header in injected}
    assert trusted_proxy_auth["assertionHeader"] in names
    assert "Authorization" not in names, (
        "the gateway's routes already read an API token from Authorization"
    )
    header = next(item for item in injected if item["name"] == trusted_proxy_auth["assertionHeader"])
    assert header["values"] == [{"claim": "id_token"}], (
        "the assertion is the signed token, and not a claim the gateway can only trust"
    )
