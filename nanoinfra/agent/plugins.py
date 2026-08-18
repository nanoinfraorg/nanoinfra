"""Load and activate locally installed Agent Plugin packages.

Agent Plugins (https://agent-plugins.org/) v1.0.0 is the package format: a directory with
``plugin.json``, optionally ``skills/<name>/SKILL.md`` and ``mcp.json``. It is the one artifact
that carries skills and MCP servers together behind a single activation boundary.

This module is a deliberate near-copy of upstream's ``nanobot/agent/plugins.py``, because that is
where upstream's future fixes land and a diffable file makes them cherry-picks. Two things differ,
both for reasons this fork has and upstream does not:

**Where activation lives.** Upstream keeps the ``enabled`` marker beside the plugin's runtime data
under the config directory. That is sound for upstream, which runs everything as one account. Here
the agent account owns the data directory (``entrypoint.sh``), so a marker there would let the agent
forge its own activation and grant itself a new stdio process. The marker therefore lives under the
executor-owned gate tree, which is already ``exec_user:ipc_group`` at mode 2750: the executor
writes, the shared group reads. Skill loading runs in the agent process and only ever reads.
``PLUGIN_DATA`` stays where upstream puts it -- it is the plugin's own scratch space and carries no
authority.

**Which extension namespace.** Reverse-domain namespaces exist in the spec so a client can add its
own fields without touching the portable core, so ours is ``dev.nanoinfra`` rather than
``dev.nanobot``. The portable core -- identity, description, repository, skills, MCP -- is
untouched, which is exactly the portability the namespacing is for.

Activation is bound to package content, not to a path. Any change to an enabled package invalidates
its marker on the next read, so a package that mutates after review stops being active until it is
reviewed again.
"""

from __future__ import annotations

import base64
import json
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import cast

from loguru import logger
from pydantic import ValidationError

from nanoinfra.agent.skills import parse_skill_metadata, valid_skill_metadata
from nanoinfra.config.loader import get_config_path
from nanoinfra.config.paths import get_data_dir
from nanoinfra.config.schema import MCPServerConfig

AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_PLUGIN_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

# Our client extension namespace. See the module docstring.
NANOINFRA_EXTENSION = "dev.nanoinfra"

# No `--` and no `..`: the first keeps a multi-server namespace (`<plugin>--<server>`) from
# colliding with a single-server plugin name, the second keeps an identity from being a traversal.
_PLUGIN_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_MCP_SERVER_FIELDS = {"type", "command", "args", "env", "cwd"}
_MAX_LOGO_BYTES = 256 * 1024

# The activation marker's home, under the executor-owned gate tree rather than the agent-owned
# data directory. See the module docstring.
_ACTIVATION_SUBDIR = "plugin-activation"
# Executor writes, shared group reads. Matches how entrypoint.sh already hands over the audit log,
# setgid included so anything created deeper keeps the shared group rather than the creator's.
_ACTIVATION_DIR_MODE = 0o2750
_ACTIVATION_FILE_MODE = 0o640


@dataclass(frozen=True, slots=True)
class _PackageSnapshot:
    root: Path
    fingerprint: str
    skill_dirs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _SkillCacheEntry:
    skills: tuple[tuple[str, Path], ...]
    packages: tuple[_PackageSnapshot, ...]


_SKILL_CACHE: dict[tuple[Path, Path], _SkillCacheEntry] = {}


@dataclass(frozen=True)
class AgentPlugin:
    """A validated, locally installed Agent Plugins v1 package."""

    name: str
    root: Path
    description: str
    repository: str
    version: str
    display_name: str
    category: str
    accent_color: str | None
    logo: str | None
    permissions: tuple[str, ...]
    mcp_servers: tuple[str, ...] = ()
    enabled: bool = False


def _installed_plugins(workspace: Path) -> list[AgentPlugin]:
    """Return installed packages found under ``<workspace>/plugins/*``."""
    workspace = workspace.expanduser().resolve()
    root = _contained(workspace / "plugins", workspace, directory=True)
    if root is None:
        return []
    plugins: dict[str, AgentPlugin | None] = {}
    for candidate in _children(root, "Agent Plugins directory"):
        plugin_root = _contained(candidate, root, directory=True)
        if plugin_root is None:
            continue
        plugin = _load_manifest(plugin_root)
        if plugin is not None:
            if plugin.name in plugins:
                # Two packages claiming one identity is ambiguous, so neither is used.
                logger.warning("Ignoring duplicate Agent Plugin identity '{}'", plugin.name)
                plugins[plugin.name] = None
            else:
                plugins[plugin.name] = plugin
    return [plugin for plugin in plugins.values() if plugin is not None]


def enabled_agent_plugin_skills(workspace: Path) -> list[tuple[str, Path]]:
    """Verify and return skills from plugins the user has explicitly enabled."""
    skills: list[tuple[str, Path]] = []
    packages: list[_PackageSnapshot] = []
    for plugin in _installed_plugins(workspace):
        plugin_skills = _discover_plugin_skills(plugin.name, plugin.root)
        fingerprint = _enabled_package_fingerprint(workspace, plugin)
        if fingerprint is None:
            continue
        skills.extend(plugin_skills)
        if plugin_skills:
            packages.append(
                _PackageSnapshot(
                    root=plugin.root,
                    fingerprint=fingerprint,
                    skill_dirs=tuple(path.parent for _name, path in plugin_skills),
                )
            )

    key = _skill_cache_key(workspace)
    _SKILL_CACHE[key] = _SkillCacheEntry(tuple(skills), tuple(packages))
    return skills


def enabled_agent_plugin_skill_dirs(
    workspace: Path,
    *,
    requested_path: str | Path | None = None,
) -> tuple[Path, ...]:
    """Return skill roots authorized for one read, revalidating their package."""
    key = _skill_cache_key(workspace)
    cached = _SKILL_CACHE.get(key)
    if cached is None:
        enabled_agent_plugin_skills(workspace)
        cached = _SKILL_CACHE.get(key)
    if cached is None:
        return ()

    target = (
        Path(requested_path).expanduser().resolve(strict=False)
        if requested_path is not None
        else None
    )
    packages = tuple(
        package
        for package in cached.packages
        if target is None
        or any(target == root or target.is_relative_to(root) for root in package.skill_dirs)
    )
    if any(_package_fingerprint(package.root) != package.fingerprint for package in packages):
        # Re-run the full activation check so a changed package loses its marker and cannot
        # become readable again through this cache.
        _invalidate_skill_cache(workspace)
        enabled_agent_plugin_skills(workspace)
        return ()

    if target is None:
        return tuple(root for package in packages for root in package.skill_dirs)
    return tuple(
        root
        for package in packages
        for root in package.skill_dirs
        if target == root or target.is_relative_to(root)
    )


def _skill_cache_key(workspace: Path) -> tuple[Path, Path]:
    return (
        workspace.expanduser().resolve(),
        get_config_path().expanduser().resolve(),
    )


def _invalidate_skill_cache(workspace: Path) -> None:
    _SKILL_CACHE.pop(_skill_cache_key(workspace), None)


def _package_fingerprint(root: Path) -> str | None:
    """Hash package paths, link targets, and file contents."""
    digest = sha256()
    try:
        for candidate in sorted(root.rglob("*")):
            relative = candidate.relative_to(root).as_posix()
            digest.update(relative.encode())
            if candidate.is_symlink():
                # The link target is part of the identity: repointing a link changes the package
                # without changing any file it contains.
                digest.update(b"\0link\0")
                digest.update(candidate.readlink().as_posix().encode())
            elif candidate.is_file():
                digest.update(b"\0file\0")
                digest.update(candidate.read_bytes())
            elif candidate.is_dir():
                digest.update(b"\0dir\0")
            else:
                # A socket or device cannot be fingerprinted, so the package cannot be trusted.
                return None
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def _load_manifest(plugin_root: Path) -> AgentPlugin | None:
    payload = _read_object(plugin_root / "plugin.json", plugin_root)
    if payload is None:
        return None
    if payload.get("$schema") != AGENT_PLUGIN_SCHEMA:
        return None
    name = payload.get("name")
    if (
        not isinstance(name, str)
        or len(name) > 64
        or _PLUGIN_NAME.fullmatch(name) is None
    ):
        logger.warning("Ignoring Agent Plugin manifest in '{}': invalid name", plugin_root)
        return None
    extension = payload.get("extensions")
    extension_payload = cast(dict[str, object], extension) if isinstance(extension, dict) else {}
    ours_value = extension_payload.get(NANOINFRA_EXTENSION)
    ours = cast(dict[str, object], ours_value) if isinstance(ours_value, dict) else {}
    return AgentPlugin(
        name=name,
        root=plugin_root,
        description=_string(payload.get("description")),
        repository=_string(payload.get("repository")),
        version=_string(payload.get("version")),
        display_name=_string(ours.get("displayName")) or name,
        category=_string(ours.get("category")) or "Plugin",
        accent_color=_accent_color(ours.get("accentColor")),
        logo=_plugin_logo(ours.get("logo"), plugin_root),
        permissions=_string_tuple(ours.get("permissions")),
    )


def agent_plugin_mcp_servers(
    workspace: Path,
    configured: dict[str, MCPServerConfig] | None = None,
) -> dict[str, MCPServerConfig]:
    """Merge explicitly enabled plugin MCP servers with user configuration.

    User configuration wins on the unlikely event of a namespaced collision.
    """
    servers: dict[str, MCPServerConfig] = {}
    for plugin in _installed_plugins(workspace):
        if not _enabled(workspace, plugin):
            continue
        plugin_servers = _plugin_mcp_servers(workspace, plugin)
        for name, server in plugin_servers.items():
            # ``--`` cannot occur in a valid plugin identity, so multi-server namespaces cannot
            # collide with a single-server plugin name.
            host_name = plugin.name if len(plugin_servers) == 1 else f"{plugin.name}--{name}"
            servers[host_name] = server
    configured = configured or {}
    if collisions := servers.keys() & configured.keys():
        logger.warning(
            "Configured MCP servers override Agent Plugins: {}", ", ".join(sorted(collisions))
        )
    return servers | configured


def merged_mcp_servers(config: object) -> dict[str, MCPServerConfig]:
    """Return a config's MCP servers plus those of enabled plugins.

    Three call sites need this same view and must agree: the gateway decides whether to start the
    mcp-host at all, the mcp-host resolves what to launch, and the agent's tool registry lists what
    exists. One of them disagreeing means either a server that never starts or a tool that cannot
    connect, so the merge lives here rather than being written out three times.

    A plugin tree that cannot be read costs the operator nothing: their own servers are returned.
    """
    tools = getattr(config, "tools", None)
    configured = dict(getattr(tools, "mcp_servers", {}) or {})
    try:
        return agent_plugin_mcp_servers(workspace_from_config(config), configured)
    except (OSError, RuntimeError) as exc:
        logger.warning("Ignoring Agent Plugin MCP servers: {}", exc)
        return configured


def workspace_from_config(config: object) -> Path:
    """Resolve the agent workspace from a config object, tolerating a partial one.

    Callers include the gateway, the mcp-host, and the agent's tool layer, and tests hand each of
    them a stand-in namespace rather than a full Config. Reaching through the attributes directly
    would turn a partial stand-in into an AttributeError in production code paths that only needed
    a default.
    """
    from nanoinfra.config.paths import get_workspace_path

    agents = getattr(config, "agents", None)
    return get_workspace_path(getattr(getattr(agents, "defaults", None), "workspace", None))


def discover_agent_plugins(workspace: Path) -> list[AgentPlugin]:
    """Return component and lifecycle state for discovered plugins."""
    return [
        replace(
            plugin,
            mcp_servers=tuple(sorted(_plugin_mcp_servers(workspace, plugin))),
            enabled=_enabled(workspace, plugin),
        )
        for plugin in _installed_plugins(workspace)
    ]


def set_agent_plugin_enabled(workspace: Path, name: str, enabled: bool) -> None:
    """Enable or disable one installed plugin.

    Writing the marker requires the executor account wherever the split is kernel-enforced, so
    this is the primitive an approved activation calls and never something the agent reaches.
    """
    plugin = next((item for item in _installed_plugins(workspace) if item.name == name), None)
    if plugin is None:
        raise ValueError(f"unknown Agent Plugin '{name}'")
    marker = _plugin_activation_dir(workspace, plugin.name, create=True) / "enabled"
    if enabled:
        activation = _activation_marker(plugin)
        if activation is None:
            raise RuntimeError(f"Agent Plugin '{name}' changed while it was being enabled")
        marker.write_text(activation, encoding="utf-8")
        marker.chmod(_ACTIVATION_FILE_MODE)
    else:
        marker.unlink(missing_ok=True)
    _invalidate_skill_cache(workspace)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one reconcile changed, for the operator's log."""

    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def reconcile_agent_plugins(workspace: Path, declared: list[str]) -> ReconcileResult:
    """Make on-disk activation match ``tools.agentPlugins``.

    Config is the authority. Enabling a package that ships an ``mcp.json`` grants a new stdio
    process, so that decision belongs in a file a human reviews, not in a marker directory or a
    UI toggle. This walks the installed packages once and moves each one to the state config asks
    for.

    Re-enabling a listed package re-binds its fingerprint, which is the point: config naming an
    identity is the reviewed moment. Between reconciles the content binding still holds, so a
    package that mutates while the gateway runs loses its marker on the next read.

    A name no package provides is reported rather than raised. A typo in config must be visible
    without taking the gateway down with it.
    """
    wanted = dict.fromkeys(name.strip() for name in declared if name.strip())
    installed = {plugin.name: plugin for plugin in _installed_plugins(workspace)}
    enabled: list[str] = []
    disabled: list[str] = []
    failed: list[str] = []

    for name, plugin in sorted(installed.items()):
        should_be_on = name in wanted
        currently_on = _enabled(workspace, plugin)
        if should_be_on == currently_on:
            continue
        try:
            set_agent_plugin_enabled(workspace, name, should_be_on)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Could not reconcile Agent Plugin '{}': {}", name, exc)
            failed.append(name)
            continue
        (enabled if should_be_on else disabled).append(name)

    unknown = tuple(name for name in wanted if name not in installed)
    if unknown:
        logger.warning(
            "tools.agentPlugins names no installed package: {}", ", ".join(sorted(unknown))
        )
    return ReconcileResult(
        enabled=tuple(enabled),
        disabled=tuple(disabled),
        unknown=unknown,
        failed=tuple(failed),
    )


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_tuple(value: object) -> tuple[str, ...]:
    items = cast(list[object], value) if isinstance(value, list) else []
    return tuple(item.strip() for item in items if isinstance(item, str) and item.strip())


def _accent_color(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else None


def _plugin_logo(value: object, plugin_root: Path) -> str | None:
    """Resolve our optional packaged logo extension."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("./"):
        logger.warning("Ignoring invalid Agent Plugin logo in '{}'", plugin_root)
        return None
    logo = _contained(plugin_root / value[2:], plugin_root)
    try:
        data = logo.read_bytes() if logo is not None else b""
        suffix = logo.suffix.lower() if logo is not None else ""
        # Magic bytes, not just the suffix: this becomes a data: URL the WebUI renders.
        if len(data) <= _MAX_LOGO_BYTES and (
            suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n")
            or suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff")
            or suffix == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        ):
            mime = "jpeg" if suffix in {".jpg", ".jpeg"} else suffix[1:]
            return f"data:image/{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except OSError:
        pass
    logger.warning("Ignoring invalid Agent Plugin logo in '{}'", plugin_root)
    return None


def _plugin_mcp_servers(workspace: Path, plugin: AgentPlugin) -> dict[str, MCPServerConfig]:
    payload = _read_object(plugin.root / "mcp.json", plugin.root)
    if payload is None:
        return {}
    raw_servers = payload.get("mcpServers")
    if (
        payload.keys() != {"$schema", "mcpServers"}
        or payload.get("$schema") != AGENT_PLUGIN_MCP_SCHEMA
        or not isinstance(raw_servers, dict)
    ):
        logger.warning("Ignoring invalid MCP component for Agent Plugin '{}'", plugin.name)
        return {}

    data = _plugin_data_dir(workspace, plugin.name, create=True)
    servers: dict[str, MCPServerConfig] = {}
    for name, raw in cast(dict[str, object], raw_servers).items():
        if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
            logger.warning("Ignoring invalid MCP server name in Agent Plugin '{}'", plugin.name)
            continue
        server = _plugin_mcp_server(raw, plugin.root, data)
        if server is None:
            logger.warning(
                "Ignoring invalid MCP server '{}' in Agent Plugin '{}'", name, plugin.name
            )
            continue
        servers[name] = server
    return servers


def _plugin_mcp_server(raw: object, root: Path, data: Path) -> MCPServerConfig | None:
    if not isinstance(raw, dict):
        return None
    payload = cast(dict[str, object], raw)
    # An unknown field means a package expecting semantics we do not implement, so it is refused
    # rather than silently reinterpreted.
    if payload.keys() - _MCP_SERVER_FIELDS:
        return None
    try:
        server = MCPServerConfig.model_validate(payload)
    except ValidationError:
        return None
    command = _stdio_command(server.command, root)
    cwd = _stdio_cwd(payload.get("cwd"), root, data)
    if server.type != "stdio" or command is None or cwd is None:
        return None
    # The package must not set the two variables that tell it where it lives.
    if {"PLUGIN_ROOT", "PLUGIN_DATA"} & server.env.keys():
        return None
    return server.model_copy(
        update={
            "command": command,
            "args": [_expand(item, root, data) for item in server.args],
            "env": {
                **{key: _expand(value, root, data) for key, value in server.env.items()},
                "PYTHONDONTWRITEBYTECODE": "1",
                "PLUGIN_ROOT": str(root),
                "PLUGIN_DATA": str(data),
            },
            "cwd": str(cwd),
        }
    )


def _stdio_command(value: object, root: Path) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("./"):
        executable = _contained(root / value[2:], root)
        return str(executable) if executable is not None else None
    # Anything else must be a bare name resolved on PATH. A path here would let a package name a
    # program outside itself.
    if any(char.isspace() for char in value) or "/" in value or "\\" in value:
        return None
    return value


def _stdio_cwd(value: object, root: Path, data: Path) -> Path | None:
    if value is None:
        return root
    if not isinstance(value, str):
        return None
    if value.startswith("./"):
        return _contained(root / value[2:], root, directory=True)
    for placeholder, base in (("${PLUGIN_ROOT}", root), ("${PLUGIN_DATA}", data)):
        if value == placeholder or value.startswith(f"{placeholder}/"):
            relative = value[len(placeholder):].lstrip("/")
            candidate = (base / relative).resolve()
            if not candidate.is_relative_to(base):
                return None
            if base == data:
                candidate.mkdir(parents=True, exist_ok=True)
                candidate.chmod(0o700)
            return candidate if candidate.is_dir() else None
    return None


def _expand(value: str, root: Path, data: Path) -> str:
    return value.replace("${PLUGIN_ROOT}", str(root)).replace("${PLUGIN_DATA}", str(data))


def _workspace_namespace(workspace: Path) -> str:
    return sha256(str(workspace.expanduser().resolve()).encode()).hexdigest()[:12]


def _descend_contained(base: Path, segments: tuple[str, ...], *, create: bool, mode: int) -> Path:
    """Walk into *base* one segment at a time, refusing any step that escapes it."""
    current = base
    for segment in segments:
        path = current / segment
        if create:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Where the split is kernel-enforced the agent account cannot create under the
                # gate tree, and that refusal is the point. Say so rather than leaking an errno.
                raise RuntimeError(
                    f"cannot create the Agent Plugin directory {path}: {exc}"
                ) from exc
        try:
            resolved = path.resolve(strict=create)
        except OSError as exc:
            raise RuntimeError("Agent Plugin directory is unavailable") from exc
        # Checked at every level, because a symlink at any one of them is enough to leave.
        if not resolved.is_relative_to(current):
            raise RuntimeError("Agent Plugin directory escapes its parent")
        if create:
            resolved.chmod(mode)
        current = resolved
    return current


def _plugin_data_dir(workspace: Path, name: str, *, create: bool) -> Path:
    """The plugin's own runtime scratch space (``PLUGIN_DATA``). Agent-writable by design."""
    base = get_config_path().expanduser().resolve().parent
    return _descend_contained(
        base,
        ("plugin-data", _workspace_namespace(workspace), name),
        create=create,
        mode=0o700,
    )


def _plugin_activation_dir(workspace: Path, name: str, *, create: bool) -> Path:
    """Where the ``enabled`` marker lives: the executor-owned gate tree, not the data dir.

    ``entrypoint.sh`` already hands ``<data dir>/gates`` to ``exec_user:ipc_group`` at mode 2750,
    so the executor writes and the shared group reads. Skill loading runs as the agent and only
    reads, which is why this is group-readable rather than 700.
    """
    base = (get_data_dir() / "gates").expanduser().resolve()
    return _descend_contained(
        base,
        (_ACTIVATION_SUBDIR, _workspace_namespace(workspace), name),
        create=create,
        mode=_ACTIVATION_DIR_MODE,
    )


def _enabled_package_fingerprint(workspace: Path, plugin: AgentPlugin) -> str | None:
    """Return the content fingerprint when this exact package is enabled."""
    try:
        marker = _plugin_activation_dir(workspace, plugin.name, create=False) / "enabled"
        if not marker.is_file():
            return None
        current = marker.read_text(encoding="utf-8")
        activation = _activation_marker(plugin)
        if activation is None:
            # The package cannot be fingerprinted at all, so it cannot stay active.
            _clear_marker(workspace, marker)
            return None
        payload = cast(dict[str, object], json.loads(activation))
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, str):
            return None
        if current == activation:
            return fingerprint
        # A pre-fingerprint marker recorded only the root. Upgrade it in place rather than
        # deactivating a package the operator already reviewed.
        if current == str(plugin.root):
            marker.write_text(activation, encoding="utf-8")
            marker.chmod(_ACTIVATION_FILE_MODE)
            return fingerprint
        # The package changed after it was enabled. It stops being active until re-reviewed.
        _clear_marker(workspace, marker)
        return None
    except (OSError, json.JSONDecodeError, RuntimeError):
        _invalidate_skill_cache(workspace)
        return None


def _clear_marker(workspace: Path, marker: Path) -> None:
    """Drop an activation that no longer matches its package.

    The agent account cannot unlink here, and that is the intended asymmetry: a stale marker fails
    the fingerprint comparison above on every read, so the package stays inactive either way.
    """
    with suppress(OSError):
        marker.unlink(missing_ok=True)
    _invalidate_skill_cache(workspace)


def _enabled(workspace: Path, plugin: AgentPlugin) -> bool:
    return _enabled_package_fingerprint(workspace, plugin) is not None


def _activation_marker(plugin: AgentPlugin) -> str | None:
    """Bind activation to one immutable package snapshot."""
    fingerprint = _package_fingerprint(plugin.root)
    if fingerprint is None:
        return None
    return json.dumps(
        {"fingerprint": fingerprint, "root": str(plugin.root)},
        separators=(",", ":"),
        sort_keys=True,
    )


def _discover_plugin_skills(plugin_name: str, plugin_root: Path) -> list[tuple[str, Path]]:
    skills_root = _contained(plugin_root / "skills", plugin_root, directory=True)
    if skills_root is None:
        return []

    skills: list[tuple[str, Path]] = []
    for candidate in _children(skills_root, f"Agent Plugin '{plugin_name}' skills"):
        skill_root = _contained(candidate, skills_root, directory=True)
        if skill_root is None:
            continue
        skill_file = _contained(skill_root / "SKILL.md", plugin_root)
        if skill_file is None:
            continue
        try:
            metadata = parse_skill_metadata(skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            metadata = None
        if metadata is None or not valid_skill_metadata(metadata, candidate.name):
            logger.warning(
                "Ignoring Agent Plugin '{}' skill '{}': invalid metadata",
                plugin_name,
                candidate.name,
            )
            continue
        skills.append((candidate.name, skill_file))
    return skills


def _children(root: Path, label: str) -> list[Path]:
    try:
        return sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        logger.warning("Could not inspect {}: {}", label, exc)
        return []


def _contained(path: Path, root: Path, *, directory: bool = False) -> Path | None:
    """Resolve *path* and return it only when it is the right kind and still inside *root*."""
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    expected_kind = resolved.is_dir() if directory else resolved.is_file()
    return resolved if expected_kind and resolved.is_relative_to(root) else None


def _read_object(path: Path, root: Path) -> dict[str, object] | None:
    contained = _contained(path, root)
    if contained is None:
        return None
    try:
        value = cast(object, json.loads(contained.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring invalid Agent Plugin component '{}': {}", contained, exc)
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None
