"""No dataclass field may hold a default that Python 3.11 refuses (#266).

This exists because of a release. `RequestContext.named_agents` was given
`MappingProxyType({})` as a field default -- read-only on purpose, so no context could mutate the
empty roster every other context was holding. Correct reasoning, and it raised at **import** time
on the minimum supported version:

    ValueError: mutable default <class 'mappingproxy'> for field named_agents
                is not allowed: use default_factory

3.11 rejects any default whose class is unhashable; the check narrowed to `list`/`dict`/`set` in
3.12. So on 3.11 every install was broken, in a module every tool imports -- and the development
machine ran 3.13, which meant nothing caught it until the tag was already pushed.

The fix was one word. What this test is for is the *class* of mistake: a shared immutable default
is a natural thing to reach for, `frozenset` and `tuple` are both fine, and the two that are not
(`mappingproxy`, and anything else with `__hash__ = None`) look exactly as safe. A version-specific
import failure is the worst shape of bug to find late, because it cannot be reproduced on the
machine that wrote it.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import warnings

import nanoinfra

#: Modules whose import is a side effect rather than a definition, or which need optional deps.
#: A module that cannot be imported here is not evidence of anything, so it is skipped rather than
#: failed -- the same reading `scripts/install_channel_dependencies.py` takes.
_SKIP = (".tests", "test_", ".conftest")


def _dataclasses_in_package() -> dict[str, type]:
    """Every dataclass reachable by importing the package, keyed by its qualified name."""
    found: dict[str, type] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for module in pkgutil.walk_packages(nanoinfra.__path__, "nanoinfra."):
            if any(part in module.name for part in _SKIP):
                continue
            try:
                imported = importlib.import_module(module.name)
            except Exception:
                continue
            for value in vars(imported).values():
                if isinstance(value, type) and dataclasses.is_dataclass(value):
                    found.setdefault(f"{value.__module__}.{value.__qualname__}", value)
    return found


def test_no_field_default_is_unhashable() -> None:
    """The rule 3.11 enforces, enforced on every version so it is found where it is written."""
    offenders: list[str] = []
    classes = _dataclasses_in_package()

    for name, cls in classes.items():
        for field in dataclasses.fields(cls):
            if field.default is dataclasses.MISSING:
                continue
            if field.default.__class__.__hash__ is None:
                offenders.append(
                    f"{name}.{field.name} defaults to a "
                    f"{type(field.default).__name__}; use default_factory"
                )

    # A guard that scanned nothing would pass forever, so the count is part of the assertion.
    assert len(classes) > 200, f"only {len(classes)} dataclasses scanned; the walk is broken"
    assert not offenders, "\n".join(offenders)
