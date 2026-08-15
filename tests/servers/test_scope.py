# tests/servers/test_scope.py
"""Item 2 (#4): resolve an action's scope to host, group, or all.

Scope is a fact about the resolved host set. The resolver names those hosts, so
#14 can show them and #8's guard can check every one of them. Two error rules
carry the weight here. An inventory the resolver cannot read is an error. A
pattern the resolver cannot expand is an error. Neither is an empty host set.

This item keeps the refusal for a group-only ansible-runner server
(server_execution.py:232). #9 removes it. The resolver only classifies such a
config.

Item 28 (#30) adds a second expander. ansible's own ``ansible-inventory`` answers
when the binary is installed, and the parser answers when it is not. Every test
here states a fact about ONE expander, so each one pins which expander answers.
The autouse fixture below makes the parser answer by default, because a developer
machine with ansible-core installed would otherwise read these parser tests
through ansible and see different error text. The subprocess is always a fake
here: tests/servers/test_scope_ansible_parity.py is the only place the real
binary runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nanoinfra.servers import scope
from nanoinfra.servers.scope import (
    ALL,
    EXPANDER_ANSIBLE,
    EXPANDER_CONFIG,
    EXPANDER_PARSER,
    GROUP,
    HOST,
    UNRESOLVED,
    ScopeResolutionError,
    resolve_scope,
    resolve_scope_label,
)
from nanoinfra.servers.types import Server

# A path that never runs. Every ansible-path test fakes the subprocess, so the
# value only has to be the truthy answer a detection returns.
_FAKE_BINARY = "/usr/bin/ansible-inventory"


@pytest.fixture(autouse=True)
def _parser_answers_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the in-repo parser the expander for every test that does not say otherwise.

    Without this, the same test file would test different code on a machine with
    ansible-core and on CI without it. A test whose meaning depends on the machine
    proves nothing.
    """
    monkeypatch.setattr(scope, "_ansible_inventory_binary", lambda: None)


class _FakeRun:
    """A recording stand-in for subprocess.run that never starts a process."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        error: Exception | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            args=argv,
            returncode=self.returncode,
            stdout=self.stdout.encode(),
            stderr=self.stderr.encode(),
        )


# What `ansible-inventory --list` prints for the INI fixture below. Verified
# against ansible-core 2.21.2: `web`/`db` hang off `prod`, hosts before the first
# section land in `ungrouped`, and group vars arrive as host vars under `_meta`.
_ANSIBLE_LIST_JSON = json.dumps(
    {
        "_meta": {
            "hostvars": {"web2.example.com": {"ansible_host": "10.0.2.2"}},
            "profile": "inventory_legacy",
        },
        "all": {"children": ["ungrouped", "prod"]},
        "db": {"hosts": ["db1.example.com"]},
        "prod": {"children": ["web", "db"]},
        "ungrouped": {"hosts": ["standalone.example.com"]},
        "web": {"hosts": ["web1.example.com", "web2.example.com"]},
    }
)


def _fake_ansible(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = _ANSIBLE_LIST_JSON,
    stderr: str = "",
    returncode: int = 0,
    error: Exception | None = None,
) -> _FakeRun:
    """Say the binary is present, and answer for it without a process."""
    fake = _FakeRun(stdout=stdout, stderr=stderr, returncode=returncode, error=error)
    monkeypatch.setattr(scope, "_ansible_inventory_binary", lambda: _FAKE_BINARY)
    monkeypatch.setattr(scope.subprocess, "run", fake)
    return fake


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


def test_yaml_inventory_hosts_as_a_list_is_an_error_like_ansible(tmp_path: Path):
    """ansible's yaml plugin refuses a list under ``hosts``, so the parser refuses it too.

    The parity test in tests/servers/test_scope_ansible_parity.py found this: the
    parser used to name both hosts, and ansible-core 2.21.2 fails the whole source
    ("requires a dictionary", plugins/inventory/yaml.py:141). A host set the backend
    can never reach is worse than a refusal, because the guard would check it.
    Only a mapping, a bare name, or nothing is valid.
    """
    project = _project(tmp_path, "web:\n  hosts:\n    - web1.example.com\n    - web2.example.com\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(project, group="web"))


def test_yaml_inventory_children_as_a_list_is_an_error_like_ansible(tmp_path: Path):
    # Same ansible rule, same section check, other key.
    project = _project(tmp_path, "web:\n  children:\n    - db\ndb:\n  hosts:\n    db1:\n")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(project, group="web"))


def test_yaml_inventory_hosts_as_a_bare_name_is_one_host(tmp_path: Path):
    # ansible converts a string section to a one-key mapping (yaml.py:138), so this
    # shape stays valid and the parser must keep reading it.
    project = _project(tmp_path, "web:\n  hosts: web1.example.com\n")

    resolution = resolve_scope(_ansible(project, group="web"))

    assert resolution.hosts == ("web1.example.com",)


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
    from nanoinfra.gates.executor.protocol import ExecuteRequest
    from nanoinfra.gates.executor.server import Executor
    from nanoinfra.servers.store import ServerStore

    captured: list[dict[str, object]] = []

    def sink(message: object) -> None:
        record = getattr(message, "record", {})["extra"].get("gate_observation")
        if record is not None:
            captured.append(record)

    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )
    # #18 moved the recorder into the executor with the credential resolution and the
    # transports, so the record is written there now.
    executor = Executor(workspace=tmp_path)
    request = ExecuteRequest(
        server_id_or_name="prod-web-01",
        command="uptime",
        session_id="s1",
        execution_context="interactive",
        preview_requested=True,
        timeout_s=None,
        token_nonce=None,
    )
    sink_id = logger.add(sink, level=0)
    try:
        await executor.handle(request)
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


# Item 28 (#30): ansible's own expander answers where a wrong answer causes harm.
# The tests below fake the subprocess. They pin which expander answered, what the
# record says about it, and the difference between an absent binary and a failed one.


def test_expander_says_config_when_no_inventory_is_read():
    # ssh/ssm/api name one host in the config, so no expander runs. The record must
    # not read as an authoritative inventory expansion.
    assert resolve_scope(_server("ssh", {"host": "10.0.1.5"})).expander == EXPANDER_CONFIG


def test_ansible_answers_and_the_resolution_names_that_expander(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _fake_ansible(monkeypatch)

    resolution = resolve_scope(_ansible(_project(tmp_path), group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")
    assert resolution.scope == GROUP
    assert resolution.expander == EXPANDER_ANSIBLE


def test_the_parser_answers_and_names_the_fallback_when_the_binary_is_absent(tmp_path: Path):
    # The autouse fixture removes the binary. A reviewer must be able to tell this
    # answer from the one above, because only one of the two is authoritative.
    resolution = resolve_scope(_ansible(_project(tmp_path), group="web"))

    assert resolution.hosts == ("web1.example.com", "web2.example.com")
    assert resolution.expander == EXPANDER_PARSER


def test_ansible_children_groups_expand_transitively_through_ansible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # ansible reports `prod` as a children-only group. The hosts sit under `web` and
    # `db`, so a resolver that read only the group's own `hosts` key would report an
    # empty set for a group that reaches three hosts.
    _fake_ansible(monkeypatch)

    resolution = resolve_scope(_ansible(_project(tmp_path), group="prod"))

    assert resolution.hosts == ("db1.example.com", "web1.example.com", "web2.example.com")


def test_ansible_all_covers_every_host_it_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _fake_ansible(monkeypatch)

    resolution = resolve_scope(_ansible(_project(tmp_path), group="all"))

    assert resolution.hosts == (
        "db1.example.com",
        "standalone.example.com",
        "web1.example.com",
        "web2.example.com",
    )
    assert resolution.scope == ALL


def test_ansible_reads_the_inventory_by_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A relative projectPath must still name the operator's file.

    The run below uses a temporary working directory, so a relative ``-i inventory``
    would point ansible at a file inside that empty directory and resolve a host set
    from nothing.
    """
    _project(tmp_path)
    fake = _fake_ansible(monkeypatch)
    monkeypatch.chdir(tmp_path)

    resolve_scope(_ansible(".", group="web"))

    argv, _ = fake.calls[0]
    assert str(tmp_path / "inventory") in argv


def test_ansible_never_runs_inside_the_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The run happens in a temporary directory, and leaves nothing behind.

    projectPath belongs to the operator, and it comes from an agent-editable config.
    A resolve reads. It must not write an artifact into a directory it only reads.
    """
    project = _project(tmp_path)
    fake = _fake_ansible(monkeypatch)

    resolve_scope(_ansible(project, group="web"))

    workdir = Path(str(fake.calls[0][1]["cwd"]))
    assert not str(workdir).startswith(project)
    # A temporary directory, so it is already gone once the resolve returns.
    assert not workdir.exists()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["inventory"]


def test_ansible_is_told_to_fail_on_a_source_it_cannot_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Both settings, by name. ansible's default is a warning and exit code 0.

    A default run answers an unreadable inventory with an inventory that holds only
    an implicit localhost. That reads as "this action touches no host", which is the
    one answer this module never gives.
    """
    fake = _fake_ansible(monkeypatch)

    resolve_scope(_ansible(_project(tmp_path), group="web"))

    env = fake.calls[0][1]["env"]
    assert isinstance(env, dict)
    assert env["ANSIBLE_INVENTORY_UNPARSED_FAILED"] == "True"
    assert env["ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED"] == "True"


def test_an_ansible_failure_is_an_error_and_never_a_parser_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """"Binary absent" falls back. "Binary present and failed" raises.

    A parser answer that contradicts ansible is the exact risk #30 closes. The second
    half of this test proves the parser WOULD have answered, so the refusal above is a
    choice and not a second inventory problem.
    """
    server = _ansible(_project(tmp_path), group="web")
    _fake_ansible(
        monkeypatch,
        returncode=1,
        stderr="[ERROR]: Completely failed to parse inventory source /p/inventory\n",
    )

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory") as raised:
        resolve_scope(server)

    assert "ansible-inventory" in str(raised.value)
    assert "web1.example.com" not in str(raised.value)
    monkeypatch.setattr(scope, "_ansible_inventory_binary", lambda: None)
    assert resolve_scope(server).hosts == ("web1.example.com", "web2.example.com")


def test_a_binary_that_cannot_start_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Detection said the binary was there. A failure to start it after that is a
    # broken install, not an absent one, so the host set stays unknown.
    _fake_ansible(monkeypatch, error=OSError("Permission denied"))

    with pytest.raises(ScopeResolutionError, match="Permission denied"):
        resolve_scope(_ansible(_project(tmp_path), group="web"))


def test_an_ansible_timeout_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A resolve that waits forever blocks the tool call that asked for it, and a
    # timeout tells nobody how many hosts the pattern names.
    _fake_ansible(monkeypatch, error=subprocess.TimeoutExpired(cmd="ansible-inventory", timeout=1))

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(_project(tmp_path), group="web"))


def test_ansible_output_that_is_not_json_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _fake_ansible(monkeypatch, stdout="not json at all")

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(_project(tmp_path), group="web"))


def test_a_group_key_the_resolver_does_not_know_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Same rule the YAML loader follows: an unknown key may hide hosts, so the
    # resolver refuses instead of reporting a host set that misses them.
    _fake_ansible(
        monkeypatch,
        stdout=json.dumps({"web": {"hosts": ["web1.example.com"], "descendants": ["db"]}}),
    )

    with pytest.raises(ScopeResolutionError, match="Cannot read inventory"):
        resolve_scope(_ansible(_project(tmp_path), group="web"))


def test_a_host_ansible_names_only_under_meta_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # An undercount is a guard bypass, so every host name ansible prints counts.
    _fake_ansible(
        monkeypatch,
        stdout=json.dumps(
            {
                "_meta": {"hostvars": {"orphan.example.com": {}}},
                "all": {"children": ["web"]},
                "web": {"hosts": ["web1.example.com"]},
            }
        ),
    )

    resolution = resolve_scope(_ansible(_project(tmp_path), group="all"))

    assert resolution.hosts == ("orphan.example.com", "web1.example.com")


def test_a_missing_inventory_refuses_before_ansible_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The path check stays ahead of both expanders.

    Without <projectPath>/inventory, ansible falls back to ansible.cfg or
    /etc/ansible/hosts and answers about an inventory the backend never passes it.
    """
    fake = _fake_ansible(monkeypatch)

    with pytest.raises(ScopeResolutionError, match="No inventory at"):
        resolve_scope(_ansible(str(tmp_path), group="web"))

    assert fake.calls == []


def test_unsupported_pattern_syntax_is_an_error_with_the_binary_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # ansible understands `:` and `!`. This resolver still refuses them, because the
    # scope of an exclusion is a guess and `:` also separates an IPv6 address.
    _fake_ansible(monkeypatch)

    with pytest.raises(ScopeResolutionError, match="unsupported syntax"):
        resolve_scope(_ansible(_project(tmp_path), group="web:db"))
    with pytest.raises(ScopeResolutionError, match="unsupported syntax"):
        resolve_scope(_ansible(_project(tmp_path), group="web,!web2.example.com"))


def test_a_pattern_that_matches_nothing_is_an_error_with_the_binary_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # ansible warns and exits 0 for a pattern that matches nothing, so the empty
    # answer has to raise here. A typo must never read as a safe no-op.
    _fake_ansible(monkeypatch)

    with pytest.raises(ScopeResolutionError, match="matches no host or group"):
        resolve_scope(_ansible(_project(tmp_path), group="staging"))


def test_a_resolved_set_past_the_ceiling_is_an_error_for_either_expander(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The ceiling belongs to the resolver, and not to one expander.

    #9's guard checks every resolved host and #14 lists them to a human, so a wider
    set is unreviewable. A ceiling that depended on which expander answered would be
    the drift #30 removes.
    """
    hosts = [f"web{number}.example.com" for number in range(2000)]
    _fake_ansible(monkeypatch, stdout=json.dumps({"web": {"hosts": hosts}}))

    with pytest.raises(ScopeResolutionError, match="refuses to expand"):
        resolve_scope(_ansible(_project(tmp_path), group="web"))


def test_resolve_scope_label_still_answers_when_ansible_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The new field is additive. The log-only record in server_execution.py reads the
    # label, and it must not learn about an expander to keep working.
    _fake_ansible(monkeypatch)

    assert resolve_scope_label(_ansible(_project(tmp_path), group="web")) == GROUP


def test_a_failed_ansible_run_leaves_the_label_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Never "one host". A failed expansion means the blast radius is unknown.
    _fake_ansible(monkeypatch, returncode=1, stderr="[ERROR]: broken\n")

    assert resolve_scope_label(_ansible(_project(tmp_path), group="web")) == UNRESOLVED
