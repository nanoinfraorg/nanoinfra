"""One identity, one workspace: the key, the directory, and the boundary.

The last test in this file scans call sites rather than listing them. A list is
what let the explorer route ship without a signature (v0.17.5): two callers built
the same payload and only one of them was in the list somebody maintained by hand.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from nanoinfra.webui.identity_workspaces import (
    IDENTITY_DIR_PREFIX,
    ensure_identity_workspace,
    identity_dirname,
    identity_workspace_key,
    identity_workspace_path,
    is_identity_dirname,
    read_identity_index,
    record_identity,
)

GOOGLE = "https://accounts.google.com"
DEX = "http://dex.localhost:5556/dex"


class TestTheKey:
    def test_the_same_person_at_one_provider_is_one_key(self) -> None:
        assert identity_workspace_key(GOOGLE, "1029384756") == identity_workspace_key(
            GOOGLE, "1029384756"
        )

    def test_one_subject_at_two_providers_is_two_keys(self) -> None:
        """Fail closed. An account at either provider must not reach the other's files."""
        assert identity_workspace_key(GOOGLE, "42") != identity_workspace_key(DEX, "42")

    def test_no_subject_is_no_key(self) -> None:
        """A token that verifies without the claim gets the shared workspace, not a refusal."""
        assert identity_workspace_key(GOOGLE, "") == ""
        assert identity_workspace_key(GOOGLE, "   ") == ""

    def test_surrounding_space_does_not_make_a_second_person(self) -> None:
        assert identity_workspace_key(GOOGLE, " 42 ") == identity_workspace_key(GOOGLE, "42")


class TestTheDirectory:
    def test_the_name_is_stable(self) -> None:
        key = identity_workspace_key(GOOGLE, "1029384756")
        assert identity_dirname(key) == identity_dirname(key)

    def test_the_name_is_not_the_address(self) -> None:
        """A directory named from the email is one a rename orphans."""
        key = identity_workspace_key(GOOGLE, "1029384756")
        assert "@" not in identity_dirname(key)
        assert "1029384756" not in identity_dirname(key)

    def test_the_name_is_filesystem_safe_and_recognisable(self) -> None:
        name = identity_dirname(identity_workspace_key(DEX, "CiQxMTExMTExMQ"))
        assert name.startswith(IDENTITY_DIR_PREFIX)
        assert name.replace(IDENTITY_DIR_PREFIX, "").isalnum()
        assert is_identity_dirname(name)

    def test_a_hand_made_workspace_is_not_mistaken_for_one(self) -> None:
        assert not is_identity_dirname("default")
        assert not is_identity_dirname("u-")
        assert not is_identity_dirname("u-not-base32!")

    def test_an_empty_key_names_no_directory(self) -> None:
        with pytest.raises(ValueError):
            identity_dirname("")


class TestTheIndex:
    def test_creation_is_idempotent(self, tmp_path: Path) -> None:
        key = identity_workspace_key(GOOGLE, "42")
        first = ensure_identity_workspace(tmp_path, key, name="a@example.com")
        second = ensure_identity_workspace(tmp_path, key, name="a@example.com")
        assert first == second
        assert first.is_dir()

    def test_the_directory_is_not_world_readable(self, tmp_path: Path) -> None:
        path = ensure_identity_workspace(
            tmp_path, identity_workspace_key(GOOGLE, "42"), name="a@example.com"
        )
        assert path.stat().st_mode & 0o077 == 0

    def test_a_rename_keeps_the_directory_and_updates_the_label(self, tmp_path: Path) -> None:
        """The address is mutable and the subject is not, so the subject decides."""
        key = identity_workspace_key(GOOGLE, "42")
        before = ensure_identity_workspace(tmp_path, key, name="old@example.com")
        after = ensure_identity_workspace(tmp_path, key, name="new@example.com")
        assert before == after
        entry = read_identity_index(tmp_path)[identity_dirname(key)]
        assert entry["identity"] == "new@example.com"
        assert entry["first_seen"] <= entry["last_seen"]

    def test_the_index_records_both_halves_of_the_key(self, tmp_path: Path) -> None:
        """An operator has to be able to prove whose directory this is."""
        record_identity(tmp_path, identity_workspace_key(GOOGLE, "42"), name="a@example.com")
        entry = read_identity_index(tmp_path)[
            identity_dirname(identity_workspace_key(GOOGLE, "42"))
        ]
        assert entry["issuer"] == GOOGLE
        assert entry["subject"] == "42"

    def test_a_damaged_index_is_a_label_and_never_an_authority(self, tmp_path: Path) -> None:
        """The directory comes from the key every time, so a broken file changes nothing."""
        key = identity_workspace_key(GOOGLE, "42")
        expected = identity_workspace_path(tmp_path, key)
        (tmp_path / ".identities.json").write_text("{not json", encoding="utf-8")
        assert read_identity_index(tmp_path) == {}
        assert ensure_identity_workspace(tmp_path, key, name="a@example.com") == expected

    def test_a_missing_index_reads_empty(self, tmp_path: Path) -> None:
        assert read_identity_index(tmp_path) == {}

    def test_the_index_is_json_an_operator_can_read(self, tmp_path: Path) -> None:
        record_identity(tmp_path, identity_workspace_key(DEX, "abc"), name="a@example.com")
        raw = (tmp_path / ".identities.json").read_text(encoding="utf-8")
        assert json.loads(raw)
        assert raw.endswith("\n")


def test_every_workspace_scope_call_site_names_a_carrier() -> None:
    """Scan, do not list.

    ``workspace_scope_for`` decides which root a caller may choose inside. A call
    that omits the carrier would widen that root back to every identity's, and it
    would do so silently. So this asserts the shape of every call in the tree
    instead of trusting a list somebody has to remember to update.
    """
    root = Path(__file__).resolve().parents[2] / "nanoinfra"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "workspace_scope_for":
                continue
            if len(node.args) < 2:
                offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    assert not offenders, f"workspace_scope_for called without a carrier: {offenders}"


def test_the_identity_root_holds_a_default_workspace(tmp_path: Path) -> None:
    """A root holds workspaces. Point the root and the workspace at one directory and
    the person's own workspace is the one thing their switcher cannot list."""
    from nanoinfra.webui.identity_workspaces import IDENTITY_DEFAULT_WORKSPACE

    own_root = ensure_identity_workspace(
        tmp_path, identity_workspace_key(GOOGLE, "42"), name="a@example.com"
    )
    assert (own_root / IDENTITY_DEFAULT_WORKSPACE).is_dir()
    assert [p.name for p in own_root.iterdir()] == [IDENTITY_DEFAULT_WORKSPACE]
