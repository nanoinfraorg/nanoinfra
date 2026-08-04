from __future__ import annotations

from nanoinfra.diagrams import __all__ as diagrams_all


def test_all_exports_are_importable():
    """Every name in __all__ must actually be importable from nanoinfra.diagrams."""
    import nanoinfra.diagrams as pkg

    for name in diagrams_all:
        assert hasattr(pkg, name), f"{name} is in __all__ but not exported"
