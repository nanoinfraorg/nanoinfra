# tests/cli/test_module_globals_are_restored.py
"""No test may write a module global of the product and leave it -- nanoinfraorg/nanoinfra#84.

`tests/cli/test_cli_input.py` assigned `terminal._prompt_session` directly, with no monkeypatch,
and nothing restored it. `_prompt_session` is a lazily built singleton: `_read_interactive_input_async`
reuses whatever it finds there rather than build one. The object that test left was constructed while
`Path.home()` was mocked, so every later test of that worker could have read a session pointed at a
directory that never existed.

**Two measurements shaped this file, and both narrowed the claim.**

A session-end scan of every `nanoinfra` module found **zero** attributes holding a mock after the
whole suite. So a guard that runs once at the end is the wrong instrument: a leak that a later test
overwrites is still a leak for every test in between, and this one is overwritten. That is why #80's
session guard was not widened to cover this.

Measured directly after the file, the global held a real `PromptSession` rather than a mock, because a
later test in the same file replaced it. So the defect is not "a mock survives". It is "a test writes a
product global by hand", and the shape is what this file checks.

The check reads the syntax tree, so it needs no ordering, no session and no timing. It catches the
shape before it can leak rather than after.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CLI_TESTS = Path(__file__).parent

# `monkeypatch.setattr` records the previous value and restores it at teardown, so a test that uses
# it cannot leave a product global changed. A bare assignment has no such record.
_ALLOWED = "monkeypatch.setattr"


def _product_module_aliases(tree: ast.Module) -> set[str]:
    """The local names that refer to a product **module** rather than to an object.

    ``from nanoinfra.cli import terminal`` binds a module, and an assignment to one of its
    attributes writes a product global. ``renderer = StreamRenderer(...)`` binds an instance, and an
    assignment to one of its attributes dies with the test. Only the first kind matters here.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nanoinfra"):
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module.startswith("nanoinfra"):
                continue
            for alias in node.names:
                # A name that starts lower case and holds no callable shape is a module by
                # convention here. The import machinery decides for real, and this test reads
                # source, so the check stays on the names a reader can see.
                if alias.name.islower() or (alias.asname or "").islower():
                    aliases.add(alias.asname or alias.name)
    return aliases


def _bare_writes_to_a_product_global(path: Path) -> list[str]:
    """Every assignment of the form ``<product module>.<attribute> = ...`` in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _product_module_aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            base = target.value
            if isinstance(base, ast.Name) and base.id in aliases:
                found.append(f"{path.name}:{node.lineno} {base.id}.{target.attr}")
    return found


def test_no_cli_test_writes_a_product_global_by_hand() -> None:
    """The shape, across the whole directory.

    A failure names the file and the line. The fix is one call: ``monkeypatch.setattr(module,
    "name", value)``, which records the previous value and restores it at teardown, so the restore
    is the framework's job and never the memory of whoever edits the test next.
    """
    offenders = [
        entry for path in sorted(_CLI_TESTS.glob("test_*.py")) for entry in
        _bare_writes_to_a_product_global(path)
    ]

    assert offenders == [], (
        "these tests write a product module global directly, so the value outlives the test:\n  "
        + "\n  ".join(offenders)
        + f"\nUse {_ALLOWED}(module, name, value) instead."
    )


def test_the_check_finds_the_shape_it_exists_for(tmp_path: Path) -> None:
    """A guard that reports nothing looks the same as a guard that cannot see.

    The sample is the exact code this issue removed, plus the two shapes that must stay legal: an
    attribute of a local instance, and a monkeypatch call.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "from nanoinfra.cli import terminal\n"
        "from nanoinfra.cli import stream as stream_mod\n"
        "\n"
        "def test_one(monkeypatch):\n"
        "    terminal._prompt_session = None\n"
        "    renderer = stream_mod.StreamRenderer()\n"
        "    renderer._live = None\n"
        "    monkeypatch.setattr(terminal, '_saved_term_attrs', None)\n",
        encoding="utf-8",
    )

    found = _bare_writes_to_a_product_global(sample)

    assert found == ["test_sample.py:5 terminal._prompt_session"]
