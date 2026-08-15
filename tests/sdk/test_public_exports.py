# tests/sdk/test_public_exports.py
"""Every SDK symbol a caller needs must reach `from nanoinfra import ...`.

`nanoinfra/__init__.py` exports the SDK surface lazily, so an import of the package costs no
provider import. A symbol that the lazy map misses raises AttributeError, and the error names the
package rather than the omission, so the cause is not obvious to a caller.

#21 added `RemoteExecutionUnavailableError`. A caller must catch it to tell a missing executor from
a refused action, and that is exactly the case the SDK asks a caller to handle.
"""

from __future__ import annotations

import nanoinfra


def test_the_remote_execution_error_is_importable() -> None:
    """#21 tells a caller to catch this, so the package must hand it over."""
    from nanoinfra.sdk import types

    assert nanoinfra.RemoteExecutionUnavailableError is types.RemoteExecutionUnavailableError


def test_every_lazy_export_resolves() -> None:
    """A name in the map that no module holds would raise only at first use."""
    for name in nanoinfra.__all__:
        assert getattr(nanoinfra, name) is not None, name


def test_the_error_is_in_all() -> None:
    """`__all__` is what a star import and a reader both read."""
    assert "RemoteExecutionUnavailableError" in nanoinfra.__all__
