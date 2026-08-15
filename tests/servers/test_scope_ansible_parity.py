# tests/servers/test_scope_ansible_parity.py
"""Item 28 (#30): the two expanders must agree, fixture by fixture.

nanoinfra/servers/scope.py reads an ansible inventory two ways. ansible's own
``ansible-inventory`` answers when ansible-core is installed, and the parser in
that module answers when it is not. Drift between them is the bypass class
nanoinfra/agent/tools/server_execution.py:44-53 documents: a guard that validates
one set of addresses while the backend dials another. #9 raised the stakes,
because it validates EVERY resolved host. An expander that names three hosts while
ansible targets five would guard the wrong three.

So this file runs both expanders over the same fixtures and compares the answers.
It is the only test that starts the real binary. Every other scope test fakes the
subprocess, because a test whose meaning depends on the machine proves nothing.

Two rules make the comparison honest:

* An error from both expanders counts as agreement. The two explain themselves in
  their own words, and the words are not the fact under test. The host set is.
* The whole file SKIPS when the binary is absent, and the skip reason says so. A
  consistency test that silently passes with nothing to compare is worse than no
  test at all, because it reads as evidence.

The fixtures below are the ones tests/servers/test_scope.py already uses. This
comparison already earned its place: it caught the parser reading a YAML list
under ``hosts``, a shape ansible itself refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.servers import scope
from nanoinfra.servers.scope import (
    EXPANDER_ANSIBLE,
    EXPANDER_PARSER,
    ScopeResolutionError,
    resolve_scope,
)
from nanoinfra.servers.types import Server

_SKIP_REASON = (
    "SKIPPED, not passed: the ansible-inventory binary is absent, so there is no "
    "authoritative expander to compare the in-repo parser against. This machine "
    "exercises the fallback path only. Install ansible-core to run this consistency "
    "check."
)

# The module's own detection decides. A separate check here could disagree with the
# resolver and then skip a test that would have run, or run one that cannot.
pytestmark = pytest.mark.skipif(
    scope._ansible_inventory_binary() is None,  # pyright: ignore[reportPrivateUsage]
    reason=_SKIP_REASON,
)

# What one expander answered. The error case carries no host set on purpose: the
# comparison asks whether the two agree, and two different sentences about the same
# refusal are still one agreement.
_ERROR = "ScopeResolutionError"

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

# One inventory and one pattern per case. Both a resolvable pattern and a refused
# one belong here: a resolver that agrees on the easy answers and disagrees on the
# refusals still hands the guard a host set ansible never targets.
_CASES: list[tuple[str, str | bytes, str]] = [
    ("ini-group", _INI_INVENTORY, "web"),
    ("ini-group-of-one", _INI_INVENTORY, "db"),
    ("ini-children", _INI_INVENTORY, "prod"),
    ("ini-all", _INI_INVENTORY, "all"),
    ("ini-glob", _INI_INVENTORY, "web*"),
    ("ini-ungrouped", _INI_INVENTORY, "ungrouped"),
    ("ini-one-host", _INI_INVENTORY, "web1.example.com"),
    ("ini-comma-union", _INI_INVENTORY, "web,db"),
    ("ini-unknown-group", _INI_INVENTORY, "staging"),
    ("ini-numeric-range", "[web]\nweb[01:03].example.com\n", "web"),
    ("ini-alphabetic-range", "[cache]\ncache-[a:c]\n", "cache"),
    ("ini-padded-range", "[webservers]\nweb-[01:14]\n", "webservers"),
    ("ini-inline-host-vars", "[web]\nweb1.example.com ansible_host=10.0.2.2\n", "web"),
    ("ini-empty-group", "[web]\n", "web"),
    ("ini-unbalanced-range", "[web]\nweb[1-3].example.com\n", "web"),
    ("ini-huge-range", "[web]\nweb[1:5000].example.com\n", "web"),
    ("yaml-group", _YAML_INVENTORY, "web"),
    ("yaml-children", _YAML_INVENTORY, "prod"),
    ("yaml-all", _YAML_INVENTORY, "all"),
    ("yaml-group-of-one", _YAML_INVENTORY, "db"),
    ("yaml-host-range", "web:\n  hosts:\n    web[1:2].example.com:\n", "web"),
    ("yaml-bare-host-name", "web:\n  hosts: web1.example.com\n", "web"),
    (
        "yaml-hosts-as-a-list",
        "web:\n  hosts:\n    - web1.example.com\n    - web2.example.com\n",
        "web",
    ),
    ("yaml-children-as-a-list", "web:\n  children:\n    - db\ndb:\n  hosts:\n    db1:\n", "web"),
    ("yaml-unknown-key", "web:\n  hostz:\n    web1.example.com:\n", "web"),
    ("yaml-recursive-anchor", "prod: &p\n  children:\n    inner: *p\n", "prod"),
    ("not-utf-8", b"[web]\n\xff\xfe not utf-8\n", "web"),
]


def _write_inventory(project: Path, content: str | bytes) -> None:
    path = project / "inventory"
    if isinstance(content, bytes):
        path.write_bytes(content)
        return
    path.write_text(content, encoding="utf-8")


def _ansible_server(project_path: Path, pattern: str) -> Server:
    return Server(
        id="a" * 32,
        name="parity-server",
        provider_id="ansible-runner",
        config={"projectPath": str(project_path), "group": pattern},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


def _outcome(server: Server) -> tuple[str, tuple[str, ...], str] | str:
    """The scope, the hosts, and the expander -- or the refusal."""
    try:
        resolution = resolve_scope(server)
    except ScopeResolutionError:
        return _ERROR
    return resolution.scope, resolution.hosts, resolution.expander


def _answer(outcome: tuple[str, tuple[str, ...], str] | str) -> object:
    """The part the two expanders must agree on. The expander name is not part of it."""
    if isinstance(outcome, str):
        return outcome
    return outcome[0], outcome[1]


def _compare(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve twice over one server, and hold the two answers to each other."""
    ansible_answered = _outcome(server)
    # Hide the binary, so the same server takes the fallback path. Nothing else
    # changes: the inventory file, the pattern, and every rule around the expansion
    # stay identical.
    monkeypatch.setattr(scope, "_ansible_inventory_binary", lambda: None)
    parser_answered = _outcome(server)

    assert _answer(ansible_answered) == _answer(parser_answered), (
        f"the two expanders disagree: ansible said {ansible_answered!r} and the in-repo "
        f"parser said {parser_answered!r}. One of them describes hosts the backend never "
        "targets, and #9's guard checks whichever one answers."
    )
    # Each answer must come from the expander it claims. Otherwise a comparison of
    # the fallback against itself would pass and prove nothing.
    if isinstance(ansible_answered, tuple):
        assert ansible_answered[2] == EXPANDER_ANSIBLE
    if isinstance(parser_answered, tuple):
        assert parser_answered[2] == EXPANDER_PARSER


@pytest.mark.parametrize(
    ("inventory", "pattern"),
    [(inventory, pattern) for _, inventory, pattern in _CASES],
    ids=[case_id for case_id, _, _ in _CASES],
)
def test_both_expanders_answer_the_same_way(
    inventory: str | bytes, pattern: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inventory(tmp_path, inventory)

    _compare(_ansible_server(tmp_path, pattern), monkeypatch)


def test_both_expanders_read_the_same_files_in_an_inventory_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ansible skips some file names inside an inventory directory. A parser that
    # merged a file ansible ignores would name a host nothing dials.
    inventory = tmp_path / "inventory"
    inventory.mkdir()
    (inventory / "01-web").write_text("[web]\nweb1.example.com\n", encoding="utf-8")
    (inventory / "02-web-more").write_text("[web]\nweb2.example.com\n", encoding="utf-8")
    (inventory / "notes.md").write_text("[web]\nignored.example.com\n", encoding="utf-8")

    _compare(_ansible_server(tmp_path, "web"), monkeypatch)


def test_both_expanders_refuse_a_directory_holding_one_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A silent skip would hide every host in the broken file. One unreadable file
    # leaves the host set unknown, so both expanders must refuse the whole set.
    inventory = tmp_path / "inventory"
    inventory.mkdir()
    (inventory / "01-web").write_text("[web]\nweb1.example.com\n", encoding="utf-8")
    (inventory / "02-broken").write_text("[web]\nweb[1-3].example.com\n", encoding="utf-8")

    _compare(_ansible_server(tmp_path, "web"), monkeypatch)


def test_the_real_run_writes_nothing_into_the_project_path(tmp_path: Path) -> None:
    """The acceptance criterion for the artifact rule, with the real binary.

    ansible_runner.get_inventory() would build an artifact tree under the directory
    it is given, and projectPath belongs to the operator. A resolve reads.
    """
    _write_inventory(tmp_path, _INI_INVENTORY)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    resolution = resolve_scope(_ansible_server(tmp_path, "web"))

    assert resolution.expander == EXPANDER_ANSIBLE
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert before == [Path("inventory")]
