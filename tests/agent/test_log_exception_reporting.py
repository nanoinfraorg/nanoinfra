"""``exc_info=True`` records no traceback under loguru.

loguru takes keyword arguments as ``{}`` format bindings, not as stdlib
``logging`` options, so ``exc_info=True`` was accepted, unused and dropped --
every one of these sites believed it was keeping an exception's traceback and
was keeping only the message. The equivalents are ``logger.exception(...)``,
which logs at ERROR, and ``logger.opt(exception=True).<level>(...)`` where the
site's own level has to survive.

Each test below drives a real call site and asserts on captured output, because
the call was always made; it was the output that was wrong.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from loguru import logger

import nanoinfra
from nanoinfra.bus.queue import MessageBus
from nanoinfra.channels.matrix.runtime import MatrixChannel
from nanoinfra.diagrams import write_gate
from nanoinfra.session.manager import SessionManager


def _sink(level: str) -> tuple[io.StringIO, int]:
    buf = io.StringIO()
    return buf, logger.add(buf, format="{message}", level=level, backtrace=True)


def test_session_flush_failure_records_its_traceback(tmp_path) -> None:
    """``session/manager.py`` -- ``logger.opt(exception=True).warning``."""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("cli:flush")

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk-is-gone-sentinel")

    sessions.save = _explode  # type: ignore[method-assign]
    sessions._cache["cli:flush"] = session

    buf, handler = _sink("WARNING")
    try:
        assert sessions.flush_all() == 0
    finally:
        logger.remove(handler)

    out = buf.getvalue()
    assert "Failed to flush session cli:flush" in out
    assert "Traceback (most recent call last)" in out
    assert "disk-is-gone-sentinel" in out


def test_diagram_audit_failure_records_its_traceback(monkeypatch) -> None:
    """``diagrams/write_gate.py`` -- warning level is load-bearing there.

    The docstring on ``record_diagram_write`` says the failure is logged and
    does not stop the write, so the level must stay WARNING rather than become
    ERROR via ``logger.exception``.
    """
    store = MagicMock()
    store.record.side_effect = RuntimeError("audit-store-sentinel")
    monkeypatch.setattr(write_gate, "_audit_store", lambda: store)

    buf, handler = _sink("WARNING")
    try:
        write_gate.record_diagram_write(diagram_id="d1", tool="update_diagram", summary="s")
    finally:
        logger.remove(handler)

    out = buf.getvalue()
    assert "Could not record the audit entry for a diagram write" in out
    assert "Traceback (most recent call last)" in out
    assert "audit-store-sentinel" in out


def test_diagram_audit_failure_stays_at_warning(monkeypatch) -> None:
    store = MagicMock()
    store.record.side_effect = RuntimeError("audit-store-sentinel")
    monkeypatch.setattr(write_gate, "_audit_store", lambda: store)

    buf, handler = _sink("ERROR")
    try:
        write_gate.record_diagram_write(diagram_id="d1", tool="update_diagram", summary="s")
    finally:
        logger.remove(handler)

    assert buf.getvalue() == ""


@pytest.mark.asyncio
async def test_matrix_stream_failure_interpolates_and_records(monkeypatch) -> None:
    """``channels/matrix/runtime.py`` -- both bugs at one site.

    The message carried a printf ``%s`` for ``chat_id`` as well, so the room
    the send failed for was dropped from the line that reported it.
    """
    channel = MatrixChannel({}, MessageBus())

    async def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("matrix-send-sentinel")

    monkeypatch.setattr(channel, "_send_room_content", _explode)

    buf, handler = _sink("ERROR")
    try:
        await channel.send_delta("!room:example.org", "hello", stream_id="s1")
    finally:
        logger.remove(handler)

    out = buf.getvalue()
    assert "Stream send/edit failed for chat_id=!room:example.org" in out
    assert "%s" not in out
    assert "Traceback (most recent call last)" in out
    assert "matrix-send-sentinel" in out


# ---------------------------------------------------------------------------
# Regression guards. The behavioural tests above cover four of the sites; these
# keep the other nine from drifting back, and keep new ones from arriving.
# ---------------------------------------------------------------------------

_LOGGER_ATTRS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "critical", "exception", "log"}
)

# Known remaining offender, outside this change's file scope. `violations` is
# asserted to be a subset rather than equal to it, so fixing it does not fail
# this test.
#: Empty, and it should stay that way. A name appearing here means a regression.
#:
#: `webui/version_check.py` was once listed as the last outstanding offender. It is not an
#: offender at all -- that module uses stdlib `logging`, where `exc_info=True` is the correct
#: spelling -- and the guard already skips stdlib modules, so it needs no entry. Verified the hard
#: way: rewriting it to `logger.opt(exception=True)` type-checks as
#: `Cannot access attribute "opt" for class "Logger"` and would have raised at runtime the first
#: time a version check failed.
_EXC_INFO_ALLOWLIST: frozenset[str] = frozenset()

# stdlib `logging` is correct with printf placeholders; these modules use it.
_STDLIB_LOGGING_DIRS = ("gates/",)


def _package_sources() -> list[Path]:
    root = Path(nanoinfra.__file__).parent
    return sorted(p for p in root.rglob("*.py") if "tests" not in p.parts)


def _is_logger_call(node: ast.Call) -> bool:
    """Match ``*.debug(...)`` and friends, incl. ``logger.opt(...).debug(...)``."""
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _LOGGER_ATTRS


def _uses_stdlib_logging(text: str) -> bool:
    return "import logging" in text and "from loguru" not in text


def test_no_loguru_call_passes_exc_info() -> None:
    root = Path(nanoinfra.__file__).parent
    violations: set[str] = set()
    for path in _package_sources():
        text = path.read_text(encoding="utf-8")
        if "exc_info" not in text or _uses_stdlib_logging(text):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and _is_logger_call(node):
                if any(kw.arg == "exc_info" for kw in node.keywords):
                    violations.add(path.relative_to(root).as_posix())

    assert violations <= _EXC_INFO_ALLOWLIST, (
        f"loguru drops exc_info; use logger.exception or logger.opt(exception=True): {violations}"
    )


def test_no_loguru_message_uses_printf_placeholders() -> None:
    root = Path(nanoinfra.__file__).parent
    violations: set[str] = set()
    for path in _package_sources():
        rel = path.relative_to(root).as_posix()
        if rel.startswith(_STDLIB_LOGGING_DIRS):
            continue
        text = path.read_text(encoding="utf-8")
        if "%s" not in text or _uses_stdlib_logging(text):
            continue
        for node in ast.walk(ast.parse(text)):
            if not (isinstance(node, ast.Call) and _is_logger_call(node) and node.args):
                continue
            first = node.args[0]
            # A positional arg after the message means printf-style intent.
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and "%s" in first.value
                and len(node.args) > 1
            ):
                violations.add(rel)

    assert not violations, f"loguru formats with {{}}, not %s: {violations}"
