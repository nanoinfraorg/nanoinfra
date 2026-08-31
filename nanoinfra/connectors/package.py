"""A connector somebody else wrote, declared in JSON and checked before it is trusted (#195).

A first-party connector is `manifest.py`, and Python is the wrong format for a package this
deployment did not write: *importing it to find out what it declares is running it*. So a
marketplace package declares itself in `connector.json`, and that file is the only entry point --
an archive holding an importable module is refused, because a package that ships code is asking for
a runtime this format does not offer.

Five rules, and the first is the one that makes the others hold:

1. **Every key is known.** An unknown key is a refusal, not an ignored field, so a package cannot
   carry an instruction that a future version would start honouring.
2. Every operation's class is one of `CAPABILITY_CLASSES`, and a `read` class on a writing method is
   refused -- the same load-time rule `contracts.py` enforces for a first-party manifest.
3. `baseUrl` is https, its host is in `credential.allowedHosts`, and every path starts with `/`.
4. `dependencies` must be empty. A declared dependency means the package expects code to run, and
   this format runs none.
5. The name is a valid connector name and matches the id the caller asked for.

The output is the same `ConnectorPlugin` a first-party manifest produces, so from the registry down
a marketplace connector and a bundled one are the same object -- and the gate, the ceiling, the
per-class token and the audit record need no changes at all. That is the payoff for having built
the declarative kind first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from nanoinfra.agent.tools.capabilities import CAPABILITY_CLASSES
from nanoinfra.channels.contracts import ChannelFieldSpec, SetupRequirement
from nanoinfra.connectors.contracts import (
    ConnectorCredentialSpec,
    ConnectorMentionSpec,
    ConnectorOperation,
    ConnectorPlugin,
    ConnectorSetupSpec,
)

#: The file whose presence marks an archive as a connector package, mirroring `plugin.json` for an
#: Agent Plugins package in the same pipeline.
PACKAGE_FILE = "connector.json"

#: The schema this version validates. A package naming a different one expects semantics this code
#: does not check, so it is refused rather than accepted on the assumption that the shapes line up.
PACKAGE_SCHEMA = "https://nanoinfra.org/schemas/connector/1.0.0/connector.schema.json"

#: What a package may not contain. Anything importable means the package expects to run.
FORBIDDEN_SUFFIXES = (".py", ".pyc", ".pyo", ".pth", ".so", ".dylib", ".dll")

MAX_PACKAGE_BYTES = 256 * 1024
MAX_OPERATIONS = 64
MAX_SETUP_FIELDS = 16
MAX_MENTIONS = 8

_TOP_LEVEL_KEYS = frozenset({
    "$schema",
    "name",
    "displayName",
    "description",
    "baseUrl",
    "credential",
    "setup",
    "operations",
    "mentions",
    "dependencies",
    "skill",
})
_CREDENTIAL_KEYS = frozenset({"kind", "tokenUrl", "scopes", "allowedHosts"})
_OPERATION_KEYS = frozenset({
    "name",
    "class",
    "method",
    "path",
    "summary",
    "returns",
    "collection",
    "parameters",
})
_SETUP_KEYS = frozenset({"fields", "officialUrl"})
_SETUP_FIELD_KEYS = frozenset({
    "name",
    "kind",
    "required",
    "default",
})
_MENTION_KEYS = frozenset({
    "kind",
    "operation",
    "idField",
    "labelField",
    "detailFields",
    "argument",
})


class ConnectorPackageError(ValueError):
    """A package that will not be written to disk, and why."""


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectorPackageError(f"{where} must be a JSON object")
    return cast(dict[str, Any], value)


def _known_keys(payload: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConnectorPackageError(
            f"{where} declares {unknown}, which this version does not validate. An unknown key "
            f"is a refusal rather than an ignored field, because a package must not carry an "
            f"instruction a later version would start honouring."
        )


def _text(payload: dict[str, Any], key: str, where: str, *, required: bool = True) -> str:
    value = payload.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ConnectorPackageError(f"{where} needs a non-empty string {key!r}")
    return value.strip()


def _string_tuple(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConnectorPackageError(f"{where} must be a list of strings")
    items = cast(list[object], value)
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ConnectorPackageError(f"{where} must hold non-empty strings")
    return tuple(str(item).strip() for item in items)


def _credential(payload: object) -> ConnectorCredentialSpec:
    if payload is None:
        return ConnectorCredentialSpec()
    block = _object(payload, "credential")
    _known_keys(block, _CREDENTIAL_KEYS, "credential")
    kind = block.get("kind", "none")
    if kind not in {"oauth2", "api_key", "none"}:
        raise ConnectorPackageError(
            f"credential.kind must be oauth2, api_key or none, not {kind!r}"
        )
    scopes_raw: object = block.get("scopes") or {}
    scopes_block = _object(scopes_raw, "credential.scopes")
    scopes: dict[str, tuple[str, ...]] = {}
    for capability_class, value in cast(dict[str, object], scopes_block).items():
        if capability_class not in CAPABILITY_CLASSES:
            raise ConnectorPackageError(
                f"credential.scopes names {capability_class!r}, and the capability classes are "
                f"{sorted(CAPABILITY_CLASSES)}"
            )
        scopes[capability_class] = _string_tuple(
            value, f"credential.scopes.{capability_class}"
        )
    allowed_hosts = _string_tuple(block.get("allowedHosts"), "credential.allowedHosts")
    if kind != "none" and not allowed_hosts:
        raise ConnectorPackageError(
            "credential.allowedHosts must name the hosts this package may address. A package "
            "that holds a token and names no hosts is a package that can send it anywhere."
        )
    return ConnectorCredentialSpec(
        kind=cast(Any, kind),
        scopes=scopes,
        token_url=_text(block, "tokenUrl", "credential", required=False),
        allowed_hosts=allowed_hosts,
    )


def _operation(payload: object, index: int) -> ConnectorOperation:
    where = f"operations[{index}]"
    block = _object(payload, where)
    _known_keys(block, _OPERATION_KEYS, where)
    capability_class = _text(block, "class", where)
    if capability_class not in CAPABILITY_CLASSES:
        raise ConnectorPackageError(
            f"{where} declares class {capability_class!r}, and the capability classes are "
            f"{sorted(CAPABILITY_CLASSES)}"
        )
    parameters_raw = block.get("parameters")
    parameters: dict[str, Any] = (
        _object(parameters_raw, f"{where}.parameters") if parameters_raw is not None else {}
    )
    # `ConnectorOperation.__post_init__` re-checks the path, the method and the read-class rule.
    # Repeating them here would be a second copy of the invariant that could drift -- so the
    # contract raises and this reports it in the type a caller of this module catches.
    try:
        return ConnectorOperation(
            name=_text(block, "name", where),
            capability_class=capability_class,
            method=cast(Any, _text(block, "method", where).upper()),
            path=_text(block, "path", where),
            description=_text(block, "summary", where, required=False),
            returns=_string_tuple(block.get("returns"), f"{where}.returns"),
            collection=_text(block, "collection", where, required=False),
            parameters=parameters,
        )
    except (ValueError, TypeError) as exc:
        raise ConnectorPackageError(f"{where}: {exc}") from exc


def _setup(payload: object) -> ConnectorSetupSpec | None:
    """The operator-facing settings, as the same `ChannelFieldSpec` a channel declares.

    A connector's form is rendered by the code that renders a channel's, so the package's JSON is
    translated into that shape here rather than a second one being introduced for packages.
    """
    if payload is None:
        return None
    block = _object(payload, "setup")
    _known_keys(block, _SETUP_KEYS, "setup")
    fields_raw: object = block.get("fields") or []
    if not isinstance(fields_raw, list):
        raise ConnectorPackageError("setup.fields must be a list")
    fields_list = cast(list[object], fields_raw)
    if len(fields_list) > MAX_SETUP_FIELDS:
        raise ConnectorPackageError(
            f"setup.fields holds {len(fields_list)} entries, above the {MAX_SETUP_FIELDS} cap"
        )
    fields: dict[str, ChannelFieldSpec] = {}
    required: list[SetupRequirement] = []
    for index, raw in enumerate(fields_list):
        where = f"setup.fields[{index}]"
        field_block = _object(raw, where)
        _known_keys(field_block, _SETUP_FIELD_KEYS, where)
        name = _text(field_block, "name", where)
        kind: object = field_block.get("kind", "string")
        if kind not in {"string", "bool", "int"}:
            raise ConnectorPackageError(
                f"{where}.kind must be string, bool or int, not {kind!r}. A package does not get "
                f"to introduce a widget the settings form has never rendered."
            )
        default: object = field_block.get("default")
        if default is not None and not isinstance(default, (str, int, bool)):
            raise ConnectorPackageError(f"{where}.default must be a string, number or boolean")
        fields[name] = ChannelFieldSpec(kind=cast(Any, kind), default=default)
        if bool(field_block.get("required", False)):
            required.append(SetupRequirement.field(name))
    return ConnectorSetupSpec(
        fields=fields,
        required=tuple(required),
        official_url=_text(block, "officialUrl", "setup", required=False),
    )


def _mentions(payload: object) -> tuple[ConnectorMentionSpec, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise ConnectorPackageError("mentions must be a list")
    items = cast(list[object], payload)
    if len(items) > MAX_MENTIONS:
        raise ConnectorPackageError(f"mentions holds {len(items)} entries, above the {MAX_MENTIONS} cap")
    specs: list[ConnectorMentionSpec] = []
    for index, raw in enumerate(items):
        where = f"mentions[{index}]"
        block = _object(raw, where)
        _known_keys(block, _MENTION_KEYS, where)
        specs.append(
            ConnectorMentionSpec(
                kind=_text(block, "kind", where),
                operation=_text(block, "operation", where),
                id_field=_text(block, "idField", where, required=False) or "id",
                label_field=_text(block, "labelField", where, required=False) or "name",
                detail_fields=_string_tuple(block.get("detailFields"), f"{where}.detailFields"),
                argument=_text(block, "argument", where, required=False),
            )
        )
    return tuple(specs)


def parse_connector_package(payload: object, *, expected_name: str = "") -> ConnectorPlugin:
    """Validate a decoded `connector.json` and build the plugin it describes.

    Raises `ConnectorPackageError` with the reason. Every refusal names the field, because the
    person reading it is deciding whether to fix a package or to distrust it.
    """
    block = _object(payload, PACKAGE_FILE)
    _known_keys(block, _TOP_LEVEL_KEYS, PACKAGE_FILE)

    schema = _text(block, "$schema", PACKAGE_FILE)
    if schema != PACKAGE_SCHEMA:
        raise ConnectorPackageError(
            f"{PACKAGE_FILE} must declare $schema {PACKAGE_SCHEMA!r}, not {schema!r}"
        )

    name = _text(block, "name", PACKAGE_FILE)
    if expected_name and name != expected_name:
        raise ConnectorPackageError(
            f"{PACKAGE_FILE} names {name!r} and the install asked for {expected_name!r}"
        )

    dependencies = _string_tuple(block.get("dependencies"), "dependencies")
    if dependencies:
        raise ConnectorPackageError(
            f"{PACKAGE_FILE} declares dependencies {list(dependencies)}, and a declarative "
            f"package runs no code that could use them. Refused rather than dropped: a package "
            f"whose dependencies were silently ignored would fail at its first call instead of "
            f"here, where somebody is reading."
        )

    operations_raw: object = block.get("operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        raise ConnectorPackageError(f"{PACKAGE_FILE} must declare a non-empty operations list")
    operations_list = cast(list[object], operations_raw)
    if len(operations_list) > MAX_OPERATIONS:
        raise ConnectorPackageError(
            f"{PACKAGE_FILE} declares {len(operations_list)} operations, above the "
            f"{MAX_OPERATIONS} cap"
        )
    operations = tuple(
        _operation(raw, index) for index, raw in enumerate(operations_list)
    )

    credential = _credential(block.get("credential"))
    base_url = _text(block, "baseUrl", PACKAGE_FILE)
    host = urlsplit(base_url).hostname or ""
    if credential.allowed_hosts and not credential.permits_host(host):
        raise ConnectorPackageError(
            f"{PACKAGE_FILE} declares baseUrl host {host!r} and allowedHosts "
            f"{sorted(credential.allowed_hosts)}. A package that contradicts its own declaration "
            f"is refused rather than reconciled."
        )

    try:
        return ConnectorPlugin(
            name=name,
            display_name=_text(block, "displayName", PACKAGE_FILE, required=False) or name,
            base_url=base_url,
            operations=operations,
            credential=credential,
            setup=_setup(block.get("setup")),
            dependencies=(),
            skill=_text(block, "skill", PACKAGE_FILE, required=False) or None,
            description=_text(block, "description", PACKAGE_FILE, required=False),
            mentions=_mentions(block.get("mentions")),
        )
    except (ValueError, TypeError) as exc:
        # The contract's own invariants -- an https base_url, a path starting with `/`, no read
        # class on a writing method -- reported in this module's own error type.
        raise ConnectorPackageError(str(exc)) from exc


def load_connector_package(directory: Path, *, expected_name: str = "") -> ConnectorPlugin:
    """Read and validate `<directory>/connector.json`, refusing anything importable beside it."""
    path = directory / PACKAGE_FILE
    try:
        exists = path.is_file()
    except OSError as exc:
        # `is_file()` propagates a permission error, and a package this process cannot read is a
        # refusal with a reason rather than a traceback out of a caller that expected one.
        raise ConnectorPackageError(f"{directory.name} could not be read: {exc}") from exc
    if not exists:
        raise ConnectorPackageError(f"{directory.name} holds no {PACKAGE_FILE}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConnectorPackageError(f"{path} could not be read: {exc}") from exc
    if size > MAX_PACKAGE_BYTES:
        raise ConnectorPackageError(
            f"{PACKAGE_FILE} is {size} bytes, above the {MAX_PACKAGE_BYTES} cap"
        )
    refuse_executable_files(directory)
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ConnectorPackageError(f"{PACKAGE_FILE} is not valid JSON: {exc}") from exc
    return parse_connector_package(payload, expected_name=expected_name or directory.name)


def refuse_executable_files(directory: Path) -> None:
    """Refuse a package that ships something importable.

    Checked at load as well as at install, because a package directory is a directory: an install
    that validated once says nothing about what is there the next time the gateway starts.
    """
    try:
        entries = sorted(directory.rglob("*"))
    except OSError as exc:
        raise ConnectorPackageError(f"{directory.name} could not be read: {exc}") from exc
    for entry in entries:
        if entry.is_dir():
            continue
        if entry.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ConnectorPackageError(
                f"{directory.name} holds {entry.relative_to(directory)}, and this package format "
                f"runs no code. A connector that needs a runtime is not a declarative package."
            )


__all__ = [
    "ConnectorPackageError",
    "FORBIDDEN_SUFFIXES",
    "MAX_OPERATIONS",
    "MAX_PACKAGE_BYTES",
    "PACKAGE_FILE",
    "PACKAGE_SCHEMA",
    "load_connector_package",
    "parse_connector_package",
    "refuse_executable_files",
]
