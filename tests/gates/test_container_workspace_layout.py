"""The container moves a pre-root workspace before it prepares anything under it.

The gateway migrates `~/.nanoinfra/workspace` to `workspaces/default` at startup. In a container
that is too late: `entrypoint.sh` prepares the credential store and the job store under the
workspace and starts three confined helpers against it first, so a move afterwards leaves the agent
on the new path and the executor on a name that no longer exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_the_fallback_workspace_is_the_shipped_default(script: str) -> None:
    """A shell cannot read config.json, so its fallback has to match what the code defaults to."""
    from nanoinfra.config.paths import default_workspace_path

    assert 'printf \'%s\' "$dir/workspaces/default"' in script
    assert default_workspace_path() == Path.home() / ".nanoinfra" / "workspaces" / "default"
    # The pre-root path is no longer a fallback. It appears once, in the migration's own
    # existence check, and nowhere else -- a second use would be a path the helpers could adopt.
    assert "printf '%s' \"$dir/workspace\"" not in script
    body = script[script.index("migrate_workspace_layout() {"):]
    body = body[: body.index("\n}\n")]
    assert script.count('"$dir/workspace"') == 1
    assert '"$dir/workspace"' in body


def test_the_migration_runs_before_the_workspace_is_used(script: str) -> None:
    call = script.index("        migrate_workspace_layout\n")
    resolve = script.index('workspace=$(resolve_workspace "$@")')
    prepare = script.index('if ! prepare_executor_paths "$workspace"')
    start = script.index('start_executor "$workspace"')
    assert call < resolve < prepare < start, "the move has to precede every use of the workspace"


def test_the_migration_reuses_the_tested_guards(script: str) -> None:
    """Not reimplemented in sh: the guards decide whether data moves."""
    body = script[script.index("migrate_workspace_layout() {"):]
    body = body[: body.index("\n}\n")]
    assert "from nanoinfra.config.workspace_migration import migrate_default_workspace" in body
    assert "mv " not in body and "shutil" not in body
    # As the agent account, which owns the config file the move rewrites.
    assert '--reuid="$agent_user"' in body
    # An operator who named a workspace is not migrated behind their back.
    assert 'NANOINFRA_WORKSPACE' in body


def test_the_migration_reports_what_it_did(script: str) -> None:
    body = script[script.index("migrate_workspace_layout() {"):]
    body = body[: body.index("\n}\n")]
    assert re.search(r"workspace moved: .*result\.source.*result\.target", body)
    assert "workspace layout unchanged" in body
