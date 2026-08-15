"""Scope resolution for Server actions -- how many hosts does one action touch?

Scope is orthogonal to nanoinfra/agent/tools/capabilities.py's class. The class
says what kind of action this is. The scope says how wide the blast radius is.

Scope is always a fact about the RESOLVED set of hosts, never about the word in
the config. A ``group`` that holds one host is scope ``host``. Only an unbounded
pattern keeps scope ``all`` at one host, because the pattern also covers every
host somebody adds tomorrow.

The resolver returns the named hosts, not only a count. #14 renders those names
to a human, and #8's guard must check every one of them -- one validated address
plus a backend that dials fourteen is the bypass class that
nanoinfra/agent/tools/server_execution.py:44-53 documents for the single-host
case.

Each provider branch reads EXACTLY the config fields its backend reads, for the
same reason _target_host() does: a resolver that expands a field the backend
ignores describes a blast radius nothing ever touches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import yaml

from nanoinfra.servers.types import Server

HOST = "host"
GROUP = "group"
ALL = "all"

# Not a scope. The label a record carries when the resolver cannot name the hosts.
# It must stay distinct from the three scopes, because "I do not know" and "one
# host" are opposite facts.
UNRESOLVED = "unresolved"

# ansible-runner's own default. The backend passes no explicit inventory, so
# RunnerConfig.prepare_inventory() supplies -i <private_data_dir>/inventory when
# that path exists. This is therefore the inventory the backend really reads. When
# the path is absent, ansible falls back to ansible.cfg or /etc/ansible/hosts.
# This resolver cannot see that fallback, so such a config is an error.
_INVENTORY_NAME = "inventory"

# Files ansible itself skips inside an inventory directory. The list approximates
# ansible's INVENTORY_IGNORE_EXTS. group_vars/host_vars are directories, so the
# directory check below already drops them.
_IGNORED_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".swp", ".bak", ".orig", ".retry", ".cfg", ".md", ".txt", ".rst", ".ini"}
)

# Pattern syntax this resolver refuses. It never guesses. `!` excludes, `&`
# intersects, `~` starts a regex, and `[` opens a range. `:` is both ansible's
# legacy union separator and the separator inside an IPv6 address, so its meaning
# is ambiguous here. A wrong resolved set is a guard bypass, not a cosmetic
# defect, so an unsupported pattern raises. A comma still unions two terms.
_UNSUPPORTED_PATTERN_CHARS = ("!", "&", "~", ":", "[", "]")

_GLOB_CHARS = ("*", "?")

_SECTION_RE = re.compile(r"^\[([^\]]+)\]$")

# One host range inside an inventory entry, such as web[01:03].example.com.
_RANGE_RE = re.compile(r"\[([^\[\]]*)\]")

# A range wider than this stops the resolver. projectPath comes from an
# agent-editable config, and a million-host expansion serves no operator.
_MAX_RANGE_HOSTS = 1024

# How deep a YAML children chain may go before the loader calls it recursive.
_MAX_YAML_DEPTH = 32

# Hosts an INI inventory lists before its first section. ansible names this group
# ungrouped.
_UNGROUPED = "ungrouped"


class ScopeResolutionError(Exception):
    """The resolver cannot name the hosts this action touches.

    Never substitute an empty set for this error. "No host matched" and "I could
    not expand the pattern" look identical in a count and are opposite facts. The
    first is a safe no-op. The second is an unknown blast radius. A caller that
    must not handle an exception calls resolve_scope_label() instead.
    """


@dataclass(frozen=True)
class ScopeResolution:
    """The named hosts an action touches, plus the scope those names imply."""

    scope: str
    hosts: tuple[str, ...]
    pattern: str | None = None


def resolve_scope(server: Server) -> ScopeResolution:
    """Resolve the hosts one action against ``server`` would touch.

    Raises ScopeResolutionError when the target cannot be named. Nothing here
    connects to anything: every provider branch reads config, and the
    ansible-runner branch reads the local inventory files only.
    """
    provider_id = server.provider_id
    if provider_id == "ssh":
        # SSHBackend dials config["host"] and nothing else, so one ssh server is
        # always exactly one host.
        return _single_host(server, "host")
    if provider_id == "ssm":
        # No dialed address exists. SSM Run Command names one instance id and IAM
        # authorizes the call, so the instance id IS the named host.
        return _single_host(server, "instanceId")
    if provider_id == "api":
        # ApiBackend pins every request to the configured baseUrl origin, so the
        # blast radius is that one origin's host.
        return _api_host(server)
    if provider_id == "ansible-runner":
        return _resolve_ansible(server)
    raise ScopeResolutionError(f"Unknown providerId {provider_id!r}. The scope is unknown.")


def resolve_scope_label(server: Server) -> str:
    """The scope for an observation record, or ``unresolved``. Never raises.

    nanoinfra/agent/tools/capabilities.py's record is log-only in M1, so a resolver
    problem must not fail the call it describes. The caller still learns the truth:
    ``unresolved`` says the blast radius is unknown.
    """
    try:
        return resolve_scope(server).scope
    except ScopeResolutionError:
        return UNRESOLVED


def _single_host(server: Server, config_field: str) -> ScopeResolution:
    value = server.config.get(config_field, "").strip()
    if not value:
        raise ScopeResolutionError(
            f"Server config has no {config_field} to resolve. The scope is unknown, not empty."
        )
    return ScopeResolution(scope=HOST, hosts=(value,), pattern=value)


def _api_host(server: Server) -> ScopeResolution:
    base_url = server.config.get("baseUrl", "").strip()
    hostname = urlparse(base_url).hostname if base_url else None
    if not hostname:
        raise ScopeResolutionError(
            f"Server config baseUrl {base_url!r} names no host. The scope is unknown, not empty."
        )
    return ScopeResolution(scope=HOST, hosts=(hostname,), pattern=base_url)


def _resolve_ansible(server: Server) -> ScopeResolution:
    """Expand the pattern AnsibleRunnerBackend targets, against the same inventory.

    The pattern and the projectPath default both mirror ansible_backend.py:58 and
    :72 exactly. A resolver that reads a different field than the backend reports
    a blast radius nothing ever touches.
    """
    pattern = (server.config.get("inventoryHost") or server.config.get("group") or "").strip()
    if not pattern:
        raise ScopeResolutionError(
            "Server config has no inventoryHost or group to target. The whole inventory "
            "is never inferred from a missing field."
        )
    inventory = _load_inventory(server.config.get("projectPath") or ".")
    hosts, unbounded = _expand_pattern(pattern, inventory)
    # Hosts stay sorted so one config always reports one host order. #14 shows
    # these names to a human, and an unstable order reads like a changed target.
    return ScopeResolution(
        scope=_scope_for(hosts, unbounded=unbounded),
        hosts=tuple(sorted(hosts)),
        pattern=pattern,
    )


def _scope_for(hosts: frozenset[str], *, unbounded: bool) -> str:
    """Derive the scope from the resolved set, with one documented exception.

    An unbounded pattern keeps scope ``all`` even at one resolved host. It also
    covers every host an operator adds tomorrow, so the count today understates
    it. Every bounded pattern takes its scope from the count alone.
    """
    if unbounded:
        return ALL
    return GROUP if len(hosts) > 1 else HOST


@dataclass
class _Inventory:
    """Flattened inventory: every host, and the hosts each group resolves to."""

    hosts: frozenset[str]
    groups: dict[str, frozenset[str]]
    source: str


@dataclass
class _InventoryBuilder:
    hosts: set[str] = field(default_factory=set)
    group_hosts: dict[str, set[str]] = field(default_factory=dict)
    group_children: dict[str, set[str]] = field(default_factory=dict)

    def ensure_group(self, name: str) -> None:
        self.group_hosts.setdefault(name, set())
        self.group_children.setdefault(name, set())

    def add_host(self, group: str, host: str) -> None:
        self.ensure_group(group)
        self.group_hosts[group].add(host)
        self.hosts.add(host)

    def add_child(self, group: str, child: str) -> None:
        self.ensure_group(group)
        self.ensure_group(child)
        self.group_children[group].add(child)

    def build(self, source: str) -> _Inventory:
        groups = {name: frozenset(self._hosts_of(name, set())) for name in self.group_hosts}
        # `all` holds every host, and no inventory file has to say so.
        groups[ALL] = frozenset(self.hosts)
        return _Inventory(hosts=frozenset(self.hosts), groups=groups, source=source)

    def _hosts_of(self, name: str, seen: set[str]) -> set[str]:
        # `seen` guards a children cycle. ansible rejects such an inventory, but a
        # resolver that recurses forever helps nobody.
        if name in seen:
            return set()
        seen.add(name)
        hosts = set(self.group_hosts.get(name, set()))
        for child in self.group_children.get(name, set()):
            hosts |= self._hosts_of(child, seen)
        return hosts


def _load_inventory(project_path: str) -> _Inventory:
    """Read the local inventory under ``project_path``. No network call happens."""
    path = Path(project_path) / _INVENTORY_NAME
    if not path.exists():
        raise ScopeResolutionError(
            f"No inventory at {path}. ansible then falls back to ansible.cfg or "
            "/etc/ansible/hosts, which this resolver cannot read, so the host set is "
            "unknown rather than empty."
        )
    sources = [path] if path.is_file() else _inventory_files(path)
    builder = _InventoryBuilder()
    for source in sources:
        _load_source(source, builder)
    return builder.build(str(path))


def _inventory_files(directory: Path) -> list[Path]:
    """The inventory files inside an inventory directory, in a stable order."""
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        raise ScopeResolutionError(f"Cannot read inventory directory {directory}: {exc}") from exc
    files = [
        entry
        for entry in entries
        if entry.is_file()
        and not entry.name.startswith(".")
        and not entry.name.endswith("~")
        and entry.suffix.lower() not in _IGNORED_SUFFIXES
    ]
    if not files:
        raise ScopeResolutionError(
            f"Inventory directory {directory} holds no inventory file, so the host set "
            "is unknown rather than empty."
        )
    return files


def _load_source(source: Path, builder: _InventoryBuilder) -> None:
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # An unreadable inventory is an error. Silence here would turn a permission
        # problem into "this action touches no host".
        raise ScopeResolutionError(f"Cannot read inventory {source}: {exc}") from exc
    # Content decides the format, not the file name: ansible-runner's default
    # inventory path is <projectPath>/inventory, which carries no suffix and holds
    # either format. The YAML attempt comes first because ansible's own plugin
    # order puts the yaml plugin ahead of the ini plugin.
    mapping = _yaml_mapping(text, source)
    if mapping is None:
        _load_ini(text, source, builder)
        return
    _load_yaml_group(ALL, {"children": mapping}, source, builder, depth=0)


def _yaml_mapping(text: str, source: Path) -> dict[object, object] | None:
    """The inventory as a YAML mapping, or None when the text is not YAML.

    INI inventory text is rarely valid YAML, and the rest is not a mapping, so a
    None result means "read this as INI".
    """
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    except Exception as exc:  # noqa: BLE001 -- must report, not raise a foreign type
        raise ScopeResolutionError(f"Cannot read inventory {source}: {exc}") from exc
    if isinstance(data, dict):
        return cast(dict[object, object], data)
    return None


def _load_yaml_group(
    name: str, body: object, source: Path, builder: _InventoryBuilder, *, depth: int
) -> None:
    if depth > _MAX_YAML_DEPTH:
        # A YAML anchor can point a child group back at its own parent. PyYAML
        # builds that cycle happily, so the loader stops here.
        raise ScopeResolutionError(
            f"Cannot read inventory {source}: group nesting passes {_MAX_YAML_DEPTH} levels. "
            "A YAML anchor may make this inventory recursive."
        )
    builder.ensure_group(name)
    if body is None:
        return
    if not isinstance(body, dict):
        raise ScopeResolutionError(
            f"Cannot read inventory {source}: group {name!r} must hold a mapping."
        )
    for raw_key, value in cast(dict[object, object], body).items():
        key = str(raw_key)
        if key == "vars":
            continue
        if key == "hosts":
            for entry in _yaml_names(value, key, name, source):
                for host in _expand_entry(entry, source, f"group {name!r}"):
                    builder.add_host(name, host)
            continue
        if key == "children":
            for child, child_body in _yaml_children(value, name, source):
                builder.add_child(name, child)
                _load_yaml_group(child, child_body, source, builder, depth=depth + 1)
            continue
        # An unknown key may hide hosts. The resolver refuses rather than reports a
        # host set that misses them.
        raise ScopeResolutionError(
            f"Cannot read inventory {source}: group {name!r} holds unsupported key {key!r}."
        )


def _yaml_names(value: object, key: str, group: str, source: Path) -> list[str]:
    """The names under a ``hosts`` or ``children`` key, as a plain list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(name) for name in cast(dict[object, object], value)]
    if isinstance(value, list):
        names: list[str] = []
        for item in cast(list[object], value):
            if not isinstance(item, str):
                raise ScopeResolutionError(
                    f"Cannot read inventory {source}: group {group!r} lists a {key} entry "
                    "that is not a name."
                )
            names.append(item)
        return names
    raise ScopeResolutionError(
        f"Cannot read inventory {source}: group {group!r} holds a {key} value the resolver "
        "cannot read."
    )


def _yaml_children(value: object, group: str, source: Path) -> list[tuple[str, object]]:
    """Child group names paired with their own body, if the body is present."""
    if isinstance(value, dict):
        bodies = cast(dict[object, object], value)
        return [(str(name), body) for name, body in bodies.items()]
    return [(name, None) for name in _yaml_names(value, "children", group, source)]


def _load_ini(text: str, source: Path, builder: _InventoryBuilder) -> None:
    group = _UNGROUPED
    mode = "hosts"
    builder.ensure_group(_UNGROUPED)
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        match = _SECTION_RE.match(line)
        if match:
            group, mode = _ini_section(match.group(1).strip(), source, lineno)
            builder.ensure_group(group)
            continue
        if mode == "vars":
            # Variables never add a host, so they never widen the blast radius.
            continue
        # An INI host line carries inline variables after the name. Only the first
        # token names the host.
        entry = line.split()[0]
        if mode == "children":
            builder.add_child(group, entry)
            continue
        for host in _expand_entry(entry, source, f"line {lineno}"):
            builder.add_host(group, host)


def _expand_entry(entry: str, source: Path, where: str) -> list[str]:
    """Expand one inventory entry's host ranges, such as ``web[01:03].example.com``.

    One unexpanded range means the resolver reports one host while the backend runs
    against three, so an entry this function cannot expand raises.
    """
    match = _RANGE_RE.search(entry)
    if match is None:
        if "[" in entry or "]" in entry:
            raise ScopeResolutionError(
                f"Cannot read inventory {source} {where}: entry {entry!r} holds an "
                "unbalanced range."
            )
        return [entry]
    values = _range_values(match.group(1), entry, source, where)
    prefix, suffix = entry[: match.start()], entry[match.end() :]
    hosts: list[str] = []
    for value in values:
        # The recursion covers a second range in the same entry. A range value
        # holds no bracket, so this always terminates.
        hosts.extend(_expand_entry(prefix + value + suffix, source, where))
    if len(hosts) > _MAX_RANGE_HOSTS:
        raise ScopeResolutionError(
            f"Cannot read inventory {source} {where}: entry {entry!r} expands past "
            f"{_MAX_RANGE_HOSTS} hosts, so the resolver refuses to expand it."
        )
    return hosts


def _range_values(body: str, entry: str, source: Path, where: str) -> list[str]:
    error = ScopeResolutionError(
        f"Cannot read inventory {source} {where}: entry {entry!r} holds range "
        f"{body!r}, which is not ansible range syntax."
    )
    parts = body.split(":")
    if len(parts) not in (2, 3):
        raise error
    start, end = parts[0], parts[1]
    try:
        step = int(parts[2]) if len(parts) == 3 else 1
    except ValueError:
        raise error from None
    if step < 1:
        raise error
    values: list[str] = []
    if start.isdigit() and end.isdigit():
        # ansible pads to the width of the start value when that value has a
        # leading zero. web[01:03] gives web01, not web1.
        width = len(start) if start.startswith("0") else 0
        values = [str(number).zfill(width) for number in range(int(start), int(end) + 1, step)]
    elif len(start) == 1 and len(end) == 1 and start.isalpha() and end.isalpha():
        values = [chr(point) for point in range(ord(start), ord(end) + 1, step)]
    else:
        raise error
    if not values:
        # A reversed range covers nothing. That is a broken entry, not a host with
        # no name.
        raise error
    if len(values) > _MAX_RANGE_HOSTS:
        raise ScopeResolutionError(
            f"Cannot read inventory {source} {where}: entry {entry!r} expands past "
            f"{_MAX_RANGE_HOSTS} hosts, so the resolver refuses to expand it."
        )
    return values


def _ini_section(name: str, source: Path, lineno: int) -> tuple[str, str]:
    if ":" not in name:
        return name, "hosts"
    base, _, suffix = name.rpartition(":")
    if suffix in ("children", "vars"):
        return base, suffix
    raise ScopeResolutionError(
        f"Cannot read inventory {source} line {lineno}: unsupported section [{name}]."
    )


def _expand_pattern(pattern: str, inventory: _Inventory) -> tuple[frozenset[str], bool]:
    """Expand an ansible host pattern to the hosts it names.

    Returns the hosts and a flag for an unbounded pattern. A term that matches
    nothing raises: ansible refuses such a pattern too, and a caller must never
    read a typo as a safe no-op.
    """
    resolved: set[str] = set()
    unbounded = False
    for term in [part.strip() for part in pattern.split(",")]:
        matched, term_unbounded = _expand_term(term, inventory, pattern)
        resolved |= matched
        unbounded = unbounded or term_unbounded
    if not resolved:
        raise _no_match_error(pattern, pattern, inventory)
    return frozenset(resolved), unbounded


def _expand_term(term: str, inventory: _Inventory, pattern: str) -> tuple[set[str], bool]:
    if not term:
        raise ScopeResolutionError(f"Pattern {pattern!r} holds an empty term.")
    for char in _UNSUPPORTED_PATTERN_CHARS:
        if char in term:
            raise ScopeResolutionError(
                f"Cannot expand pattern {pattern!r}: {char!r} is unsupported syntax. "
                "The resolved host set would be a guess."
            )
    if term in (ALL, "*"):
        return set(inventory.hosts), True
    if any(char in term for char in _GLOB_CHARS):
        return _glob_match(term, inventory, pattern), True
    # A group name outranks a host name, exactly as ansible resolves it.
    if term in inventory.groups:
        return set(inventory.groups[term]), False
    if term in inventory.hosts:
        return {term}, False
    raise _no_match_error(term, pattern, inventory)


def _glob_match(term: str, inventory: _Inventory, pattern: str) -> set[str]:
    # A glob matches group names and host names both, like ansible. The scope is
    # `all` either way, so only the named hosts change.
    matched: set[str] = set()
    for name, hosts in inventory.groups.items():
        if fnmatchcase(name, term):
            matched |= set(hosts)
    for host in inventory.hosts:
        if fnmatchcase(host, term):
            matched.add(host)
    if not matched:
        raise _no_match_error(term, pattern, inventory)
    return matched


def _no_match_error(term: str, pattern: str, inventory: _Inventory) -> ScopeResolutionError:
    return ScopeResolutionError(
        f"Cannot expand pattern {pattern!r}: {term!r} matches no host or group in "
        f"{inventory.source}. That is an unresolved pattern, not an empty one."
    )


__all__ = [
    "ALL",
    "GROUP",
    "HOST",
    "UNRESOLVED",
    "ScopeResolution",
    "ScopeResolutionError",
    "resolve_scope",
    "resolve_scope_label",
]
