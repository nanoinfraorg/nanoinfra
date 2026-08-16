# tests/test_environment_is_not_stripped.py
"""A run must leave the environment it found -- nanoinfraorg/nanoinfra#90.

#89 spent four hypotheses on one question, and three of them were asserted before being measured. The
answer was `uv sync --all-extras --dev`, which makes an environment match the lock and therefore
removes every channel SDK, because those are installed outside the lock from each channel manifest.
`AGENTS.md` already prescribes the reinstall that undoes it, and a document cannot notice when somebody
runs the first line alone.

**Two states were invisible while they were wrong, and a developer reads both as normal:**

- `basedpyright` reports 0 errors with the channel SDKs present and **1425** without them, all from
  unresolved imports in `nanoinfra/channels/*/runtime.py`. A real new type error hides inside that wall,
  and grepping the output for one's own files is not a substitute. A broken `_handle_message` override
  reached CI that way once.
- The bare suite collects **8366 passed** with them and **8009 passed with 8 more skips** without them.
  A suite that quietly stops collecting 357 tests still prints green.

So the property is presence and absence, and it is checked rather than documented.
"""

from __future__ import annotations

import pytest


def test_a_missing_distribution_fails_the_run(environment_guard) -> None:
    """The case #89 chased: something removed a package and every surface stayed quiet."""
    problem = environment_guard.compare(
        before={"nanoinfra", "discord.py", "qrcode"},
        after={"nanoinfra", "qrcode"},
    )

    assert problem is not None
    assert "discord.py" in problem
    assert "install_channel_dependencies --all-channels" in problem, (
        "the message must carry the recovery, because it is one line and nobody should hunt for it"
    )


def test_an_added_distribution_fails_the_run_too(environment_guard) -> None:
    """The other half of the property.

    A test that installs a package is as wrong as one that removes a package, and
    `enable_optional_feature` can reach a real installer, so the guard watches both directions.
    """
    problem = environment_guard.compare(
        before={"nanoinfra"},
        after={"nanoinfra", "some-package-a-test-installed"},
    )

    assert problem is not None
    assert "some-package-a-test-installed" in problem


def test_a_run_that_changed_nothing_reports_nothing(environment_guard) -> None:
    problem = environment_guard.compare(before={"nanoinfra", "qrcode"}, after={"qrcode", "nanoinfra"})

    assert problem is None


def test_a_version_change_is_not_a_problem(environment_guard) -> None:
    """An upgrade during a session is a legitimate thing a developer does.

    A guard that shouted about a version would be turned off, and then it would catch nothing at all.
    The property is which distributions exist, and never which versions.
    """
    problem = environment_guard.compare(before={"httpx"}, after={"httpx"})

    assert problem is None


def test_the_names_are_compared_in_one_normal_form(environment_guard) -> None:
    """`Discord.py` and `discord-py` name one distribution.

    A guard that reported a rename it invented itself would train a reader to ignore it.
    """
    problem = environment_guard.compare(before={"Discord.PY"}, after={"discord-py"})

    assert problem is None


def test_the_guard_reads_the_live_environment(environment_guard) -> None:
    """The comparison is pure, and the reader of the real environment must work as well.

    A pure function that nothing feeds is a function that guards nothing.
    """
    names = environment_guard.installed_names()

    assert "pytest" in names
    assert len(names) > 20


@pytest.mark.parametrize("direction", ["removed", "added"])
def test_the_message_names_the_direction(environment_guard, direction: str) -> None:
    """A reader must not have to work out whether something went or arrived."""
    if direction == "removed":
        problem = environment_guard.compare(before={"a", "b"}, after={"a"})
    else:
        problem = environment_guard.compare(before={"a"}, after={"a", "b"})

    assert problem is not None
    assert direction in problem.lower()
