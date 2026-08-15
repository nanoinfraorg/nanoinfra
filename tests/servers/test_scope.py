# tests/servers/test_scope.py
"""Item 2 (#4): resolve an action's scope to host, group, or all.

Scope is a fact about the resolved host set. The resolver names those hosts, so
#14 can show them and #8's guard can check every one of them. Two error rules
carry the weight here. An inventory the resolver cannot read is an error. A
pattern the resolver cannot expand is an error. Neither is an empty host set.

This item keeps the refusal for a group-only ansible-runner server
(server_execution.py:232). #9 removes it. The resolver only classifies such a
config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.servers.scope import (
    ALL,
    GROUP,
    HOST,
    UNRESOLVED,
    ScopeResolutionError,
    resolve_scope,
    resolve_scope_label,
)
from nanoinfra.servers.types import Server


def _server(provider_id: str, config: dict[str, str]) -> Server:
    return Server(
        id="a" * 32,
        name="test-server",
        provider_id=provider_id,
        config=config,
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


# Four hosts: one ungrouped, two in `web`, one in `db`. `prod` holds three of
# them through children. `db` deliberately holds exactly one host.
_INI_INVENTORY = """\
# a comment line
standalone.example.com

[web]
web1.example.com
web2.example.com ansible_host=10.0.2.2

[db]
db1.example.com

[prod:children]
web
db

[web:vars]
http_port=80
"""


def _project(tmp_path: Path, text: str = _INI_INVENTORY) -> str:
    # ansible-runner reads <projectPath>/inventory when the backend names no
    # inventory, which it never does.
    (tmp_path / "inventory").write_text(text, encoding="utf-8")
    return str(tmp_path)


def _ansible(project_path: str, **config: str) -> Server:
    return _server("ansible-runner", {"projectPath": project_path, **config})


def test_ssh_server_resolves_to_scope_host():
    resolution = resolve_scope(_server("ssh", {"host": "10.0.1.5"}))

    assert resolution.scope == HOST


def test_ssh_server_names_only_the_dialed_host():
    # SSHBackend never reads `group`, so a group field in an ssh config widens
    # nothing. A resolver that read it would report hosts nothing connects to.
    resolution = resolve_scope(_server("ssh", {"host": "10.0.1.5", "group": "web"}))

    assert resolution.hosts == ("10.0.1.5",)


def test_ssh_server_without_host_is_an_error_not_an_empty_set():
    with pytest.raises(ScopeResolutionError):
        resolve_scope(_server("ssh", {"group": "web"}))


def test_ssm_server_resolves_to_the_single_instance_id():
    resolution = resolve_scope(_server("ssm", {"instanceId": "i-0abc", "region": "us-east-1"}))

    assert resolution.scope == HOST
    assert resolution.hosts == ("i-0abc",)


def test_api_server_resolves_to_the_base_url_origin_host():
    resolution = resolve_scope(_server("api", {"baseUrl": "https://api.internal:8443/v1"}))

    assert resolution.scope == HOST
    assert resolution.hosts == ("api.internal",)


def test_unknown_provider_is_an_error():
    with pytest.raises(ScopeResolutionError):
        resolve_scope(_server("telepathy", {"host": "10.0.1.5"}))


def test_scope_values_are_the_three_the_spec_names():
    assert (HOST, GROUP, ALL) == ("host", "group", "all")


def test_ansible_group_resolves_to_the_exact_named_hosts(tmp_path: Path):
    resolution = resolve_scope(_ansible(_project(tmp_path), group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")
    assert resolution.scope == GROUP


def test_ansible_group_of_one_host_resolves_to_scope_host(tmp_path: Path):
    # Blast radius is a fact about the resolved set. The word "group" in the
    # config decides nothing.
    resolution = resolve_scope(_ansible(_project(tmp_path), group="db"))

    assert resolution.hosts == ("db1.example.com",)
    assert resolution.scope == HOST


def test_ansible_children_groups_expand_transitively(tmp_path: Path):
    resolution = resolve_scope(_ansible(_project(tmp_path), group="prod"))

    assert resolution.hosts == ("db1.example.com", "web1.example.com", "web2.example.com")
    assert resolution.scope == GROUP


def test_ansible_inventory_host_wins_over_group_like_the_backend(tmp_path: Path):
    # ansible_backend.py:58 targets `inventoryHost or group`, in that order.
    resolution = resolve_scope(
        _ansible(_project(tmp_path), inventoryHost="web1.example.com", group="prod")
    )

    assert resolution.hosts == ("web1.example.com",)
    assert resolution.scope == HOST


def test_ansible_literal_all_resolves_to_scope_all(tmp_path: Path):
    resolution = resolve_scope(_ansible(_project(tmp_path), group="all"))

    assert resolution.scope == ALL
    assert resolution.hosts == (
        "db1.example.com",
        "standalone.example.com",
        "web1.example.com",
        "web2.example.com",
    )


def test_ansible_wildcard_resolves_to_scope_all(tmp_path: Path):
    resolution = resolve_scope(_ansible(_project(tmp_path), group="web*"))

    assert resolution.scope == ALL
    assert resolution.hosts == ("web1.example.com", "web2.example.com")


def test_unbounded_pattern_stays_scope_all_at_one_resolved_host(tmp_path: Path):
    # The one case where the pattern outranks the count: `all` also covers every
    # host an operator adds tomorrow.
    project = _project(tmp_path, "[db]\ndb1.example.com\n")

    resolution = resolve_scope(_ansible(project, group="all"))

    assert resolution.hosts == ("db1.example.com",)
    assert resolution.scope == ALL


def test_ansible_config_without_a_target_field_is_an_error(tmp_path: Path):
    # Same refusal the backend makes: running against the whole inventory is
    # never inferred from a missing field.
    with pytest.raises(ScopeResolutionError):
        resolve_scope(_ansible(_project(tmp_path), host="10.0.1.5"))


def test_missing_inventory_is_an_error_not_an_empty_set(tmp_path: Path):
    with pytest.raises(ScopeResolutionError, match="No inventory at"):
        resolve_scope(_ansible(str(tmp_path), group="web"))


def test_unreadable_inventory_is_an_error_not_an_empty_set(tmp_path: Path):
    (tmp_path / "inventory").write_bytes(b"[web]\n\xff\xfe not utf-8\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(str(tmp_path), group="web"))


def test_unknown_group_is_an_error_not_an_empty_set(tmp_path: Path):
    with pytest.raises(ScopeResolutionError, match="matches no host or group"):
        resolve_scope(_ansible(_project(tmp_path), group="staging"))


def test_exclusion_pattern_is_an_error_not_a_guess(tmp_path: Path):
    with pytest.raises(ScopeResolutionError, match="unsupported syntax"):
        resolve_scope(_ansible(_project(tmp_path), group="web,!web2.example.com"))


def test_colon_union_pattern_is_an_error_not_a_guess(tmp_path: Path):
    with pytest.raises(ScopeResolutionError, match="unsupported syntax"):
        resolve_scope(_ansible(_project(tmp_path), group="web:db"))


def test_numeric_host_range_expands_to_every_host_it_covers(tmp_path: Path):
    project = _project(tmp_path, "[web]\nweb[01:03].example.com\n")

    resolution = resolve_scope(_ansible(project, group="web"))

    assert resolution.hosts == (
        "web01.example.com",
        "web02.example.com",
        "web03.example.com",
    )
    assert resolution.scope == GROUP


def test_alphabetic_host_range_expands_to_every_host_it_covers(tmp_path: Path):
    project = _project(tmp_path, "[cache]\ncache-[a:c]\n")

    resolution = resolve_scope(_ansible(project, group="cache"))

    assert resolution.hosts == ("cache-a", "cache-b", "cache-c")


def test_unsupported_range_syntax_is_an_error_not_a_silent_undercount(tmp_path: Path):
    # A dash is not ansible range syntax. One entry left literal would report one
    # host while the backend runs against three.
    project = _project(tmp_path, "[web]\nweb[1-3].example.com\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(project, group="web"))


def test_huge_host_range_is_an_error_not_an_expansion(tmp_path: Path):
    project = _project(tmp_path, "[web]\nweb[1:5000].example.com\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(project, group="web"))


def test_empty_inventory_directory_is_an_error_not_an_empty_set(tmp_path: Path):
    (tmp_path / "inventory").mkdir()

    with pytest.raises(ScopeResolutionError, match="no inventory file"):
        resolve_scope(_ansible(str(tmp_path), group="web"))


_YAML_INVENTORY = """\
all:
  hosts:
    standalone.example.com:
  children:
    web:
      hosts:
        web1.example.com:
        web2.example.com:
          ansible_host: 10.0.2.2
      vars:
        http_port: 80
    db:
      hosts:
        db1.example.com:
    prod:
      children:
        web:
        db:
"""


def test_yaml_inventory_group_resolves_to_the_exact_named_hosts(tmp_path: Path):
    # ansible-runner's default inventory path has no suffix, so the loader reads
    # the content rather than the file name.
    project = _project(tmp_path, _YAML_INVENTORY)

    resolution = resolve_scope(_ansible(project, group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")
    assert resolution.scope == GROUP


def test_yaml_inventory_children_expand_transitively(tmp_path: Path):
    project = _project(tmp_path, _YAML_INVENTORY)

    resolution = resolve_scope(_ansible(project, group="prod"))

    assert resolution.hosts == ("db1.example.com", "web1.example.com", "web2.example.com")


def test_yaml_inventory_all_covers_every_host(tmp_path: Path):
    project = _project(tmp_path, _YAML_INVENTORY)

    resolution = resolve_scope(_ansible(project, group="all"))

    assert resolution.hosts == (
        "db1.example.com",
        "standalone.example.com",
        "web1.example.com",
        "web2.example.com",
    )
    assert resolution.scope == ALL


def test_yaml_inventory_hosts_as_a_list_are_named(tmp_path: Path):
    project = _project(tmp_path, "web:\n  hosts:\n    - web1.example.com\n    - web2.example.com\n")

    resolution = resolve_scope(_ansible(project, group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")


def test_yaml_inventory_host_range_expands_to_every_host(tmp_path: Path):
    project = _project(tmp_path, "web:\n  hosts:\n    web[1:2].example.com:\n")

    resolution = resolve_scope(_ansible(project, group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")


def test_broken_yaml_inventory_is_an_error_not_an_empty_set(tmp_path: Path):
    # Valid YAML mapping shape, unknown key. The loader must not read past a key
    # it does not understand, because hosts may hide behind it.
    project = _project(tmp_path, "web:\n  hostz:\n    web1.example.com:\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(project, group="web"))


def test_recursive_yaml_inventory_is_an_error_not_a_hang(tmp_path: Path):
    project = _project(tmp_path, "prod: &p\n  children:\n    inner: *p\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(project, group="prod"))


def test_inventory_directory_merges_every_file(tmp_path: Path):
    inventory = tmp_path / "inventory"
    inventory.mkdir()
    (inventory / "01-web").write_text("[web]\nweb1.example.com\n", encoding="utf-8")
    (inventory / "02-web-more").write_text("[web]\nweb2.example.com\n", encoding="utf-8")
    (inventory / "notes.md").write_text("[web]\nignored.example.com\n", encoding="utf-8")

    resolution = resolve_scope(_ansible(str(tmp_path), group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")


async def test_execute_on_server_observation_carries_the_resolved_scope(tmp_path: Path):
    """#16's record gains the scope field this item resolves.

    A dry run reaches the recorder and connects to nothing, so this test needs no
    backend. The refusal for a group-only ansible server stays where it is: #9 owns
    that, and this item only classifies such a config.
    """
    from loguru import logger

    from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
    from nanoinfra.secrets.store import SecretStore
    from nanoinfra.servers.job_store import JobStore
    from nanoinfra.servers.store import ServerStore

    captured: list[dict[str, object]] = []

    def sink(message: object) -> None:
        record = getattr(message, "record", {})["extra"].get("gate_observation")
        if record is not None:
            captured.append(record)

    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )
    tool = ExecuteOnServerTool(
        servers=ServerStore(tmp_path), secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )
    sink_id = logger.add(sink, level=0)
    try:
        await tool.execute(server_id_or_name="prod-web-01", command="uptime")
    finally:
        logger.remove(sink_id)

    remote = [record for record in captured if record["capability_class"] == MUTATE_REMOTE]
    assert [record["scope"] for record in remote] == [HOST]


def test_resolve_scope_label_names_the_scope(tmp_path: Path):
    assert resolve_scope_label(_ansible(_project(tmp_path), group="web")) == GROUP


def test_resolve_scope_label_says_unresolved_instead_of_raising(tmp_path: Path):
    # The observation record in server_execution.py must never fail a call. An
    # unknown blast radius is still a fact worth recording, and it never reads as
    # "one host".
    assert resolve_scope_label(_ansible(str(tmp_path), group="web")) == UNRESOLVED
    assert UNRESOLVED not in (HOST, GROUP, ALL)
