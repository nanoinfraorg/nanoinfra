# tests/config/test_gates_config.py
"""Item 5 (#7): the gates config block.

Config is the only source of authority. Reachability lists are not authority, so nothing
here reads allowFrom or the pairing store. Defaults are restrictive: an absent block, or a
block that fails to parse, must widen nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from nanoinfra.config.gates import GatesConfig
from nanoinfra.config.schema import Config


def test_absent_block_denies_every_unattended_remote_action() -> None:
    """An operator who writes no policy gets the restrictive one, not the permissive one."""
    gates = GatesConfig()

    assert gates.unattended.mutate_remote.host == "deny"
    assert gates.unattended.mutate_remote.group == "deny"
    assert gates.unattended.mutate_remote.all == "deny"
    assert gates.unattended.credential_access == "deny"


def test_absent_block_requires_approval_for_interactive_remote_actions() -> None:
    gates = GatesConfig()

    assert gates.interactive.mutate_remote.host == "approve"
    assert gates.interactive.mutate_remote.group == "approve"
    assert gates.interactive.mutate_remote.all == "deny"
    assert gates.interactive.credential_access == "approve"


def test_inventory_writes_are_allowed_interactively_and_denied_unattended() -> None:
    """An operator editing inventory does normal work. An automation doing it sets up a
    redirected grant, which is what #23 and #24 exist to stop."""
    gates = GatesConfig()

    assert gates.interactive.mutate_inventory == "allow"
    assert gates.unattended.mutate_inventory == "deny"


def test_all_scope_accepts_no_value_other_than_deny() -> None:
    """There is no runtime path to `all` anywhere in this design, so the schema refuses
    to express one rather than negotiate at evaluation time."""
    with pytest.raises(ValidationError):
        GatesConfig.model_validate(
            {"unattended": {"mutate.remote": {"host": "grant", "group": "grant", "all": "approve"}}}
        )


def test_an_unknown_key_inside_gates_fails_to_load_and_names_the_key() -> None:
    """Base sets no `extra` policy, so pydantic would ignore a typo by default. A mistyped
    allowlist key would then read as an empty allowlist instead of an error."""
    with pytest.raises(ValidationError) as excinfo:
        GatesConfig.model_validate({"standingGrant": []})

    assert "standingGrant" in str(excinfo.value)


def test_a_grant_with_an_unknown_field_fails_to_load() -> None:
    """A grant cannot name a capability class. It permits mutate.remote and nothing else,
    so a grant that tries to permit mutate.inventory has no field to say it in."""
    with pytest.raises(ValidationError):
        GatesConfig.model_validate(
            {"standingGrants": [{"hosts": ["h"], "commands": ["c"], "classes": ["mutate.inventory"]}]}
        )


def test_both_camel_case_and_snake_case_keys_load() -> None:
    camel = GatesConfig.model_validate(
        {"standingGrants": [{"hosts": ["web-01"], "commands": ["systemctl reload nginx"]}]}
    )
    snake = GatesConfig.model_validate(
        {"standing_grants": [{"hosts": ["web-01"], "commands": ["systemctl reload nginx"]}]}
    )

    assert camel.standing_grants[0].hosts == ["web-01"]
    assert snake.standing_grants[0].hosts == ["web-01"]


def test_a_grant_stores_commands_verbatim() -> None:
    """`commands` is an allowlist of exact resolved strings, never a pattern language.
    Patterns reintroduce the weakness that makes the existing pattern detection useless."""
    metacharacters = "systemctl restart nginx.*|rm -rf /"
    gates = GatesConfig.model_validate(
        {"standingGrants": [{"hosts": ["web-01"], "commands": [metacharacters]}]}
    )

    assert gates.standing_grants[0].commands == [metacharacters]


def test_a_grant_defaults_to_unattended_only() -> None:
    gates = GatesConfig.model_validate(
        {"standingGrants": [{"hosts": ["web-01"], "commands": ["uptime"]}]}
    )

    assert gates.standing_grants[0].contexts == ["unattended"]


def test_audit_defaults_to_digest_only_retention() -> None:
    """Resolved commands routinely embed secrets, so full text is an explicit opt-in."""
    gates = GatesConfig()

    assert gates.audit.record_command_text is False
    assert gates.audit.retention_days == 90


def test_approvers_load_as_channel_and_sender_pairs() -> None:
    gates = GatesConfig.model_validate(
        {"approvers": [{"channel": "webui", "sender": "operator-1"}]}
    )

    assert gates.approvers[0].channel == "webui"
    assert gates.approvers[0].sender == "operator-1"


def test_approval_paths_default_to_the_webui_path_only() -> None:
    """#13 needs to know which paths authenticate an approver, so the list is authority.

    `webui` qualifies today. It has a real session concept, and it supports trusted-proxy
    bootstrap auth (`7413ae89`). One path is also the common single-operator install, which
    is why #13 answers such a deployment with a named missing-path error.
    """
    assert GatesConfig().approval_paths == ["webui"]


def test_approval_paths_load_from_camel_case_and_snake_case() -> None:
    camel = GatesConfig.model_validate({"approvalPaths": ["webui", "telegram"]})
    snake = GatesConfig.model_validate({"approval_paths": ["webui", "telegram"]})

    assert camel.approval_paths == ["webui", "telegram"]
    assert snake.approval_paths == ["webui", "telegram"]


def test_a_mistyped_approval_paths_key_fails_to_load_and_names_the_key() -> None:
    """A typo must not fall back to the default list. That default would grant the WebUI
    path while the operator believes a second path is live."""
    with pytest.raises(ValidationError) as excinfo:
        GatesConfig.model_validate({"approvalPath": ["webui"]})

    assert "approvalPath" in str(excinfo.value)


def test_the_root_config_exposes_a_gates_block() -> None:
    config = Config.model_validate(
        {"gates": {"standingGrants": [{"hosts": ["web-01"], "commands": ["uptime"]}]}}
    )

    assert config.gates.standing_grants[0].commands == ["uptime"]


def test_the_root_config_defaults_gates_restrictively() -> None:
    assert Config().gates.unattended.mutate_remote.host == "deny"


def test_the_gates_module_never_reads_a_reachability_list() -> None:
    """Structural guard for the rule that config is authority. allowFrom exists so
    teammates can reach the bot, and the pairing store is mutable at runtime from chat.
    Neither may decide who approves a privileged action.

    Checks imports and attribute access, not prose: the module docstring names both lists
    in order to explain why it must not read them, and a text grep would punish that.
    """
    source = (Path(__file__).parents[2] / "nanoinfra/config/gates.py").read_text()
    tree = ast.parse(source)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = [name for name in imported if "channels" in name or "pairing" in name]

    accessed = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    reachability = {"allow_from", "allowFrom", "is_approved_sender", "approved_senders"}

    assert forbidden == []
    assert accessed & reachability == set()
