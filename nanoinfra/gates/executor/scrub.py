"""The executor's scrub service -- nanoinfraorg/nanoinfra#41.

The executor owns the credential store (#18), so the executor is the only process that may hold
a redaction sentinel. This module builds the sentinels, scrubs one text, and answers on a socket
of its own. ``scrub_protocol.py`` states why the socket is separate from the execute socket.

No module the agent process loads may import this one. ``tests/agent/test_redaction_isolation.py``
walks the syntax tree of the whole package to assert that. Import direction is how the split
stays true rather than merely intended.

**A failure answers a refusal, and never an empty sentinel list.** The old function returned an
empty list for every failure, and the caller then persisted the text unscrubbed. That is fail
open on the one path #17 exists to close. A store this side cannot read now raises, the socket
answers ``ok=False``, and the caller persists a marker instead of the text.

Two failures are not failures, and both keep the old answer:

- No key configured. No turn could have resolved a secret either, so nothing needs a scrub.
- One secret this process cannot decrypt. A value nobody can read is a value nobody can leak,
  and the other secrets still scrub.

**The error text names a type and never a message.** libpq quotes a whole DSN, password and all,
in its own error text. So the wire carries the exception class and a fixed sentence, and the
detail goes to this process's log.

**No record here holds the text.** The log lines carry no traceback, because loguru prints the
local variables of each frame and one of those locals is the text under scrub. The service also
imports no audit store and no job store, so no path through it writes a record at all.

**One request resolves the sentinels again.** A secret an operator created during the turn must
be scrubbed out of that same turn, so no cache spans two requests. The cost is one store read
per request, inside the process that already holds the key.

**A batch keeps that property and gets cheaper (nanoinfraorg/nanoinfra#54).** ``answer_scrub_batch``
resolves the sentinels once for the whole batch, so a walk over a hundred text carriers reads the
store once rather than a hundred times. Nothing is cached between two requests, so a secret the
operator created during the turn still scrubs out of that same turn. The saving is therefore free
of the property it might have cost: the batch narrows the read count without widening the window
in which a new secret goes unseen.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from pathlib import Path

from loguru import logger

from nanoinfra.agent.redaction import SecretSentinel, scrub_one_text, usable_sentinels
from nanoinfra.gates.executor.protocol import ProtocolError, read_frame, write_frame
from nanoinfra.gates.executor.scrub_protocol import (
    ScrubBatchRequest,
    ScrubBatchResponse,
    ScrubRequest,
    ScrubResponse,
    decode_scrub_request_frame,
    encode_scrub_batch_response,
    encode_scrub_response,
)

# The directory mode for a directory this module creates. Private first is the fail-closed
# order. A deployment that runs two accounts prepares the run directory itself, and the
# supervisor's own mode then stays in place.
_SOCKET_DIR_MODE = 0o700


def workspace_secret_sentinels(workspace: Path | str) -> list[SecretSentinel]:
    """Return the secrets this workspace can decrypt, as sentinels.

    The SecretStore import stays local, the same as it did in the agent. A caller that only
    serves a socket must not pull the crypto and Postgres import graph in to be importable.

    An unset ``NANOINFRA_SECRETS_KEY`` yields no sentinel, because no turn could have resolved
    a secret either. One secret that fails to decrypt is skipped. Every other failure raises,
    so the caller answers a refusal rather than a false success.
    """
    from nanoinfra.secrets import crypto
    from nanoinfra.secrets.store import SecretStore

    if not crypto.is_configured():
        return []
    store = SecretStore(Path(workspace))
    sentinels: list[SecretSentinel] = []
    for secret in store.list_secrets():
        try:
            value = store.resolve_plaintext(secret.id)
        except Exception:  # noqa: BLE001 -- one bad secret must not stop the rest
            logger.warning("gates: could not decrypt secret {} for a scrub", secret.id)
            continue
        if value:
            sentinels.append(SecretSentinel(name=secret.name, value=value))
    return usable_sentinels(sentinels)


def answer_scrub(request: ScrubRequest, *, workspace: Path | str) -> ScrubResponse:
    """Scrub one text, or say that the scrub did not run.

    The class decides which of the two placeholders the answer carries. A ``credential.access``
    result drops whole and keeps the names that matched (#17). Every other class gets a
    value-by-value scrub, which keeps the secret name and drops the value (#31).
    """
    try:
        sentinels = workspace_secret_sentinels(workspace)
    except Exception as exc:  # noqa: BLE001 -- a broken store refuses, and never answers nothing
        return ScrubResponse(ok=False, text="", error=_store_failure(exc))
    return ScrubResponse(
        ok=True,
        text=scrub_one_text(request.text, request.capability_class or None, sentinels),
        error=None,
    )


def answer_scrub_batch(
    request: ScrubBatchRequest, *, workspace: Path | str
) -> ScrubBatchResponse:
    """Scrub every text of a batch in order, or say that the scrub did not run (#54).

    The sentinels resolve once for the whole batch, which is the saving this verb exists for. No
    sentinel survives the call, so the property ``answer_scrub`` holds still holds here: a secret
    an operator created during the turn is scrubbed out of that same turn.

    Each item keeps its own class, because one batch mixes the classes of several tools. A
    ``credential.access`` result in the middle of a batch still drops whole (#17).

    The answer holds one text per item, always, and in the order the items arrived. A failure
    answers no text at all rather than a shorter list. A caller cannot tell a short list from a
    reordered one, so a short list would let it pair one field's scrub with another field's text.
    """
    try:
        sentinels = workspace_secret_sentinels(workspace)
    except Exception as exc:  # noqa: BLE001 -- a broken store refuses, and never answers nothing
        return ScrubBatchResponse(ok=False, texts=[], error=_store_failure(exc))
    return ScrubBatchResponse(
        ok=True,
        texts=[
            scrub_one_text(item.text, item.capability_class or None, sentinels)
            for item in request.items
        ],
        error=None,
    )


def _store_failure(exc: BaseException) -> str:
    """The sentence a refusal carries when the store did not read.

    No traceback here, and that is deliberate. This loguru version takes ``diagnose`` per handler
    only, and a diagnosed traceback prints the local variables of each frame. One of those locals
    is the text under scrub, and another is a decrypted value. So the record names the failure and
    holds no frame.
    """
    logger.warning(
        "gates: the scrub service could not read its secret store: {}: {}",
        type(exc).__name__,
        exc,
    )
    return (
        f"the executor could not read its secret store ({type(exc).__name__}). "
        "Its own log holds the detail."
    )


def bind_scrub_socket(socket_path: Path | str) -> socket.socket:
    """Bind and listen, and raise when that fails.

    The socket takes no explicit mode. The agent connects to it, exactly as it connects to the
    execute socket, so the two get their mode from the same umask. A mode here that the execute
    socket does not carry would refuse the agent on one socket and admit it on the other.

    A bind failure must reach the caller. An executor that serves no scrub socket makes every
    persist withhold its text, and that is a state a deployment has to see.
    """
    path = Path(socket_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(8)
    except OSError:
        listener.close()
        raise
    logger.info("gates: executor scrub socket listening on {}", path)
    return listener


def serve_scrub_socket(
    listener: socket.socket, workspace: Path | str, *, max_requests: int | None = None
) -> None:
    """Answer on a bound scrub socket until the listener closes.

    Each connection gets its own thread, the same as the other two sockets. A persist must not
    wait behind another persist, because both sit on the hot path of a turn.

    ``max_requests`` exists for tests. Production passes nothing.
    """
    served = 0
    while max_requests is None or served < max_requests:
        try:
            conn, _ = listener.accept()
        except OSError as exc:
            # The owner closed the listener, which is how a shutdown ends this loop.
            logger.debug("gates: scrub socket stopped answering: {}", exc)
            return
        served += 1
        threading.Thread(
            target=_answer_one_connection,
            args=(conn, Path(workspace)),
            name="nanoinfra-scrub",
            daemon=True,
        ).start()


def serve_scrub_forever(
    socket_path: Path | str, workspace: Path | str, *, max_requests: int | None = None
) -> None:
    """Bind the scrub socket, answer, and remove the socket file on exit.

    A stale socket file blocks the next bind, and a supervisor that restarts the executor must
    not need a human to delete one.
    """
    path = Path(socket_path)
    listener = bind_scrub_socket(path)
    try:
        serve_scrub_socket(listener, workspace, max_requests=max_requests)
    finally:
        listener.close()
        with contextlib.suppress(OSError):
            path.unlink()


def _answer_one_connection(conn: socket.socket, workspace: Path) -> None:
    """Answer one connection. A bad frame gets a refusal, and never a crash.

    No log line carries the text. A scrub exists to keep a credential out of a durable file,
    and a log line that quotes the text would write one to a second file.
    """
    with conn:
        try:
            request = decode_scrub_request_frame(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            # OSError covers a peer that dies mid-frame. A dropped connection ends this thread
            # either way, and a caught one ends it without a traceback.
            #
            # The refusal goes out in the single-text shape. This side does not know which verb
            # the peer meant, because reading the verb is the step that failed, and a refusal in
            # the wrong shape still refuses: the client raises on a frame it cannot decode.
            logger.warning("gates: scrub socket refused a frame: {}", exc)
            with contextlib.suppress(OSError, ProtocolError):
                write_frame(
                    conn,
                    encode_scrub_response(
                        ScrubResponse(ok=False, text="", error=f"Malformed request: {exc}")
                    ),
                )
            return

        # The answer keeps the shape of the verb that arrived. A batch request that got a
        # single-text answer would look like a batch of one to a careless reader.
        batched = isinstance(request, ScrubBatchRequest)
        try:
            if isinstance(request, ScrubBatchRequest):
                payload = encode_scrub_batch_response(
                    answer_scrub_batch(request, workspace=workspace)
                )
            else:
                payload = encode_scrub_response(answer_scrub(request, workspace=workspace))
        except Exception as exc:  # noqa: BLE001 -- one bad request must not end the process
            # No traceback, for the reason _store_failure gives: the frame locals hold the text.
            logger.warning(
                "gates: the scrub socket failed a request: {}: {}", type(exc).__name__, exc
            )
            reason = f"The executor failed this scrub ({type(exc).__name__})."
            payload = (
                encode_scrub_batch_response(
                    ScrubBatchResponse(ok=False, texts=[], error=reason)
                )
                if batched
                else encode_scrub_response(ScrubResponse(ok=False, text="", error=reason))
            )

        with contextlib.suppress(OSError, ProtocolError):
            write_frame(conn, payload)


__all__ = [
    "answer_scrub",
    "answer_scrub_batch",
    "bind_scrub_socket",
    "serve_scrub_forever",
    "serve_scrub_socket",
    "workspace_secret_sentinels",
]
