"""Turn config plus manifests into the connectors that are actually active.

Every refusal in this module happens **at activation**, not at call time, and each one names
both halves of the mismatch. The failure being designed out is the one an operator meets at
03:00: a connector that read fine all week and refused a write in a run record, because a
scope was never granted or a ceiling was set below the class the operation needs.

So a connector with a problem is not activated, the problem is logged with the key that
fixes it, and the rest of the deployment comes up. One misconfigured connector must not stop
the others, and it must not half-work either.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.connectors.contracts import ConnectorOperation, ConnectorPlugin
from nanoinfra.connectors.credentials import (
    ConnectorCredential,
    CredentialError,
    check_connector_hosts,
    check_connector_scopes,
)
from nanoinfra.connectors.registry import (
    capped_operations,
    discover_connectors,
    enabled_operations,
)


@dataclass(frozen=True, slots=True)
class ActiveConnector:
    """One connector this deployment activated, with everything a call needs."""

    plugin: ConnectorPlugin
    operations: tuple[ConnectorOperation, ...]
    credential: ConnectorCredential
    defaults: dict[str, Any] = dataclass_field(default_factory=dict[str, Any])

    @property
    def name(self) -> str:
        return self.plugin.name


@dataclass(frozen=True, slots=True)
class ActivationProblem:
    """A connector that was asked for and did not activate, and the key that fixes it."""

    connector: str
    reason: str

    def __str__(self) -> str:
        return f"{self.connector}: {self.reason}"


def _credential_for(
    name: str, cfg: ConnectorRuntimeConfig, plugin: ConnectorPlugin
) -> ConnectorCredential:
    connector_cfg = cfg.connectors.get(name)
    ref = connector_cfg.credential if connector_cfg else ""
    if plugin.credential.kind == "none":
        # A connector against a public API holds no credential, so there is nothing to bind and
        # nothing to mint. Refusing one would have made the simplest connector -- the one somebody
        # writes first, to see the shape -- the only one that could not run.
        #
        # A credential named anyway is still honoured: an operator who put one there meant it, and
        # a package declaring `none` does not get to decide that a deployment sends no token.
        if not ref:
            return ConnectorCredential(name="", client_id="", secret_ref="", token_url="")
    if not ref:
        raise CredentialError(
            f"connectors.{name} names no credential. Set connectors.{name}.credential to one "
            f"of credentials.* -- the binding is the grant, so a connector with none resolves "
            "nothing."
        )
    credential_cfg = cfg.credentials.get(ref)
    if credential_cfg is None:
        raise CredentialError(
            f"connectors.{name}.credential is {ref!r} and credentials holds "
            f"{sorted(cfg.credentials) or 'nothing'}."
        )
    if not credential_cfg.client_id or not credential_cfg.secret_ref:
        raise CredentialError(
            f"credentials.{ref} needs a clientId and a secretRef naming the refresh token."
        )
    return ConnectorCredential(
        name=ref,
        client_id=credential_cfg.client_id,
        secret_ref=credential_cfg.secret_ref,
        client_secret_ref=credential_cfg.client_secret_ref,
        token_url=credential_cfg.token_url or plugin.credential.token_url,
        scopes=tuple(credential_cfg.scopes),
        # Config's list, or the manifest's own when config names none: a first-party package
        # reviewed in this repository may state its reach, and an operator may narrow it.
        allowed_hosts=(
            tuple(credential_cfg.allowed_hosts)
            or plugin.credential.allowed_hosts
        ),
    )


def _operations_for(
    name: str, cfg: ConnectorRuntimeConfig, plugin: ConnectorPlugin
) -> tuple[ConnectorOperation, ...]:
    connector_cfg = cfg.connectors.get(name)
    wanted = connector_cfg.enabled_operations if connector_cfg else None
    if wanted is not None:
        known = {op.name for op in plugin.operations}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise CredentialError(
                f"connectors.{name}.enabledOperations names {unknown}, which this connector "
                f"does not declare. It declares {sorted(known)}."
            )
    chosen = enabled_operations(plugin, wanted)
    ceiling = connector_cfg.max_class if connector_cfg else None
    capped = capped_operations(chosen, ceiling)
    if ceiling and len(capped) != len(chosen):
        dropped = sorted({op.name for op in chosen} - {op.name for op in capped})
        logger.info(
            "connector '{}' is capped at {}, so {} stay unavailable",
            name,
            ceiling,
            dropped,
        )
    if not capped:
        raise CredentialError(
            f"connectors.{name} activates no operation: enabledOperations and maxClass "
            "together leave nothing to call."
        )
    return capped


def _defaults_for(plugin: ConnectorPlugin, settings: dict[str, str]) -> dict[str, Any]:
    """What every call of this connector starts with.

    A manifest field carries a default, and that default is part of the contract: Calendar
    declares ``calendarId: "primary"``, so a deployment that says nothing about it means the
    primary calendar. Reading only the operator's settings made a declared default a value
    config had to repeat, and a path placeholder then had no value at all -- the first call
    failed with "needs 'calendarId'" for a field the package had already answered.

    The operator's settings win, because config is the authority over a package.
    """
    declared: dict[str, Any] = {}
    if plugin.setup is not None:
        for name, spec in plugin.setup.fields.items():
            if spec.default not in (None, ""):
                declared[name] = spec.default
    declared.update(settings)
    return declared


def resolve_active(
    cfg: ConnectorRuntimeConfig,
    workspace_path: Path | None = None,
) -> tuple[list[ActiveConnector], list[ActivationProblem]]:
    """The connectors that activate, and the ones that did not with the reason.

    Nothing here reads a secret or opens a socket, so this is safe to call at startup and
    safe to call again from a settings route that wants to show an operator what is wrong.

    ``workspace_path`` adds the marketplace packages installed under it (#195). Omitted, only the
    packages bundled in this repository are considered -- which is what a caller with no workspace
    in hand should see, rather than a partial answer.
    """
    installed = discover_connectors(workspace_path)
    active: list[ActiveConnector] = []
    problems: list[ActivationProblem] = []

    for name in cfg.active:
        plugin = installed.get(name)
        if plugin is None:
            problems.append(
                ActivationProblem(
                    name,
                    f"no connector package is installed under that name. Installed: "
                    f"{sorted(installed) or 'none'}.",
                )
            )
            continue
        try:
            credential = _credential_for(name, cfg, plugin)
            operations = _operations_for(name, cfg, plugin)
            # Every class the connector still offers has to be servable by the credential.
            # Checked against the operations that survived the ceiling, so a connector capped
            # at `read` activates on a read-only credential.
            offered = ConnectorPlugin(
                name=plugin.name,
                display_name=plugin.display_name,
                base_url=plugin.base_url,
                operations=operations,
                credential=plugin.credential,
                setup=plugin.setup,
                dependencies=plugin.dependencies,
                webui=plugin.webui,
                skill=plugin.skill,
                description=plugin.description,
            )
            check_connector_scopes(offered, credential)
            # Where the token may go, checked before the first call rather than during one.
            check_connector_hosts(offered, credential)
        except CredentialError as exc:
            problems.append(ActivationProblem(name, str(exc)))
            continue

        connector_cfg = cfg.connectors.get(name)
        active.append(
            ActiveConnector(
                plugin=plugin,
                operations=operations,
                credential=credential,
                defaults=_defaults_for(
                    plugin, dict(connector_cfg.settings) if connector_cfg else {}
                ),
            )
        )
    return active, problems


def startup_summary(
    active: list[ActiveConnector], problems: list[ActivationProblem]
) -> str:
    """One line at boot, in the shape ``gates/startup.py`` uses.

    A connector that did not activate is louder than one that did, because the operator asked
    for it and it is not there.
    """
    if not active and not problems:
        return "connectors: none active"
    parts: list[str] = []
    for entry in active:
        # Manifest order, not alphabetical: a row that reads "read/mutate.remote" says what
        # the connector mostly does first, and "mutate.remote/read" reads like a warning.
        seen: list[str] = []
        for op in entry.operations:
            if op.capability_class not in seen:
                seen.append(op.capability_class)
        parts.append(f"{entry.name} ({len(entry.operations)} ops, {'/'.join(seen)})")
    text = f"connectors: {', '.join(parts) or 'none active'}"
    if problems:
        text += f". Not activated: {'; '.join(str(problem) for problem in problems)}"
    return text


__all__ = [
    "ActivationProblem",
    "ActiveConnector",
    "resolve_active",
    "startup_summary",
]
