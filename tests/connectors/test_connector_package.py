"""The declared contract, checked before a package is written to disk (#195, parts 0-2).

The rule that makes the others hold is the first one: an **unknown key is a refusal**. Everything
else here is a specific thing a package must not be able to do -- ship code, declare a dependency,
call a write a read, or name a host nobody reviewed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.connectors.credentials import (
    ConnectorCredential,
    CredentialError,
    check_connector_hosts,
)
from nanoinfra.connectors.package import (
    PACKAGE_SCHEMA,
    ConnectorPackageError,
    load_connector_package,
    parse_connector_package,
)


def manifest(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "$schema": PACKAGE_SCHEMA,
        "name": "acme-crm",
        "displayName": "Acme CRM",
        "description": "Reads and writes Acme contacts.",
        "baseUrl": "https://api.acme.example",
        "credential": {
            "kind": "oauth2",
            "tokenUrl": "https://api.acme.example/oauth/token",
            "allowedHosts": ["api.acme.example"],
            "scopes": {"read": ["crm.read"], "mutate.remote": ["crm.write"]},
        },
        "operations": [
            {
                "name": "list_contacts",
                "class": "read",
                "method": "GET",
                "path": "/v1/contacts",
                "collection": "items",
                "returns": ["id", "name"],
            },
            {
                "name": "create_contact",
                "class": "mutate.remote",
                "method": "POST",
                "path": "/v1/contacts",
            },
        ],
        "dependencies": [],
    }
    payload.update(overrides)
    return payload


def write_package(directory: Path, payload: dict[str, Any] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "connector.json").write_text(
        json.dumps(payload if payload is not None else manifest()), encoding="utf-8"
    )
    return directory


# --- what a valid package produces -------------------------------------------------------


def test_a_package_becomes_the_same_plugin_a_manifest_produces() -> None:
    """The payoff for having built the declarative kind first: from the registry down, a
    marketplace connector and a bundled one are the same object, so the gate, the ceiling, the
    per-class token and the audit record need no changes at all."""
    plugin = parse_connector_package(manifest())

    assert plugin.name == "acme-crm"
    assert plugin.base_url == "https://api.acme.example"
    assert [op.name for op in plugin.operations] == ["list_contacts", "create_contact"]
    assert plugin.operation("list_contacts") is not None
    assert plugin.classes == ("read", "mutate.remote")
    assert plugin.credential.scopes_for("read") == ("crm.read",)
    assert plugin.credential.allowed_hosts == ("api.acme.example",)


def test_the_projection_and_the_collection_survive() -> None:
    plugin = parse_connector_package(manifest())
    listing = plugin.operation("list_contacts")

    assert listing is not None
    assert listing.returns == ("id", "name")
    assert listing.collection == "items"


# --- the rule that makes the others hold -------------------------------------------------


def test_an_unknown_key_is_a_refusal_rather_than_an_ignored_field() -> None:
    """A package must not carry an instruction a later version would start honouring."""
    with pytest.raises(ConnectorPackageError, match="does not validate"):
        parse_connector_package(manifest(runtime="runtime.py"))


def test_an_unknown_key_inside_an_operation_is_refused_too() -> None:
    payload = manifest()
    payload["operations"][0]["retryPolicy"] = "aggressive"

    with pytest.raises(ConnectorPackageError, match="does not validate"):
        parse_connector_package(payload)


def test_a_package_naming_another_schema_is_refused() -> None:
    with pytest.raises(ConnectorPackageError, match=r"\$schema"):
        parse_connector_package(manifest(**{"$schema": "https://example.invalid/other.json"}))


# --- the format runs no code -------------------------------------------------------------


def test_a_declared_dependency_is_refused_and_says_why() -> None:
    with pytest.raises(ConnectorPackageError, match="runs no code"):
        parse_connector_package(manifest(dependencies=["httpx>=0.27"]))


def test_a_package_holding_an_importable_module_is_refused(tmp_path: Path) -> None:
    directory = write_package(tmp_path / "acme-crm")
    (directory / "runtime.py").write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(ConnectorPackageError, match="runs no code"):
        load_connector_package(directory)


def test_the_code_check_runs_on_every_load_and_not_only_at_install(tmp_path: Path) -> None:
    """A package directory is a directory: an install that validated once says nothing about
    what is there the next time the gateway starts."""
    directory = write_package(tmp_path / "acme-crm")
    assert load_connector_package(directory).name == "acme-crm"

    (directory / "helper.so").write_bytes(b"\x7fELF")

    with pytest.raises(ConnectorPackageError, match="runs no code"):
        load_connector_package(directory)


# --- the capability classes --------------------------------------------------------------


def test_a_class_that_is_not_a_capability_class_is_refused() -> None:
    payload = manifest()
    payload["operations"][0]["class"] = "google.read"

    with pytest.raises(ConnectorPackageError, match="capability classes are"):
        parse_connector_package(payload)


def test_a_read_class_on_a_writing_method_is_refused() -> None:
    """The same load-time rule a first-party manifest gets, reported in this module's own type."""
    payload = manifest()
    payload["operations"][0]["method"] = "POST"

    with pytest.raises(ConnectorPackageError, match="read"):
        parse_connector_package(payload)


# --- where a token may go ----------------------------------------------------------------


def test_a_credential_that_names_no_hosts_is_refused() -> None:
    """A package that holds a token and names no hosts can send it anywhere."""
    payload = manifest()
    payload["credential"]["allowedHosts"] = []

    with pytest.raises(ConnectorPackageError, match="may address"):
        parse_connector_package(payload)


def test_a_package_that_contradicts_its_own_declaration_is_refused() -> None:
    with pytest.raises(ConnectorPackageError, match="contradicts its own declaration"):
        parse_connector_package(manifest(baseUrl="https://evil.example"))


def test_an_http_base_url_is_refused() -> None:
    payload = manifest(baseUrl="http://api.acme.example")
    payload["credential"]["allowedHosts"] = ["api.acme.example"]

    with pytest.raises(ConnectorPackageError, match="https"):
        parse_connector_package(payload)


def test_activation_refuses_a_host_the_credential_does_not_allow() -> None:
    """The hole the confined host does not close: a manifest declares where a token goes, and
    Landlock does not stop an outbound HTTPS call. Config decides."""
    plugin = parse_connector_package(manifest())
    credential = ConnectorCredential(
        name="acme",
        client_id="cid",
        secret_ref="ref",
        token_url="https://api.acme.example/oauth/token",
        allowed_hosts=("api.other.example",),
    )

    with pytest.raises(CredentialError) as raised:
        check_connector_hosts(plugin, credential)

    # Both halves named, the way the maxClass refusal names both: a message that says only
    # "refused" makes the operator guess which side to change.
    assert "api.acme.example" in str(raised.value)
    assert "api.other.example" in str(raised.value)


def test_activation_allows_a_credential_that_names_the_host() -> None:
    plugin = parse_connector_package(manifest())
    credential = ConnectorCredential(
        name="acme",
        client_id="cid",
        secret_ref="ref",
        token_url="https://api.acme.example/oauth/token",
        allowed_hosts=("API.Acme.Example",),
    )

    check_connector_hosts(plugin, credential)


def test_a_credential_with_no_hosts_is_unconstrained_for_a_reviewed_package() -> None:
    """Empty means the manifest decides, which is what a first-party package reviewed in this
    repository gets."""
    plugin = parse_connector_package(manifest())
    credential = ConnectorCredential(
        name="acme", client_id="cid", secret_ref="ref", token_url="", allowed_hosts=()
    )

    check_connector_hosts(plugin, credential)


# --- identity and bounds -----------------------------------------------------------------


def test_a_name_that_does_not_match_the_install_is_refused() -> None:
    with pytest.raises(ConnectorPackageError, match="asked for"):
        parse_connector_package(manifest(), expected_name="other-crm")


def test_a_package_with_no_operations_is_refused() -> None:
    with pytest.raises(ConnectorPackageError, match="non-empty operations"):
        parse_connector_package(manifest(operations=[]))


def test_a_directory_with_no_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "acme-crm").mkdir()

    with pytest.raises(ConnectorPackageError, match="connector.json"):
        load_connector_package(tmp_path / "acme-crm")


def test_invalid_json_is_refused_with_the_reason(tmp_path: Path) -> None:
    directory = tmp_path / "acme-crm"
    directory.mkdir()
    (directory / "connector.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ConnectorPackageError, match="not valid JSON"):
        load_connector_package(directory)


def test_the_setup_form_becomes_the_shape_a_channel_declares() -> None:
    """A connector's form is rendered by the code that renders a channel's, so the package's JSON
    is translated into that shape rather than a second one being introduced."""
    plugin = parse_connector_package(
        manifest(
            setup={
                "officialUrl": "https://acme.example/docs",
                "fields": [
                    {"name": "accountId", "kind": "string", "required": True},
                    {"name": "includeArchived", "kind": "bool", "default": False},
                ],
            }
        )
    )

    assert plugin.setup is not None
    assert sorted(plugin.setup.fields) == ["accountId", "includeArchived"]
    assert plugin.setup.fields["includeArchived"].kind == "bool"
    assert plugin.setup.official_url == "https://acme.example/docs"


def test_a_setup_field_may_not_introduce_a_widget_the_form_never_rendered() -> None:
    with pytest.raises(ConnectorPackageError, match="string, bool or int"):
        parse_connector_package(
            manifest(setup={"fields": [{"name": "when", "kind": "datepicker"}]})
        )


# --- a connector that needs no credential ------------------------------------------------


def test_a_public_api_connector_needs_no_credential() -> None:
    """The simplest connector somebody writes -- one read against a public API -- must be able to
    run. Requiring a credential would have made it the only kind that could not."""
    plugin = parse_connector_package(
        manifest(
            credential={"kind": "none"},
            operations=[
                {"name": "current_weather", "class": "read", "method": "GET", "path": "/v1/forecast"}
            ],
        )
    )

    assert plugin.credential.kind == "none"
    # No hosts required: there is no token to send anywhere.
    assert plugin.credential.allowed_hosts == ()


def test_the_bundled_hello_world_example_loads() -> None:
    """The example in `examples/connectors/hello-world` is the documentation. If it stops loading,
    the thing people copy is broken."""
    root = Path(__file__).resolve().parents[2] / "examples" / "connectors" / "hello-world"

    plugin = load_connector_package(root)

    assert plugin.name == "hello-world"
    assert plugin.classes == ("read",)
    assert plugin.credential.kind == "none"
    operation = plugin.operation("current_weather")
    assert operation is not None
    assert operation.method == "GET"
    # The projection is the point of the example, so it has to stay in it.
    assert "current.temperature_2m" in operation.returns
