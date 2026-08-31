"""Discover connector packages, and turn their operations into tools.

Two jobs, both mirroring what channels already do. `nanoinfra/channels/registry.py`
scans `nanoinfra.channels` with `pkgutil.iter_modules` and loads each package's
`manifest.PLUGIN` without importing its runtime; this does the same for
`nanoinfra.connectors`, so listing what is installed costs no optional SDK import.

The second job has no channel equivalent: **one tool per operation**, each carrying
the class its manifest declared. That is the whole reason connectors exist as a kind
— `capability_class_of()` reads a class off the tool, so a single tool with a
`operation=` argument would have one class for a read and a write both, which means
`mutate.remote` for the read as well. That is the MCP behaviour, and escaping it is
the point.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.connectors.contracts import ConnectorOperation, ConnectorPlugin

_MANIFEST_ATTR = "PLUGIN"


def connector_package_names() -> list[str]:
    """Every connector package that ships in the tree, in import order."""
    import nanoinfra.connectors as package

    return sorted(
        name
        for _, name, is_package in pkgutil.iter_modules(package.__path__)
        if is_package
    )


def load_connector_package(name: str) -> ConnectorPlugin | None:
    """Load one package's manifest, or None when it has none.

    A package whose manifest raises is logged and skipped rather than taking the
    gateway down: one bad connector must not stop the others, and the validation in
    `contracts.py` is what turns a bad manifest into a message an operator can read.
    """
    module_name = f"nanoinfra.connectors.{name}.manifest"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None
    except Exception as exc:  # a manifest that refuses to load names itself
        logger.warning("connector '{}' manifest failed to load: {}", name, exc)
        return None

    plugin = getattr(module, _MANIFEST_ATTR, None)
    if not isinstance(plugin, ConnectorPlugin):
        logger.warning("connector '{}' has no {} of the right type", name, _MANIFEST_ATTR)
        return None
    # The directory name is the identity an operator writes in config, so a manifest
    # that disagrees with it would be configured under a name that resolves to
    # something else.
    expected = name.replace("_", "-")
    if plugin.name != expected:
        logger.warning(
            "connector package '{}' declares name '{}'; expected '{}'",
            name,
            plugin.name,
            expected,
        )
        return None
    return plugin


#: Where a marketplace package is written (#195, part 2).
#:
#: **Not** `<workspace>/connectors/`, which is already `state.py`'s directory: that one is
#: `drwxrwx---` owned by the agent, so the executor cannot even traverse it -- and pointing package
#: discovery at it broke the *first-party* Google Calendar connector on a live deployment with a
#: `PermissionError`, before any marketplace package existed anywhere. A package has to be readable
#: by three accounts (the agent lists, the executor routes, the confined host calls) and a state
#: file has to be writable by one. Those are different directories.
CONNECTOR_PACKAGE_DIR = "connector-packages"


def workspace_connector_root(workspace_path: Path) -> Path:
    """Where a marketplace package is written (#195, part 2).

    Under the workspace rather than beside the bundled packages, for the same reason a marketplace
    skill goes there: the tree is what this repository reviewed, and a deployment's own installs
    are the deployment's.
    """
    return Path(workspace_path) / CONNECTOR_PACKAGE_DIR


def discover_connectors(workspace_path: Path | None = None) -> dict[str, ConnectorPlugin]:
    """Every installed connector, by the name config uses.

    Two roots. The bundled packages are Python manifests reviewed in this repository; a workspace
    package is `connector.json`, validated on every load rather than trusted from install time,
    because a directory is a directory and nothing stops it changing between two gateway starts.

    A bundled package wins a name collision. A marketplace package that could shadow
    `google-calendar` would be a package that redefines what an operator already reviewed.
    """
    found: dict[str, ConnectorPlugin] = {}
    if workspace_path is not None:
        for plugin in _workspace_connectors(workspace_connector_root(workspace_path)):
            found[plugin.name] = plugin
    for package in connector_package_names():
        plugin = load_connector_package(package)
        if plugin is not None:
            if plugin.name in found:
                logger.warning(
                    "connector '{}' is installed in the workspace and bundled in the tree; "
                    "the bundled package wins",
                    plugin.name,
                )
            found[plugin.name] = plugin
    return found


def is_marketplace_connector(name: str, workspace_path: Path | None) -> bool:
    """Whether *name* is a package installed under the workspace rather than bundled here (#195).

    The origin decides the route: a bundled manifest is reviewed in this repository and the executor
    performs its call; a marketplace package's call goes through the confined host. Checked by
    looking for the directory rather than by remembering the install, because a directory is a
    directory and nothing stops it appearing between two gateway starts.
    """
    if workspace_path is None:
        return False
    try:
        return (workspace_connector_root(workspace_path) / name / "connector.json").is_file()
    except OSError as exc:
        # `Path.is_file()` propagates a permission error rather than answering False, and this
        # question has a safe answer when it cannot be answered: a package this process cannot see
        # is not one to route through the host. Logged, because a package root the executor cannot
        # read is a deployment fault worth noticing rather than swallowing.
        logger.warning("could not check for a marketplace package of '{}': {}", name, exc)
        return False


def _workspace_connectors(root: Path) -> list[ConnectorPlugin]:
    """Load every declarative package under *root*, skipping the ones that refuse to validate."""
    try:
        if not root.is_dir():
            return []
        entries = sorted(root.iterdir())
    except OSError as exc:
        # An unreadable package root must not take the bundled connectors down with it.
        logger.warning("could not list connector packages in {}: {}", root, exc)
        return []
    from nanoinfra.connectors.package import ConnectorPackageError
    from nanoinfra.connectors.package import load_connector_package as _load

    plugins: list[ConnectorPlugin] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            plugins.append(_load(entry, expected_name=entry.name))
        except ConnectorPackageError as exc:
            # Skipped rather than fatal, and named: one refused package must not stop the others,
            # and an operator who installed it is the person who needs to read this.
            logger.warning("connector package '{}' refused: {}", entry.name, exc)
    return plugins


def enabled_operations(
    plugin: ConnectorPlugin, allowed: list[str] | None
) -> tuple[ConnectorOperation, ...]:
    """The operations a deployment exposes to the model.

    `None` means every operation. A list means exactly those — the same answer MCP
    gives with `enabledTools`, and for the same reason: a deployment that only reads
    a calendar should put two tools in the context window, not nine.
    """
    if allowed is None:
        return plugin.operations
    wanted = set(allowed)
    return tuple(op for op in plugin.operations if op.name in wanted)


def capped_operations(
    operations: tuple[ConnectorOperation, ...], max_class: str | None
) -> tuple[ConnectorOperation, ...]:
    """Drop operations above the ceiling a deployment set.

    A package declares its own classes, and a package that came from a marketplace
    declaring "this call is a read" is self-certification. `maxClass` is the
    operator's answer, in git-reviewed config: a connector capped at `read` offers no
    writes however its manifest is written.
    """
    if not max_class:
        return operations
    if max_class == "read":
        return tuple(op for op in operations if op.is_read)
    return operations


def operation_summary(plugin: ConnectorPlugin) -> list[dict[str, Any]]:
    """What the Apps row shows: each operation, its class and its method."""
    return [
        {
            "name": op.name,
            "tool": plugin.tool_name(op),
            "capability_class": op.capability_class,
            "method": op.method,
            "returns": list(op.returns),
        }
        for op in plugin.operations
    ]


__all__ = [
    "capped_operations",
    "connector_package_names",
    "discover_connectors",
    "enabled_operations",
    "load_connector_package",
    "operation_summary",
]
