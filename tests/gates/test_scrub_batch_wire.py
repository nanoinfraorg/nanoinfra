# tests/gates/test_scrub_batch_wire.py
"""nanoinfraorg/nanoinfra#54: the batch verb on the scrub wire.

#41 put one text on the wire, and one text per connection. A Responses payload holds one item
per message plus one per tool call, so a walk over it would open one connection per item and
read the credential store once per item. This module pins the batch verb that makes the walk
affordable.

Four properties matter here.

The batch answers in the order it was asked. The caller pairs each answer with the field it
came from by position, so order is the whole contract.

A mismatch between the count sent and the count returned is a protocol error. The client
refuses the whole batch rather than pairing what arrived. A caller that paired the wrong answer
with the wrong field would write one field's scrub over another field's text.

The sentinels resolve once per request, and never once per text. That is the saving, and it
keeps the property ``scrub.py`` already states: a secret an operator created during the turn is
scrubbed out of that same turn, because no cache spans two requests.

The single-text verb still works, and a caller with one text still pays for one text.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.redaction import ScrubBatchError, TranscriptRedactor
from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.gates.executor.protocol import MAX_FRAME_BYTES, ProtocolError, read_frame
from nanoinfra.gates.executor.scrub import answer_scrub, answer_scrub_batch, bind_scrub_socket
from nanoinfra.gates.executor.scrub_client import ScrubClient, ScrubUnavailableError
from nanoinfra.gates.executor.scrub_protocol import (
    SCRUB_OP_MANY,
    SCRUB_OP_ONE,
    SCRUB_PROTOCOL_VERSION,
    ScrubBatchRequest,
    ScrubBatchResponse,
    ScrubRequest,
    ScrubResponse,
    decode_scrub_batch_request,
    decode_scrub_batch_response,
    decode_scrub_request,
    decode_scrub_request_frame,
    encode_scrub_batch_request,
    encode_scrub_batch_response,
    encode_scrub_request,
    split_scrub_batch,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"
OTHER_NAME = "prod-api-token"
OTHER_VALUE = "tok-live-9f2b7a4c1e8d"


@pytest.fixture(autouse=True)
def _secrets_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)


def _stored_secrets(workspace: Path) -> None:
    store = SecretStore(workspace)
    for name, value in ((SECRET_NAME, SECRET_VALUE), (OTHER_NAME, OTHER_VALUE)):
        store.create({"name": name, "kind": "password", "providerId": "local", "value": value})


# -- the frame ---------------------------------------------------------------


def test_a_batch_request_round_trips() -> None:
    request = ScrubBatchRequest(
        items=[
            ScrubRequest(text="ran mysql -phunter2", capability_class=MUTATE_REMOTE),
            ScrubRequest(text="restart nginx", capability_class=""),
        ]
    )

    assert decode_scrub_batch_request(encode_scrub_batch_request(request)) == request


def test_a_batch_response_round_trips() -> None:
    response = ScrubBatchResponse(ok=True, texts=["one", "two"], error=None)

    assert decode_scrub_batch_response(encode_scrub_batch_response(response)) == response


def test_the_batch_request_carries_only_the_items() -> None:
    """Nothing about a secret name, a secret value, or a workspace rides on this wire."""
    assert set(ScrubBatchRequest.__dataclass_fields__) == {"items"}


def test_the_batch_response_carries_the_texts_and_one_verdict() -> None:
    """One verdict for the whole batch. A per-text verdict would invite a partial answer."""
    assert set(ScrubBatchResponse.__dataclass_fields__) == {"ok", "texts", "error"}


def test_the_batch_answers_a_list_and_never_a_map() -> None:
    """Position is the contract, so the answer is a sequence.

    A map keyed by text would collapse two carriers that hold the same text, and the caller
    could not tell which field lost its answer.
    """
    response = decode_scrub_batch_response(
        encode_scrub_batch_response(
            ScrubBatchResponse(ok=True, texts=["same", "same"], error=None)
        )
    )

    assert response.texts == ["same", "same"]


def test_a_batch_frame_without_a_version_refuses() -> None:
    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(b'{"op": "scrub_many", "items": []}')


def test_a_batch_frame_with_another_version_refuses() -> None:
    """A newer peer may carry a field this side would ignore, and that is the hole."""
    payload = json.dumps(
        {"v": SCRUB_PROTOCOL_VERSION + 1, "op": SCRUB_OP_MANY, "items": []}
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(payload)


def test_a_batch_frame_with_an_extra_field_refuses() -> None:
    payload = json.dumps(
        {"v": SCRUB_PROTOCOL_VERSION, "op": SCRUB_OP_MANY, "items": [], "workspace": "/srv"}
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(payload)


def test_a_batch_item_with_an_extra_field_refuses() -> None:
    """An item takes the same strict decode the single frame takes."""
    payload = json.dumps(
        {
            "v": SCRUB_PROTOCOL_VERSION,
            "op": SCRUB_OP_MANY,
            "items": [{"text": "x", "capability_class": "", "secret_name": "prod"}],
        }
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(payload)


def test_a_batch_item_that_carries_its_own_version_refuses() -> None:
    """One frame is one version. A per-item version would let one frame mean two things."""
    payload = json.dumps(
        {
            "v": SCRUB_PROTOCOL_VERSION,
            "op": SCRUB_OP_MANY,
            "items": [{"text": "x", "capability_class": "", "v": SCRUB_PROTOCOL_VERSION}],
        }
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(payload)


def test_a_batch_item_that_is_not_an_object_refuses() -> None:
    payload = json.dumps(
        {"v": SCRUB_PROTOCOL_VERSION, "op": SCRUB_OP_MANY, "items": ["plain text"]}
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(payload)


def test_a_frame_that_names_no_verb_refuses() -> None:
    """The server reads the verb rather than sniffing the shape."""
    payload = json.dumps({"v": SCRUB_PROTOCOL_VERSION, "items": []}).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_request_frame(payload)


def test_a_frame_that_names_an_unknown_verb_refuses() -> None:
    payload = json.dumps(
        {"v": SCRUB_PROTOCOL_VERSION, "op": "scrub_everything", "items": []}
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_scrub_request_frame(payload)


def test_the_single_verb_refuses_a_batch_frame() -> None:
    """A strict decoder accepts one shape, so neither verb reads the other one's frame."""
    with pytest.raises(ProtocolError):
        decode_scrub_request(encode_scrub_batch_request(ScrubBatchRequest(items=[])))


def test_the_batch_verb_refuses_a_single_frame() -> None:
    with pytest.raises(ProtocolError):
        decode_scrub_batch_request(
            encode_scrub_request(ScrubRequest(text="x", capability_class=""))
        )


def test_the_server_reads_either_verb_from_one_frame() -> None:
    """``_answer_one_connection`` has one decoder, and it dispatches on the named verb."""
    one = decode_scrub_request_frame(
        encode_scrub_request(ScrubRequest(text="x", capability_class=""))
    )
    many = decode_scrub_request_frame(
        encode_scrub_batch_request(
            ScrubBatchRequest(items=[ScrubRequest(text="x", capability_class="")])
        )
    )

    assert isinstance(one, ScrubRequest)
    assert isinstance(many, ScrubBatchRequest)


def test_the_single_frame_still_names_its_own_verb() -> None:
    """A caller with one text sends one text, and never a list of one."""
    frame = json.loads(encode_scrub_request(ScrubRequest(text="x", capability_class="")))

    assert frame["op"] == SCRUB_OP_ONE
    assert "items" not in frame


# -- the answer -------------------------------------------------------------


def test_a_batch_answers_every_text_in_order(tmp_path: Path) -> None:
    _stored_secrets(tmp_path)
    request = ScrubBatchRequest(
        items=[
            ScrubRequest(text=f"first {SECRET_VALUE}", capability_class=""),
            ScrubRequest(text="second holds nothing", capability_class=""),
            ScrubRequest(text=f"third {OTHER_VALUE}", capability_class=""),
        ]
    )

    response = answer_scrub_batch(request, workspace=tmp_path)

    assert response.ok is True
    assert response.texts == [
        f"first [redacted secret: {SECRET_NAME}]",
        "second holds nothing",
        f"third [redacted secret: {OTHER_NAME}]",
    ]


def test_a_batch_answers_one_text_per_item(tmp_path: Path) -> None:
    """The count of the answer equals the count of the request, always."""
    _stored_secrets(tmp_path)
    items = [ScrubRequest(text=f"line {index}", capability_class="") for index in range(17)]

    response = answer_scrub_batch(ScrubBatchRequest(items=items), workspace=tmp_path)

    assert len(response.texts) == 17


def test_a_batch_keeps_the_class_of_each_item(tmp_path: Path) -> None:
    """One batch can mix classes, so the class stays on the item and never on the frame."""
    _stored_secrets(tmp_path)
    request = ScrubBatchRequest(
        items=[
            ScrubRequest(text=f"ran with {SECRET_VALUE}", capability_class=MUTATE_REMOTE),
            ScrubRequest(text=SECRET_VALUE, capability_class=CREDENTIAL_ACCESS),
        ]
    )

    response = answer_scrub_batch(request, workspace=tmp_path)

    assert response.texts[0] == f"ran with [redacted secret: {SECRET_NAME}]"
    assert response.texts[1] == f"[redacted credential.access result: secret={SECRET_NAME}]"


def test_an_empty_batch_answers_nothing(tmp_path: Path) -> None:
    response = answer_scrub_batch(ScrubBatchRequest(items=[]), workspace=tmp_path)

    assert response.ok is True
    assert response.texts == []


def test_a_batch_reads_the_store_once_for_the_whole_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saving is here: one sentinel resolution per request, not one per text."""
    _stored_secrets(tmp_path)
    calls: list[str] = []
    original = SecretStore.list_secrets

    def _counted(self: SecretStore) -> Any:
        calls.append("list")
        return original(self)

    monkeypatch.setattr(SecretStore, "list_secrets", _counted)
    items = [ScrubRequest(text=f"line {index}", capability_class="") for index in range(12)]

    answer_scrub_batch(ScrubBatchRequest(items=items), workspace=tmp_path)

    assert calls == ["list"]


def test_a_batch_resolves_the_sentinels_again_on_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cache spans two requests, so a secret created during the turn scrubs in that turn."""
    calls: list[str] = []
    original = SecretStore.list_secrets

    def _counted(self: SecretStore) -> Any:
        calls.append("list")
        return original(self)

    monkeypatch.setattr(SecretStore, "list_secrets", _counted)
    request = ScrubBatchRequest(items=[ScrubRequest(text=SECRET_VALUE, capability_class="")])

    # The count is read around each call, because ``create`` reads the store as well.
    before_first = len(calls)
    first = answer_scrub_batch(request, workspace=tmp_path)
    read_by_first = len(calls) - before_first
    _stored_secrets(tmp_path)
    before_second = len(calls)
    second = answer_scrub_batch(request, workspace=tmp_path)
    read_by_second = len(calls) - before_second

    assert (read_by_first, read_by_second) == (1, 1)
    assert first.texts == [SECRET_VALUE]
    assert second.texts == [f"[redacted secret: {SECRET_NAME}]"]


def test_a_broken_store_refuses_the_whole_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure answers a refusal, and never a shorter list of answers."""
    _stored_secrets(tmp_path)

    def _broken(self: SecretStore) -> Any:
        raise RuntimeError("psycopg.OperationalError")

    monkeypatch.setattr(SecretStore, "list_secrets", _broken)

    response = answer_scrub_batch(
        ScrubBatchRequest(items=[ScrubRequest(text=SECRET_VALUE, capability_class="")]),
        workspace=tmp_path,
    )

    assert response.ok is False
    assert response.texts == []
    assert "psycopg.OperationalError" not in (response.error or "")
    assert "RuntimeError" in (response.error or "")


def test_the_single_verb_still_answers(tmp_path: Path) -> None:
    """#41's own verb keeps working, unchanged."""
    _stored_secrets(tmp_path)

    response = answer_scrub(
        ScrubRequest(text=f"ran {SECRET_VALUE}", capability_class=MUTATE_REMOTE),
        workspace=tmp_path,
    )

    assert response == ScrubResponse(
        ok=True, text=f"ran [redacted secret: {SECRET_NAME}]", error=None
    )


# -- the client -------------------------------------------------------------


class _BatchScrubber:
    """A scrub socket a test drives. It counts connections and can answer a wrong count."""

    def __init__(
        self,
        socket_path: Path,
        *,
        answer: ScrubBatchResponse | None = None,
        drop_one: bool = False,
    ) -> None:
        self.connections = 0
        self.batches: list[list[tuple[str, str]]] = []
        self._answer = answer
        self._drop_one = drop_one
        self._stop = threading.Event()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.settimeout(0.05)
        self._listener.bind(str(socket_path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            with conn:
                try:
                    request = decode_scrub_batch_request(read_frame(conn))
                except Exception:  # noqa: BLE001 -- a test double answers nothing here
                    return
                self.batches.append(
                    [(item.text, item.capability_class) for item in request.items]
                )
                texts = [f"scrubbed:{item.text}" for item in request.items]
                if self._drop_one:
                    texts = texts[:-1]
                answer = self._answer or ScrubBatchResponse(ok=True, texts=texts, error=None)
                conn.sendall(_framed(encode_scrub_batch_response(answer)))

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._listener.close()


def _framed(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def test_the_client_sends_one_batch_on_one_connection(tmp_path: Path) -> None:
    """The acceptance clause of #54: many texts cost one connection."""
    socket_path = tmp_path / "run" / "e.scrub.sock"
    scrubber = _BatchScrubber(socket_path)
    try:
        answers = ScrubClient(socket_path).scrub_many(
            [("first", None), ("second", MUTATE_REMOTE), ("third", None)]
        )
    finally:
        scrubber.close()

    assert scrubber.connections == 1
    assert answers == ["scrubbed:first", "scrubbed:second", "scrubbed:third"]
    assert scrubber.batches == [[("first", ""), ("second", MUTATE_REMOTE), ("third", "")]]


def test_an_empty_batch_opens_no_connection(tmp_path: Path) -> None:
    socket_path = tmp_path / "run" / "e.scrub.sock"
    scrubber = _BatchScrubber(socket_path)
    try:
        answers = ScrubClient(socket_path).scrub_many([])
    finally:
        scrubber.close()

    assert scrubber.connections == 0
    assert answers == []


def test_a_short_answer_is_a_protocol_error(tmp_path: Path) -> None:
    """A caller that paired the wrong answer with the wrong field would write over a text."""
    socket_path = tmp_path / "run" / "e.scrub.sock"
    scrubber = _BatchScrubber(socket_path, drop_one=True)
    try:
        with pytest.raises(ScrubUnavailableError) as raised:
            ScrubClient(socket_path).scrub_many([("first", None), ("second", None)])
    finally:
        scrubber.close()

    assert "2" in str(raised.value)
    assert "1" in str(raised.value)


def test_a_refusal_reaches_the_caller_as_unavailable(tmp_path: Path) -> None:
    socket_path = tmp_path / "run" / "e.scrub.sock"
    scrubber = _BatchScrubber(
        socket_path,
        answer=ScrubBatchResponse(ok=False, texts=[], error="the store did not read"),
    )
    try:
        with pytest.raises(ScrubUnavailableError):
            ScrubClient(socket_path).scrub_many([("first", None)])
    finally:
        scrubber.close()


def test_a_missing_socket_refuses_the_batch(tmp_path: Path) -> None:
    with pytest.raises(ScrubUnavailableError):
        ScrubClient(tmp_path / "absent.sock").scrub_many([("first", None)])


# -- the frame size limit ---------------------------------------------------


def test_a_batch_that_fits_one_frame_stays_one_batch() -> None:
    items = [ScrubRequest(text="x" * 1_000, capability_class="") for _ in range(10)]

    assert [len(chunk) for chunk in split_scrub_batch(items)] == [10]


def test_a_batch_above_the_frame_limit_splits() -> None:
    """The wire caps a frame at 8 MiB, so a long transcript pays a few connections, not one
    per item."""
    big = "y" * (MAX_FRAME_BYTES // 4)
    items = [ScrubRequest(text=big, capability_class="") for _ in range(9)]

    chunks = list(split_scrub_batch(items))

    assert len(chunks) > 1
    assert sum(len(chunk) for chunk in chunks) == 9
    for chunk in chunks:
        assert len(encode_scrub_batch_request(ScrubBatchRequest(items=chunk))) <= MAX_FRAME_BYTES


def test_an_empty_batch_splits_into_nothing() -> None:
    assert list(split_scrub_batch([])) == []


# -- the two-pass adapter ---------------------------------------------------
#
# ``TranscriptRedactor.in_one_batch`` turns any redaction that asks one text at a time into one
# batch. It runs the redaction twice: once to collect and once to fill. The tests below pin the
# property that makes the trick safe, which is that the second pass checks its own position.


def test_in_one_batch_asks_once_and_fills_in_order(tmp_path: Path) -> None:
    seen: list[Sequence[tuple[str, str | None]]] = []

    def _scrub_many(texts: Sequence[tuple[str, str | None]]) -> list[str]:
        seen.append(list(texts))
        return [f"scrubbed:{text}" for text, _ in texts]

    redactor = TranscriptRedactor(lambda text, _cls: text, scrub_many=_scrub_many)

    answered = redactor.in_one_batch(
        lambda scrub: [scrub("one", None), scrub("two", MUTATE_REMOTE)]
    )

    assert answered == ["scrubbed:one", "scrubbed:two"]
    assert seen == [[("one", None), ("two", MUTATE_REMOTE)]]
    _ = tmp_path


def test_in_one_batch_with_no_batch_verb_runs_once_per_text() -> None:
    """A redactor a test built with its own scrubber keeps the behaviour it asked for."""
    calls: list[str] = []

    def _scrub(text: str, _cls: str | None) -> str:
        calls.append(text)
        return f"scrubbed:{text}"

    answered = TranscriptRedactor(_scrub).in_one_batch(
        lambda scrub: [scrub("one", None), scrub("two", None)]
    )

    assert answered == ["scrubbed:one", "scrubbed:two"]
    assert calls == ["one", "two"]


def test_in_one_batch_asks_nothing_when_the_walk_finds_no_text() -> None:
    """A payload with no carrier must cost no round trip."""
    seen: list[Sequence[tuple[str, str | None]]] = []

    def _scrub_many(texts: Sequence[tuple[str, str | None]]) -> list[str]:
        seen.append(list(texts))
        return []

    answered = TranscriptRedactor(
        lambda text, _cls: text, scrub_many=_scrub_many
    ).in_one_batch(lambda _scrub: "nothing to do")

    assert answered == "nothing to do"
    assert seen == [[]]


def test_in_one_batch_refuses_a_short_answer() -> None:
    redactor = TranscriptRedactor(lambda text, _cls: text, scrub_many=lambda _texts: ["only one"])

    with pytest.raises(ScrubBatchError):
        redactor.in_one_batch(lambda scrub: [scrub("one", None), scrub("two", None)])


def test_in_one_batch_refuses_a_pass_that_asks_a_different_question() -> None:
    """The two-pass trick needs a pure walk, and the second pass verifies it rather than trusts."""
    redactor = TranscriptRedactor(
        lambda text, _cls: text,
        scrub_many=lambda texts: [f"scrubbed:{text}" for text, _ in texts],
    )
    passes = {"count": 0}

    def _impure(scrub: Any) -> list[str]:
        passes["count"] += 1
        first = "one" if passes["count"] == 1 else "something else"
        return [scrub(first, None), scrub("two", None)]

    with pytest.raises(ScrubBatchError) as raised:
        redactor.in_one_batch(_impure)

    assert "position 0" in str(raised.value)


def test_in_one_batch_refuses_a_pass_that_asks_for_more_than_it_collected() -> None:
    redactor = TranscriptRedactor(
        lambda text, _cls: text,
        scrub_many=lambda texts: [f"scrubbed:{text}" for text, _ in texts],
    )
    passes = {"count": 0}

    def _growing(scrub: Any) -> list[str]:
        passes["count"] += 1
        texts = ["one"] if passes["count"] == 1 else ["one", "one"]
        return [scrub(text, None) for text in texts]

    with pytest.raises(ScrubBatchError):
        redactor.in_one_batch(_growing)


def test_in_one_batch_refuses_a_pass_that_leaves_an_answer_unused() -> None:
    """An unused answer means a field went unscrubbed, so the record must not persist."""
    redactor = TranscriptRedactor(
        lambda text, _cls: text,
        scrub_many=lambda texts: [f"scrubbed:{text}" for text, _ in texts],
    )
    passes = {"count": 0}

    def _shrinking(scrub: Any) -> list[str]:
        passes["count"] += 1
        texts = ["one", "one"] if passes["count"] == 1 else ["one"]
        return [scrub(text, None) for text in texts]

    with pytest.raises(ScrubBatchError):
        redactor.in_one_batch(_shrinking)


def test_the_socket_answers_a_batch_end_to_end(tmp_path: Path) -> None:
    """One real listener, one real client, and the executor's own answer."""
    _stored_secrets(tmp_path)
    socket_path = tmp_path / "run" / "e.scrub.sock"
    listener = bind_scrub_socket(socket_path)
    from nanoinfra.gates.executor.scrub import serve_scrub_socket

    thread = threading.Thread(
        target=serve_scrub_socket,
        args=(listener, tmp_path),
        kwargs={"max_requests": 1},
        daemon=True,
    )
    thread.start()
    try:
        answers = ScrubClient(socket_path).scrub_many(
            [(f"ran {SECRET_VALUE}", MUTATE_REMOTE), ("nothing here", None)]
        )
    finally:
        listener.close()
        thread.join(timeout=10)

    assert answers == [f"ran [redacted secret: {SECRET_NAME}]", "nothing here"]
