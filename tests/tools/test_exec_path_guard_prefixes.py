"""The workspace path guard must see a path whatever shell punctuation precedes it.

``_extract_absolute_paths`` feeds the deny decision in ``ExecTool``. A path the
regex does not extract is never checked, so a missing prefix character is a
silent bypass rather than a cosmetic gap. See nanoinfraorg/nanoinfra#134.
"""

from __future__ import annotations

import pytest

from nanoinfra.agent.tools.shell import ExecTool

# Each case is a command whose absolute path is preceded by punctuation rather than a space.
# These all extracted nothing before the fix.
PREFIX_CASES = [
    pytest.param("cat </etc/shadow", "/etc/shadow", id="input-redirect"),
    pytest.param("tee </etc/shadow", "/etc/shadow", id="input-redirect-tee"),
    pytest.param("rsync src:/etc/shadow .", "/etc/shadow", id="colon-remote"),
    pytest.param("scp h:/etc/shadow /tmp/x", "/etc/shadow", id="colon-scp"),
    pytest.param("$(</etc/shadow)", "/etc/shadow", id="subshell-paren"),
    pytest.param("cp x,/etc/shadow", "/etc/shadow", id="comma"),
    pytest.param("cat >/etc/shadow", "/etc/shadow", id="output-redirect"),
    pytest.param("cat /etc/shadow", "/etc/shadow", id="plain-space"),
]


@pytest.mark.parametrize(("command", "expected"), PREFIX_CASES)
def test_absolute_path_is_extracted_after_punctuation(command: str, expected: str) -> None:
    assert expected in ExecTool._extract_absolute_paths(command)


def test_brace_expansion_is_extracted() -> None:
    """A brace list yields one token; the guard only needs to see the absolute path in it."""
    extracted = ExecTool._extract_absolute_paths("cat {/etc/shadow,/tmp/y}")
    assert any(path.startswith("/etc/shadow") for path in extracted)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("(cat /etc/shadow)", "/etc/shadow"),
        ("cat $(cat /etc/shadow)", "/etc/shadow"),
        ("{ cat /etc/shadow; }", "/etc/shadow"),
    ],
)
def test_trailing_shell_punctuation_is_stripped(command: str, expected: str) -> None:
    """Before the fix these extracted '/etc/shadow)' -- still blocked, but the wrong path."""
    assert expected in ExecTool._extract_absolute_paths(command)


def test_home_shortcut_is_extracted_after_punctuation() -> None:
    assert "~/x" in ExecTool._extract_absolute_paths("cat <~/x")


def test_posix_double_slash_is_not_reported_as_an_absolute_path() -> None:
    """'//host/share' is implementation-defined on POSIX and is not our absolute path."""
    assert "//server/share" not in ExecTool._extract_absolute_paths("cat //server/share")


def test_relative_paths_are_still_ignored() -> None:
    """Widening the prefix class must not start extracting relative paths."""
    assert ExecTool._extract_absolute_paths("cat etc/shadow") == []
    assert ExecTool._extract_absolute_paths("echo a,b,c") == []
