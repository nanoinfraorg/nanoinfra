"""Cross-suite test infrastructure."""

from __future__ import annotations

import os
import site
import ssl
import sys
from collections.abc import Iterator
from pathlib import Path

import certifi
import pytest
from loguru import logger

# The operator's own installation, read once at import time, which is before any fixture
# rewrites HOME. Every guard below measures against this value (nanoinfraorg/nanoinfra#45).
REAL_NANOINFRA_DIR = Path.home().resolve() / ".nanoinfra"

# The same directory as text, for the substring test in the audit hook below.
_REAL_DIRECTORY_TEXT = str(REAL_NANOINFRA_DIR)

# The Python user site directory, also read before HOME moves. A subprocess computes this
# directory from HOME, so a fresh HOME hides every package the developer installed with
# `pip --user`. `ansible-inventory` is one of them, and `tests/servers` runs the real binary.
REAL_USER_BASE = site.getuserbase()

# The width a captured CLI output is rendered at. rich reads COLUMNS, and its default of 80
# wraps a long path in the middle of a file name. A `tmp_path` under xdist carries an extra
# `popen-gw0/` segment, so two assertions about a config file name passed serially and failed
# under `-n auto` for a reason that had nothing to do with either test.
_CAPTURED_TERMINAL_COLUMNS = "200"
_CAPTURED_TERMINAL_LINES = "50"

# The write events an audit hook watches. A read never contaminates an installation, and the
# hook runs for every event in the process, so the set stays as small as the property allows.
_WRITE_EVENTS = frozenset({
    "open",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.symlink",
})

# Which `open` modes write. The audit event carries the mode string for the builtin and None
# for a low level call, and None is treated as a write, because a refusal to guess is safer
# here than a guess that misses a write.
_WRITE_MODE_CHARACTERS = frozenset("wxa+")

# (test id, path) for every write a test aimed at the real installation. A session scoped
# fixture reads this at the end of the run.
_WRITES_TO_THE_REAL_INSTALLATION: list[tuple[str, str]] = []

# The test that is running now, for the record above. A guard that names no test would leave
# an operator to bisect the suite by hand.
_current_test_id = "before the first test"


@pytest.fixture(autouse=True)
def _isolate_nanoinfra_log_activation() -> Iterator[None]:
    """Keep CLI log settings from leaking into later tests in the same process."""
    logger.enable("nanoinfra")
    try:
        yield
    finally:
        logger.enable("nanoinfra")


@pytest.fixture(autouse=True)
def _isolate_the_operator_installation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Give every test its own home, so the real installation is out of reach (#45).

    ``get_config_path`` reads ``Path.home() / ".nanoinfra"``, and no fixture pointed that
    anywhere else. A test that touched a store without a patched HOME therefore wrote to the
    installation the operator runs. Four kinds of contamination came out of one session on the
    maintainer's machine: four pending pairing requests for a channel they do not run, a latch
    on a session id that never existed, two helper sockets under their real run directory, and
    ten orphan children holding them.

    A test that sets HOME itself keeps its own value, because its ``setenv`` runs later than
    this fixture.

    PYTHONUSERBASE goes out unchanged on purpose. This fixture isolates where nanoinfra
    **writes**, and never where Python finds its **packages**. A fresh HOME removes the user
    site directory from a child's ``sys.path``, and `ansible-inventory` lives there on a
    ``pip --user`` install, so the 37 parity tests in ``tests/servers`` and ``tests/gates``
    stopped finding the binary they exist to compare against.

    The config path global resets around each test as well. ``set_config_path`` writes a module
    level value, so a test that pointed it at its own ``tmp_path`` left a dead path behind for
    the next test in the same worker.
    """
    from nanoinfra.config import loader

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHONUSERBASE", REAL_USER_BASE)
    # A private global, and the reason to reach it is that no public reset exists. The value
    # restores rather than clears, so a session scoped fixture that set it keeps its own.
    monkeypatch.setattr(loader, "_current_config_path", loader._current_config_path)  # pyright: ignore[reportPrivateUsage]

    global _current_test_id
    _current_test_id = request.node.nodeid
    yield
    _current_test_id = f"after {request.node.nodeid}"


@pytest.fixture(autouse=True)
def _pin_the_captured_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render a captured CLI output at one width, whatever the length of a tmp path (#45).

    An assertion about the text of a message must not depend on where pytest put a directory.
    A test that asserts on layout sets its own value, because its ``setenv`` runs later.
    """
    monkeypatch.setenv("COLUMNS", _CAPTURED_TERMINAL_COLUMNS)
    monkeypatch.setenv("LINES", _CAPTURED_TERMINAL_LINES)


def _record_a_write_to_the_real_installation(event: str, args: tuple[object, ...]) -> None:
    """Note one write a test aimed at the operator's own installation (#45).

    The hook records and never raises. A raise inside an audit hook surfaces wherever the
    write happened, and a library that catches broad exceptions would swallow it, so the
    record is the half that cannot be lost. The session fixture below fails the run.
    """
    if event not in _WRITE_EVENTS or not args:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return
    try:
        text = os.fsdecode(target)
    except (TypeError, ValueError, UnicodeDecodeError):
        return
    # The cheap half first. This hook runs for every file the suite opens, and a substring
    # test rejects each one of those in a few nanoseconds. The exact test follows, because a
    # prefix match alone would also accept `~/.nanoinfra-backup`.
    if _REAL_DIRECTORY_TEXT not in text:
        return
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        if isinstance(mode, str) and not (_WRITE_MODE_CHARACTERS & set(mode)):
            return
    path = Path(text)
    if REAL_NANOINFRA_DIR == path or REAL_NANOINFRA_DIR in path.parents:
        _WRITES_TO_THE_REAL_INSTALLATION.append((_current_test_id, text))


class InstallationWriteGuard:
    """The guard, behind a fixture, for the tests that check the guard itself.

    A test cannot import this module by name. ``tests`` holds no ``__init__.py``, so several
    directories each carry a module called ``conftest``, and an import picks whichever one
    reached ``sys.modules`` first. Fixtures are the addressable half.
    """

    def __init__(self) -> None:
        self._first_own_record = len(_WRITES_TO_THE_REAL_INSTALLATION)

    @property
    def real_installation(self) -> Path:
        return REAL_NANOINFRA_DIR

    def record(self, event: str, args: tuple[object, ...]) -> None:
        """Run the hook by hand, so a test proves the record without a real write."""
        _record_a_write_to_the_real_installation(event, args)

    def own_records(self) -> list[tuple[str, str]]:
        return _WRITES_TO_THE_REAL_INSTALLATION[self._first_own_record :]

    def forget_own_records(self) -> None:
        """Drop what this test recorded, so the session guard stays truthful.

        A real contamination has no such call and reaches the end of the run.
        """
        del _WRITES_TO_THE_REAL_INSTALLATION[self._first_own_record :]


@pytest.fixture
def installation_write_guard() -> Iterator[InstallationWriteGuard]:
    guard = InstallationWriteGuard()
    try:
        yield guard
    finally:
        guard.forget_own_records()


@pytest.fixture
def real_nanoinfra_dir() -> Path:
    """The installation the operator runs, which no test may write to."""
    return REAL_NANOINFRA_DIR


@pytest.fixture
def real_user_base() -> str:
    """The Python user site base, read before any fixture moved HOME."""
    return REAL_USER_BASE


@pytest.fixture(scope="session", autouse=True)
def _fail_the_run_for_a_write_to_the_real_installation() -> Iterator[None]:
    """Fail the run when a test wrote to the operator's own installation (#45).

    Each of the four contaminations that produced #45 was invisible until the operator saw it
    in their own UI, which is the reason this guard exists rather than a review rule. The
    failure names the test and the path, so nobody has to bisect the suite.

    An audit hook cannot be removed, and this fixture is the only caller, so it installs once
    per session. A child process is covered by the isolated HOME instead: it computes the
    directory from its environment, and it never sees the real one.
    """
    sys.addaudithook(_record_a_write_to_the_real_installation)
    yield
    if not _WRITES_TO_THE_REAL_INSTALLATION:
        return
    lines = "\n".join(
        f"  {test_id} wrote {path}" for test_id, path in _WRITES_TO_THE_REAL_INSTALLATION[:20]
    )
    remaining = len(_WRITES_TO_THE_REAL_INSTALLATION) - 20
    if remaining > 0:
        lines += f"\n  and {remaining} more"
    pytest.fail(
        f"a test wrote to the operator's own installation at {REAL_NANOINFRA_DIR}:\n{lines}\n"
        "A store resolves its path through Path.home(), so the test needs the isolated home "
        "this suite gives it, or an explicit path under its tmp_path.",
        pytrace=False,
    )


@pytest.fixture(scope="session", autouse=True)
def _use_windows_system_ca_for_default_http_clients() -> Iterator[None]:
    """Avoid reparsing certifi's CA bundle for every offline HTTP client.

    Loading certifi takes roughly 0.7 seconds per client on Windows. The test
    suite constructs hundreds of clients while mocking their I/O. System roots
    preserve certificate verification for accidental local requests; explicit
    ``cafile``, ``capath``, and ``cadata`` arguments still use the real loader.
    """
    if sys.platform != "win32":
        yield
        return

    original = ssl.create_default_context
    certifi_path = os.path.normcase(os.path.abspath(certifi.where()))

    def create_default_context(
        purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
        *,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> ssl.SSLContext:
        requested_path = os.path.normcase(os.path.abspath(cafile)) if cafile else None
        if requested_path == certifi_path and capath is None and cadata is None:
            return original(purpose)
        return original(
            purpose,
            cafile=cafile,
            capath=capath,
            cadata=cadata,
        )

    ssl.create_default_context = create_default_context
    try:
        yield
    finally:
        ssl.create_default_context = original
