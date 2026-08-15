# tests/test_installation_isolation.py
"""No test may reach the installation the operator runs -- nanoinfraorg/nanoinfra#45.

`get_config_path()` reads `Path.home() / ".nanoinfra"`, and for a long time no fixture pointed
that anywhere else. So a test that touched a store wrote into the deployment on the developer's
own machine. Four kinds of contamination came out of one session:

1. Four pending pairing requests, on `mattermost` and `signal`, in the real `pairing.json`. The
   WebUI then showed a pairing dialog for a channel the operator does not run.
2. A latch on the session id `s1`, in the real audit log, so the operator read a latch banner
   for a session that never existed.
3. Two helper sockets under the real `run` directory, which would collide with the next real
   gateway start.
4. Ten orphan children, two of which held those sockets.

Each one was invisible until the operator saw it in their own UI. That is the reason this file
holds a guard rather than a note in a contributing document.

The fixtures live in the root `conftest.py`, and this file reads the properties they give. It
reads them through fixtures rather than an import, because `tests` holds no `__init__.py` and
several directories each carry a module called `conftest`.
"""

from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nanoinfra.config import paths

if TYPE_CHECKING:
    from conftest import InstallationWriteGuard


def test_the_home_of_a_test_is_not_the_home_of_the_operator(real_nanoinfra_dir: Path) -> None:
    assert Path.home().resolve() != real_nanoinfra_dir.parent


def test_every_path_helper_resolves_away_from_the_real_installation(
    real_nanoinfra_dir: Path,
) -> None:
    """The property covers each helper, and not only `get_config_path`.

    Some helpers read the config path and some read `Path.home()` directly, so one assertion
    about one helper would leave the other kind unproven.
    """
    resolved = {
        "get_config_path": paths.get_config_path(),
        "get_data_dir": paths.get_data_dir(),
        "get_cron_dir": paths.get_cron_dir(),
        "get_logs_dir": paths.get_logs_dir(),
        "get_media_dir": paths.get_media_dir(),
        "get_webui_dir": paths.get_webui_dir(),
        "get_workspace_path": paths.get_workspace_path(),
        "get_cli_history_path": paths.get_cli_history_path(),
        "get_legacy_sessions_dir": paths.get_legacy_sessions_dir(),
    }

    reached: dict[str, Path] = {}
    for name, path in resolved.items():
        absolute = path.resolve()
        if real_nanoinfra_dir == absolute or real_nanoinfra_dir in absolute.parents:
            reached[name] = absolute

    assert not reached, (
        f"these helpers resolve into the operator's own installation: {reached}. A store built "
        "on one of them writes into the deployment the operator runs."
    )


def test_the_config_path_global_does_not_leak_into_the_next_test() -> None:
    """A test that points the global at its own directory leaves nothing behind.

    ``set_config_path`` writes a module level value. Two tests in one worker share that value,
    so a leak points the second test at a directory the first one deleted.
    """
    from nanoinfra.config import loader

    before = loader.get_config_path()
    loader.set_config_path(Path("/nonexistent/leaked/config.json"))

    assert loader.get_config_path() != before
    # The autouse fixture restores the value at teardown. The test below reads the same global
    # and holds whichever order the two run in, because a restore happens after every test and
    # not only after this one.


def test_the_config_path_global_carries_no_value_from_another_test() -> None:
    assert paths.get_config_path() != Path("/nonexistent/leaked/config.json")
    assert Path.home().resolve() in paths.get_config_path().resolve().parents


def test_a_child_process_reads_the_isolated_home(real_nanoinfra_dir: Path) -> None:
    """A subprocess must not reach the real installation either.

    The four contaminations included two sockets a real helper child bound, so an in-process
    guard alone would have missed half of them.
    """
    completed = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == str(Path.home())
    assert completed.stdout.strip() != str(real_nanoinfra_dir.parent)


def test_a_child_process_still_finds_a_user_installed_package(real_user_base: str) -> None:
    """The isolation covers where nanoinfra writes, and never where Python finds packages.

    A fresh HOME removes the user site directory from a child's ``sys.path``. On a
    ``pip --user`` install that hides `ansible`, and 37 tests in ``tests/servers`` and
    ``tests/gates`` compare the in-repo parser against the real `ansible-inventory` binary.
    Those tests failed for that reason alone while this fixture was under development, and the
    failure read as a disagreement between the two expanders.
    """
    completed = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getuserbase())"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == real_user_base
    assert os.environ["PYTHONUSERBASE"] == real_user_base


def test_the_user_base_is_a_real_directory(real_user_base: str) -> None:
    """The fixture passes a value, and a wrong value would be silent."""
    assert Path(real_user_base).is_absolute()
    assert site.getuserbase() == real_user_base


def test_the_captured_terminal_width_does_not_depend_on_a_tmp_path(tmp_path: Path) -> None:
    """A message assertion must not break because pytest chose a longer directory.

    rich reads COLUMNS and its default of 80 wraps a long path in the middle of a file name. A
    ``tmp_path`` under xdist carries an extra ``popen-gw0`` segment, so two assertions about a
    config file name passed serially and failed under ``-n auto``. The cause was the width and
    never the two tests.
    """
    assert int(os.environ["COLUMNS"]) >= 200
    assert len(str(tmp_path / "config.json")) < int(os.environ["COLUMNS"])


def test_the_guard_sees_a_write_to_the_real_installation(
    installation_write_guard: InstallationWriteGuard,
) -> None:
    """The guard itself needs proof, because a guard that reports nothing looks identical.

    The call runs the hook by hand. An actual write into the real installation would be the
    contamination this file exists to stop, so no test may create one to prove the record works.
    """
    installation_write_guard.record(
        "open", (str(installation_write_guard.real_installation / "pairing.json"), "w", 0)
    )

    assert len(installation_write_guard.own_records()) == 1


def test_the_hook_is_installed_in_this_session(
    installation_write_guard: InstallationWriteGuard,
) -> None:
    """The test above proves the logic, and this one proves the session holds the hook.

    A hook that no session installed would leave every other assertion here true and every
    contamination unreported.

    The call reaches the real audit machinery and writes nothing. Python raises an audit event
    **before** the operation, so a remove of a name that does not exist records the attempt and
    then fails, whatever the state of the operator's own directory.
    """
    absent = installation_write_guard.real_installation / "no-such-file-probe"

    with pytest.raises(FileNotFoundError):
        os.remove(absent)

    assert installation_write_guard.own_records() == [
        (
            "tests/test_installation_isolation.py::test_the_hook_is_installed_in_this_session",
            str(absent),
        )
    ], "the record must name the test, or an operator has to bisect the suite by hand"
    assert not absent.exists()


@pytest.mark.parametrize(
    ("event", "argument"),
    [
        ("open", None),
        ("open", "relative.json"),
        ("open", str(Path.home() / "elsewhere.json")),
        ("os.mkdir", "{real}-backup/run"),
        ("os.stat", "{real}/config.json"),
    ],
    ids=["no-path", "relative", "another-home", "a-similar-name", "a-read"],
)
def test_the_guard_stays_quiet_for_everything_else(
    event: str,
    argument: str | None,
    installation_write_guard: InstallationWriteGuard,
) -> None:
    """A guard that fires on a read, or on a similarly named directory, would be noise.

    ``~/.nanoinfra-backup`` is the case a prefix match alone would accept, and a read
    contaminates nothing.
    """
    target = (
        argument.format(real=installation_write_guard.real_installation)
        if argument is not None
        else None
    )
    args = (target, "w", 0) if event == "open" else (target,)

    installation_write_guard.record(event, args)

    assert installation_write_guard.own_records() == []
