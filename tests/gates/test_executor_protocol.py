# tests/gates/test_executor_protocol.py
"""Item 15 (#18): the wire between the agent and the executor.

The transport is a Unix domain socket. TCP would widen any egress policy to include our own
executor, and that holes the one rule that makes a narrow network policy useful.

The wire carries structured fields only. No free-form field exists for the model to fill, so a
request cannot smuggle a command the executor did not resolve itself.

Item 36 (#38) took the version to 2 and added ``origin_path``. #13 cannot judge path
independence without the origin, so a version 1 frame gets a refusal. An agent that does not
state its path must not execute. The client and the server ship together, so no rolling deploy
depends on the older frame.

Item 10 (#47) took the version to 3 and added ``origin_actor``. The field set changed, so the
version rises. The executor treats both origin fields as the agent's assertion about itself, and
an absent identity travels as null rather than as an empty string, because #13 must be able to
tell "no identity" from a person named by the empty text.

Delegation took the version to 7 and added ``acting_agent``, ``delegated_by`` and
``inherited_capabilities`` (#251). The gate asks *may this actor do this now*, and it cannot
answer that for a delegated turn without knowing which agent acts, which one asked, and what the
asking turn was allowed to do. All three are ``None``/empty on a turn nothing delegated, which is
every turn in a deployment with one agent.

Data connectors took the version to 5 and added a second request kind. The kind travels in the
envelope beside the version, so one socket serves two shapes and neither can be read as the
other. The connector frame names a connector and an operation and never a URL or a method: the
executor reads those from the installed manifest, so a frame cannot describe a call the package
never declared, and it cannot relabel a write as a read.
"""

from __future__ import annotations

import json

import pytest

from nanoinfra.gates.executor.protocol import (
    PROTOCOL_VERSION,
    ConnectorRequest,
    ExecuteRequest,
    ExecuteResponse,
    ProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    read_frame,
    write_frame,
)


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, object] = {
        "server_id_or_name": "prod-web-01",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "telegram",
        "origin_actor": "12345",
    }
    fields.update(over)
    return ExecuteRequest(**fields)  # pyright: ignore[reportArgumentType]


def test_a_request_round_trips() -> None:
    request = _request()

    assert decode_request(encode_request(request)) == request


def test_a_response_round_trips() -> None:
    response = ExecuteResponse(ok=True, output="up 3 days", exit_code=0, error=None, reason="")

    assert decode_response(encode_response(response)) == response


def _connector_request(**over: object) -> ConnectorRequest:
    fields: dict[str, object] = {
        "connector": "google-calendar",
        "operation": "create_event",
        "arguments_json": '{"summary": "Standup"}',
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "token_nonce": None,
        "origin_path": "telegram",
        "origin_actor": "12345",
    }
    fields.update(over)
    return ConnectorRequest(**fields)  # pyright: ignore[reportArgumentType]


def test_a_connector_request_round_trips() -> None:
    request = _connector_request()

    assert decode_request(encode_request(request)) == request


def test_the_two_request_kinds_do_not_read_as_each_other() -> None:
    """One socket, two shapes. The kind decides which, and a frame with none refuses."""
    assert isinstance(decode_request(encode_request(_request())), ExecuteRequest)
    assert isinstance(decode_request(encode_request(_connector_request())), ConnectorRequest)

    payload = json.loads(encode_request(_connector_request()))
    del payload["kind"]
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())

    payload = json.loads(encode_request(_connector_request()))
    payload["kind"] = "gmail"
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_connector_frame_names_no_url_and_no_method() -> None:
    """The executor reads both from the manifest, so the agent cannot describe the call.

    ``arguments_json`` is the one caller-shaped field, and it is bounded elsewhere: the
    executor validates it against the operation's own declared schema and refuses an
    undeclared key, so the bound is a manifest in this repository rather than the sender.
    """
    allowed = set(ConnectorRequest.__dataclass_fields__)

    assert allowed == {
        "connector",
        "operation",
        "arguments_json",
        "session_id",
        "execution_context",
        "preview_requested",
        "token_nonce",
        "origin_path",
        "origin_actor",
        "acting_agent",
        "delegated_by",
        "inherited_capabilities",
    }


def test_an_unknown_field_on_a_connector_frame_is_refused() -> None:
    """A tolerated extra field is an ambiguity between two peers, and that is the hole."""
    payload = json.loads(encode_request(_connector_request()))
    payload["capability_class"] = "read"

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_request_carries_no_free_form_field() -> None:
    """A field the model could fill with prose is a field it could use to describe intent.

    #14 keeps model text out of the approval prompt. The wire keeps it off the request.
    """
    allowed = set(ExecuteRequest.__dataclass_fields__)

    assert allowed == {
        "server_id_or_name",
        "command",
        "session_id",
        "execution_context",
        "preview_requested",
        "timeout_s",
        "token_nonce",
        "origin_path",
        "origin_actor",
        "acting_agent",
        "delegated_by",
        "inherited_capabilities",
    }


def test_the_version_is_seven_and_a_version_one_frame_is_refused() -> None:
    """#38 needs the origin path, so the older frame cannot describe a request any more.

    A version 1 peer states no path. #13 cannot prove path independence for it, so the frame
    gets a refusal instead of a default.

    The version reached 5 when the wire gained a second request kind for a data connector
    call, and 6 when it gained a third for a secret write. The field set of this request did
    not change either time; the envelope did, and an envelope change is a version change for
    the same reason a field change is. 7 is a field change again: #251 added the two agent names
    and the ceiling the delegating turn declared.
    """
    payload = json.loads(encode_request(_request()))
    payload["v"] = 1
    del payload["origin_path"]
    del payload["origin_actor"]

    assert PROTOCOL_VERSION == 7
    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_the_delegation_survives_the_round_trip() -> None:
    """The gate reads all three to decide a delegated action (#251)."""
    request = _request(
        acting_agent="sre-prod",
        delegated_by="manager",
        inherited_capabilities=["mutate.remote"],
    )

    decoded = decode_request(encode_request(request))

    assert (decoded.acting_agent, decoded.delegated_by) == ("sre-prod", "manager")
    assert decoded.inherited_capabilities == ["mutate.remote"]


def test_a_frame_that_nothing_delegated_names_no_agent_and_declares_no_ceiling() -> None:
    """Every frame of a deployment with one agent. The gate must read it as it always did.

    Null and not empty text, for the reason the origin identity is null: an empty name reads as
    a name, and the approvals inbox renders these two values.
    """
    payload = json.loads(encode_request(_request()))

    assert payload["acting_agent"] is None
    assert payload["delegated_by"] is None
    assert payload["inherited_capabilities"] == []


def test_a_frame_missing_a_delegation_field_is_refused() -> None:
    """The field set is the version, so a peer that shares the version shares every field.

    A default on this side would let the executor decide a delegated action from a fact the
    agent never sent, which is the guess this wire exists to refuse.
    """
    for absent in ("acting_agent", "delegated_by", "inherited_capabilities"):
        payload = json.loads(encode_request(_request()))
        del payload[absent]

        with pytest.raises(ProtocolError):
            decode_request(json.dumps(payload).encode())


def test_a_version_two_frame_is_refused_rather_than_read_as_an_unknown_identity() -> None:
    """The field set changed, so the version changed (#47, item 10).

    A version 2 frame carries no ``origin_actor``. This side could read the absence as "no
    identity", which is a value #13 accepts, so the frame would execute. That is the guess this
    wire refuses: a peer that shares the version must share the field set exactly.
    """
    payload = json.loads(encode_request(_request()))
    payload["v"] = 2
    del payload["origin_actor"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_version_two_frame_without_the_origin_path_is_refused() -> None:
    """The field is mandatory on the wire. A frame that omits it names no path at all."""
    payload = json.loads(encode_request(_request()))
    del payload["origin_path"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_frame_without_the_origin_actor_is_refused() -> None:
    """The field is mandatory on the wire, the same as the origin path.

    A default on this side would mean two peers disagreed about one frame, and the executor
    would then judge identity independence from a value the agent never sent.
    """
    payload = json.loads(encode_request(_request()))
    del payload["origin_actor"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_the_origin_path_survives_the_round_trip() -> None:
    """The executor reads this field to judge path independence, so it must arrive intact."""
    request = _request(origin_path="webui")

    assert decode_request(encode_request(request)).origin_path == "webui"


def test_the_origin_actor_survives_the_round_trip() -> None:
    """The executor reads this field to judge identity independence (#47, item 11)."""
    request = _request(origin_actor="webui:alberto@example.com")

    assert decode_request(encode_request(request)).origin_actor == "webui:alberto@example.com"


def test_an_absent_origin_identity_travels_as_null_and_never_as_empty_text() -> None:
    """"No identity" and "a person whose name is the empty string" are different facts.

    #13 falls back to the path rule alone for the first one. An empty string on the wire would
    read as a name, and a name that matches nothing is not the same as a missing value.
    """
    payload = json.loads(encode_request(_request(origin_actor=None)))

    assert payload["origin_actor"] is None
    assert decode_request(encode_request(_request(origin_actor=None))).origin_actor is None


def test_an_unknown_field_is_refused() -> None:
    """Fail closed on a wire the peer does not share. A tolerated extra is an ambiguity."""
    payload = json.loads(encode_request(_request()))
    payload["extra"] = "surprise"

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_missing_version_is_refused() -> None:
    payload = json.loads(encode_request(_request()))
    del payload["v"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_future_version_is_refused() -> None:
    """A newer peer may mean a field this one ignores, and ignoring a field is the hole."""
    payload = json.loads(encode_request(_request()))
    payload["v"] = PROTOCOL_VERSION + 1

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_non_json_frame_is_refused() -> None:
    with pytest.raises(ProtocolError):
        decode_request(b"not json at all")


def test_a_frame_round_trips_over_a_socket_pair() -> None:
    import socket

    left, right = socket.socketpair()
    try:
        write_frame(left, encode_request(_request()))
        payload = read_frame(right)
    finally:
        left.close()
        right.close()

    assert decode_request(payload) == _request()


def test_two_frames_do_not_run_together() -> None:
    """Length prefixing, not newline framing. A command may hold a newline."""
    import socket

    left, right = socket.socketpair()
    try:
        write_frame(left, encode_request(_request(command="line one\nline two")))
        write_frame(left, encode_request(_request(command="second")))
        first = decode_request(read_frame(right))
        second = decode_request(read_frame(right))
    finally:
        left.close()
        right.close()

    assert first.command == "line one\nline two"
    assert second.command == "second"


def test_an_oversized_frame_is_refused() -> None:
    """A length prefix a peer controls must not let it ask for an unbounded allocation."""
    import socket

    from nanoinfra.gates.executor.protocol import MAX_FRAME_BYTES

    left, right = socket.socketpair()
    try:
        left.sendall((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        with pytest.raises(ProtocolError):
            read_frame(right)
    finally:
        left.close()
        right.close()


def test_a_truncated_frame_is_refused() -> None:
    """A peer that dies mid-write must not leave the reader with a partial request."""
    import socket

    left, right = socket.socketpair()
    try:
        payload = encode_request(_request())
        left.sendall(len(payload).to_bytes(4, "big"))
        left.sendall(payload[:5])
        left.close()
        with pytest.raises(ProtocolError):
            read_frame(right)
    finally:
        right.close()


def test_a_response_carries_no_secret_field() -> None:
    """The executor returns output and a verdict. It never returns a credential."""
    fields = set(ExecuteResponse.__dataclass_fields__)

    assert "secret" not in fields
    assert "secret_value" not in fields
    # `terminal` joined the shape in #42: the tool latches a terminal refusal alone, so a
    # configuration gap no longer blocks a session that could never have passed. The `preview_*`
    # fields joined in #179 and carry the gate's answer for an action that did not run: a
    # decision, a reason, and the two halves of the grant that would permit it. None of them is
    # a credential, and `preview_command` is the command the caller already sent.
    assert fields == {
        "ok",
        "output",
        "exit_code",
        "error",
        "reason",
        "terminal",
        "preview_outcome",
        "preview_reason",
        "preview_grant_id",
        "preview_scope",
        "preview_hosts",
        "preview_command",
        "preview_credential_outcome",
        "preview_credential_reason",
    }
