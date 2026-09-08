"""`**` in a search glob has to span zero or more directories (upstream PR 5692).

`_match_glob` handed slash-bearing patterns to `PurePosixPath.match`, which treats `**` as a
plain `*`: exactly one segment, never zero and never two. So `**/*.py` missed every root-level
file, `src/**` reached one level down, `src/**/*.py` matched nothing at all -- and both tool
schemas advertise `'tests/**/test_*.py'` to the model, a pattern that only matched paths with
exactly one directory in between.
"""

from __future__ import annotations

from pathlib import Path

from nanoinfra.agent.tools.search import FindFilesTool, GrepTool, _match_glob

#: The example both tool schemas give the model.
_SCHEMA_EXAMPLE = "tests/**/test_*.py"


def _matches(rel_path: str, pattern: str) -> bool:
    return _match_glob(rel_path, rel_path.rsplit("/", 1)[-1], pattern)


def test_a_double_star_spans_zero_segments() -> None:
    assert _matches("a.py", "**/*.py")
    assert _matches("src/a.py", "src/**/*.py")
    assert _matches("src/a.py", "src/**")


def test_a_double_star_spans_more_than_one_segment() -> None:
    assert _matches("src/deep/nested/a.py", "**/*.py")
    assert _matches("src/deep/nested/a.py", "src/**/*.py")
    assert _matches("src/deep/nested/a.py", "src/**")


def test_the_schema_example_matches_at_every_depth() -> None:
    """The model is told to use this one, so it has to work at every depth."""
    assert _matches("tests/test_a.py", _SCHEMA_EXAMPLE)
    assert _matches("tests/unit/test_a.py", _SCHEMA_EXAMPLE)
    assert _matches("tests/unit/deep/test_a.py", _SCHEMA_EXAMPLE)
    assert not _matches("tests/unit/helpers.py", _SCHEMA_EXAMPLE)
    assert not _matches("docs/test_a.md", _SCHEMA_EXAMPLE)


def test_patterns_that_already_worked_still_match() -> None:
    """The plain cases, and the unanchored match `PurePosixPath.match` gave a path pattern.

    A path pattern matched from the right, so `src/*.py` reached a nested `src`. Keeping that
    makes the new behaviour a superset: no pattern that matched before stops matching.
    """
    assert _matches("src/deep/a.py", "*.py")
    assert not _matches("src/deep/a.ts", "*.py")
    assert _matches("src/settings_view.tsx", "src/*")
    assert _matches("vendor/src/a.py", "src/*.py")
    assert _matches("vendor/src/deep/a.py", "src/**/*.py")
    assert not _matches("src/a.py", "")


async def test_find_files_finds_the_schema_example_at_every_depth(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit" / "deep").mkdir(parents=True)
    (tmp_path / "tests" / "test_a.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests" / "unit" / "test_b.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests" / "unit" / "deep" / "test_c.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests" / "unit" / "helpers.py").write_text("pass\n", encoding="utf-8")

    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(path=".", glob=_SCHEMA_EXAMPLE)

    assert sorted(result.splitlines()) == [
        "tests/test_a.py",
        "tests/unit/deep/test_c.py",
        "tests/unit/test_b.py",
    ]


async def test_grep_finds_the_schema_example_at_every_depth(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "test_a.py").write_text("match_here\n", encoding="utf-8")
    (tmp_path / "tests" / "unit" / "test_b.py").write_text("match_here\n", encoding="utf-8")
    (tmp_path / "tests" / "unit" / "helpers.py").write_text("match_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="match_here", path=".", glob=_SCHEMA_EXAMPLE)

    assert sorted(result.splitlines()) == ["tests/test_a.py", "tests/unit/test_b.py"]
