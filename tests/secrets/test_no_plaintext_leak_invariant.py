"""Static tripwire for the "only store.py ever decrypts a secret" convention.

This convention is documented in several places (module docstrings in
store.py, secrets_api.py) but was, until now, unenforced -- nothing stopped
a future change from adding a second call site for
``crypto.decrypt``/``SecretStore.resolve_plaintext`` outside store.py,
silently reopening a path for plaintext to leak into a REST response.

Secrets has no agent-facing tool/skill at all -- it's WebUI/REST-only, so
the LLM never sees a plaintext value, not even for the one turn a chat
skill would have required. ``resolve_plaintext`` exists on ``SecretStore``
for the Servers execution engine (a future caller, not yet built) to
decrypt a secret in-process immediately before connecting -- this test
guards that seam staying singular as that caller lands.

Deliberately a plain substring/regex scan over file contents, not an AST
walk -- simple and good enough to catch a regression; sophisticated enough
evasion (e.g. ``getattr(crypto, "decrypt")``) is out of scope for a
tripwire like this.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATTERN = re.compile(r"crypto\.decrypt|\.resolve_plaintext\(")
_ALLOWED_FILE = _REPO_ROOT / "nanoinfra" / "secrets" / "store.py"

_SCAN_TARGETS = [
    _REPO_ROOT / "nanoinfra" / "secrets",
    _REPO_ROOT / "nanoinfra" / "webui" / "secrets_api.py",
]


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for target in _SCAN_TARGETS:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.glob("*.py")))
    return files


def test_only_store_py_calls_crypto_decrypt_or_resolve_plaintext() -> None:
    assert _ALLOWED_FILE.is_file(), f"expected {_ALLOWED_FILE} to exist"

    offenders = [
        path
        for path in _iter_py_files()
        if path != _ALLOWED_FILE and _PATTERN.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"Only {_ALLOWED_FILE} may reference 'crypto.decrypt' or "
        f"'.resolve_plaintext(' -- found matches in: {offenders}. "
        "Plaintext must only ever be resolved through SecretStore's single "
        "seam; a new call site elsewhere risks leaking a value into a REST "
        "response or agent tool result."
    )


def test_pattern_is_not_vacuous() -> None:
    """Sanity-check the regex itself still matches something real, so this
    test can't silently pass forever if store.py's implementation changes
    shape (e.g. renamed) without anyone noticing the guard went blind."""
    assert _PATTERN.search(_ALLOWED_FILE.read_text(encoding="utf-8"))
