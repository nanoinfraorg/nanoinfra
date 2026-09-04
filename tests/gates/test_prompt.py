# tests/gates/test_prompt.py
"""Item 11 (#14): render the approval prompt from resolver output.

A human approves two facts. The command that runs, and the hosts it runs on. Both facts come
from the #4 resolver. Neither fact comes from the model. A model-written summary would put the
unfaithful-summarization problem inside the security path: the human authorizes a sentence,
the executor runs a command, and nothing compares the two.

Three properties carry the weight, so each property gets its own tests:

1. A group renders every resolved name plus a count. It never renders the group name.
2. No renderer input can carry a model-authored field. The signature refuses it.
3. The approved digest covers the exact bytes the renderer produced.

The last tests hold the module to the #18 executor boundary. The renderer imports no agent
module, reads no request context, and reads no config. A renderer that needs any of those
cannot move into a separate process later.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.gates import prompt as prompt_module
from nanoinfra.gates.prompt import (
    ApprovalPrompt,
    PromptRenderError,
    action_from_rendered_prompt,
    digest_rendered_prompt,
    render_approval_prompt,
    render_approval_prompt_for_hosts,
)
from nanoinfra.gates.tokens import (
    ApprovalToken,
    ApprovalTokenStore,
    TokenRefusal,
    compute_target_digest,
)
from nanoinfra.servers.scope import GROUP, resolve_scope
from nanoinfra.servers.types import Server

_COMMAND = "systemctl restart nginx"

# The fourteen hosts behind one group name. The whole point of this item is that a human reads
# these names and never the word that stands for them.
_WEBSERVERS = tuple(f"web-{index:02d}" for index in range(1, 15))

# The complete rendered payload for that group. The digest is written out rather than computed
# here, because the test has to fail when the digest input changes. The human approves these
# exact bytes, so a byte is a fact and not an implementation detail.
_GOLDEN_PAYLOAD = """\
nanoinfra approval request v1
The executor resolved this request. No part of it comes from the agent.

Command, exactly as the executor will run it:
  | systemctl restart nginx

Hosts: 14
   1. web-01
   2. web-02
   3. web-03
   4. web-04
   5. web-05
   6. web-06
   7. web-07
   8. web-08
   9. web-09
  10. web-10
  11. web-11
  12. web-12
  13. web-13
  14. web-14

Binding digest: sha256:42459a566eadc21441aa08728e6fe0cdc5fc3c3f0e8e362746fc398a97ba618e
"""

# Field names that would carry a model sentence. A renderer input named like one of these is
# the defect this item exists to prevent.
_MODEL_TEXT_NAMES = frozenset(
    {
        "summary",
        "description",
        "reason",
        "rationale",
        "intent",
        "note",
        "notes",
        "explanation",
        "message",
        "label",
        "title",
        "purpose",
    }
)

# Packages that live in the agent process. An import of any one of them means the renderer
# cannot move into the #18 executor process without a rewrite.
_FORBIDDEN_IMPORT_ROOTS = (
    "nanoinfra.agent",
    "nanoinfra.channels",
    "nanoinfra.config",
    "nanoinfra.gateway",
    "nanoinfra.session",
    "nanoinfra.webui",
)

# Names that read ambient state. A pure function reads none of them.
_FORBIDDEN_READS = frozenset(
    {
        "current_request_session_key",
        "current_request_execution_context",
        "environ",
        "getenv",
        "get_config",
        "load_config",
        "getcwd",
    }
)


def _ansible_server(project_path: Path) -> Server:
    """One ansible-runner server whose ``webservers`` group holds the fourteen hosts."""
    (project_path / "inventory").write_text("[webservers]\nweb-[01:14]\n", encoding="utf-8")
    return Server(
        id="a" * 32,
        name="web-tier",
        provider_id="ansible-runner",
        config={"projectPath": str(project_path), "group": "webservers"},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


def _issue(store: ApprovalTokenStore, digest: str) -> ApprovalToken:
    """Issue one approval bound to ``digest``, with the fields #13 would fill."""
    return store.issue(
        session_id="session-1",
        actor="webui:operator-1",
        origin_path="telegram:chat-9",
        approval_path="webui:operator-1",
        target_digest=digest,
        capability_class=MUTATE_REMOTE,
        scope=GROUP,
    )


def test_a_group_of_fourteen_renders_every_name_and_the_count() -> None:
    """The golden payload. Fourteen names and the number fourteen, both visible.

    A count with no names hides the one host an operator would have refused. A name list with
    no count makes a fifteenth line easy to miss.
    """
    payload = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_WEBSERVERS)

    assert payload.text == _GOLDEN_PAYLOAD
    assert payload.host_count == 14
    assert "Hosts: 14" in payload.text
    for host in _WEBSERVERS:
        assert host in payload.text


def test_the_group_name_never_replaces_the_host_names(tmp_path: Path) -> None:
    """``group: webservers`` renders as fourteen names, and the word never appears.

    The resolver path and the explicit-host path produce the same bytes. That matters: the
    payload a human reads must not depend on which call site built it.
    """
    resolution = resolve_scope(_ansible_server(tmp_path))
    payload = render_approval_prompt(command=_COMMAND, resolution=resolution)

    assert payload.text == _GOLDEN_PAYLOAD
    assert "webservers" not in payload.text
    assert "web tier" not in payload.text
    # The pattern stays available beside the payload, and outside the bytes the digest covers.
    assert payload.pattern == "webservers"
    assert payload.scope == GROUP


def test_the_approved_digest_equals_the_digest_of_the_rendered_payload() -> None:
    """The acceptance criterion. The approval covers what the human read.

    The digest re-derives from the rendered bytes alone. So the store never has to trust a
    field that travels beside the payload.
    """
    payload = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_WEBSERVERS)

    assert payload.target_digest == compute_target_digest(command=_COMMAND, hosts=_WEBSERVERS)
    assert digest_rendered_prompt(payload.text) == payload.target_digest

    store = ApprovalTokenStore()
    token = _issue(store, payload.target_digest)
    outcome = store.consume(
        nonce=token.nonce,
        session_id="session-1",
        target_digest=digest_rendered_prompt(payload.text),
    )
    assert outcome.ok


def test_the_rendered_payload_gives_back_the_command_and_the_hosts() -> None:
    """#219 derives a standing grant from these two values, and from nothing else.

    They come back out of the bytes the digest covers, so a grant built from them cannot be
    wider than the action a human read. Anything the request body claimed is not consulted.
    """
    payload = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_WEBSERVERS)

    recovered = action_from_rendered_prompt(payload.text)

    assert recovered.command == _COMMAND
    assert recovered.hosts == tuple(sorted(_WEBSERVERS))
    assert recovered.target_digest == payload.target_digest


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        ("command", "systemctl restart nginx", "systemctl stop nginx"),
        ("count", "Hosts: 14", "Hosts: 13"),
        ("host_name", "web-07", "db-07"),
        ("digest", "sha256:42459a", "sha256:00000a"),
        ("provenance", "No part of it comes from the agent.", "Restart the web tier."),
    ],
)
def test_one_changed_byte_breaks_the_binding(name: str, before: str, after: str) -> None:
    """A payload nobody rendered has no digest. It raises instead of getting one.

    Every mutation here leaves the text self-inconsistent, so the re-render check catches it.
    ``name`` only labels the case.
    """
    payload = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_WEBSERVERS)
    tampered = payload.text.replace(before, after)
    assert tampered != payload.text, name

    with pytest.raises(PromptRenderError):
        digest_rendered_prompt(tampered)


def test_an_extra_host_line_breaks_the_binding() -> None:
    """A fifteenth host appended after the render never becomes an approved host."""
    payload = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_WEBSERVERS)

    with pytest.raises(PromptRenderError):
        digest_rendered_prompt(payload.text + "  15. db-01\n")


def test_a_consistent_forgery_gets_another_digest_and_fails_verification() -> None:
    """A whole re-render of another command is self-consistent, and still refused.

    This is the case the digest exists for. The text reads correctly, so no reader catches it.
    The store catches it, because the digest of those bytes is not the digest it holds.
    """
    approved = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_WEBSERVERS)
    forged = render_approval_prompt_for_hosts(command="systemctl stop nginx", hosts=_WEBSERVERS)

    assert digest_rendered_prompt(forged.text) != approved.target_digest

    store = ApprovalTokenStore()
    token = _issue(store, approved.target_digest)
    outcome = store.consume(
        nonce=token.nonce,
        session_id="session-1",
        target_digest=digest_rendered_prompt(forged.text),
    )
    assert not outcome.ok
    assert outcome.refusal is TokenRefusal.DIGEST_MISMATCH


def test_no_renderer_input_can_carry_a_model_authored_field() -> None:
    """The signature refuses model text. A rule nobody has to remember.

    Every parameter is keyword-only, required, and typed to a structured value or to the
    resolved command. There is no free-form field to fill, so a caller cannot pass a sentence
    even by mistake.
    """
    allowed_annotations = {"str", "ScopeResolution", "tuple[str, ...]"}

    for renderer in (render_approval_prompt, render_approval_prompt_for_hosts):
        parameters = inspect.signature(renderer).parameters
        assert set(parameters) <= {"command", "resolution", "hosts"}
        for name, parameter in parameters.items():
            assert name not in _MODEL_TEXT_NAMES
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert parameter.default is inspect.Parameter.empty, name
            assert parameter.annotation in allowed_annotations, (name, parameter.annotation)

    # The rendered payload carries no free-form field either, so #27 has nothing to display
    # that a model could have written.
    assert not {field.name for field in fields(ApprovalPrompt)} & _MODEL_TEXT_NAMES

    with pytest.raises(TypeError):
        render_approval_prompt_for_hosts(  # type: ignore[call-arg]
            command=_COMMAND,
            hosts=_WEBSERVERS,
            summary="Restart the web tier.",
        )


def test_a_sentence_is_not_a_host_list() -> None:
    """``hosts`` takes a tuple and refuses anything else.

    A ``Sequence[str]`` annotation would accept a bare ``str``, because a ``str`` is a sequence
    of ``str``. "the web tier" would then render one host per character.
    """
    sentence = "the web tier"
    with pytest.raises(PromptRenderError):
        render_approval_prompt_for_hosts(command=_COMMAND, hosts=sentence)  # type: ignore[arg-type]

    # A list is refused as well. The payload and its digest come from one immutable tuple.
    host_list = ["web-01"]
    with pytest.raises(PromptRenderError):
        render_approval_prompt_for_hosts(
            command=_COMMAND,
            hosts=host_list,  # type: ignore[arg-type]
        )


def test_only_real_resolver_output_renders() -> None:
    """A stand-in that looks like a resolution is refused.

    The renderer is a function of resolver output. An object that merely holds a ``hosts``
    attribute proves nothing about who resolved those names.
    """

    class FakeResolution:
        scope = GROUP
        hosts = ("web-01",)
        pattern = "webservers"

    stand_in = FakeResolution()
    with pytest.raises(PromptRenderError):
        render_approval_prompt(command=_COMMAND, resolution=stand_in)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "host", ["web-01\nweb-99", "web-01\x1b[2K", "web-01\r", "web\u2028x", "web 01"]
)
def test_a_host_name_with_a_control_character_is_refused(host: str) -> None:
    """A control character would repaint the display, so the bytes read would differ.

    A newline adds a host line the count does not cover. An escape sequence erases a line in a
    terminal channel. U+2028 breaks a line for some readers and not for others. A space is
    refused as well, because the payload separates host names by whitespace: one name with a
    space in it then reads as two hosts.
    """
    with pytest.raises(PromptRenderError):
        render_approval_prompt_for_hosts(command=_COMMAND, hosts=(host,))


@pytest.mark.parametrize("command", ["reboot\r  | true", "\x1b[1Areboot", "reboot\x07"])
def test_a_command_with_a_control_character_is_refused(command: str) -> None:
    """Same rule for the command. A newline is the one exception, and the next test covers it."""
    with pytest.raises(PromptRenderError):
        render_approval_prompt_for_hosts(command=command, hosts=("web-01",))


def test_a_multi_line_command_stays_readable_and_round_trips() -> None:
    """A prefix marks every command line, so a command cannot forge a section of the payload.

    The command below imitates a host line and a header line. Neither one changes the host
    list, and the digest still re-derives from the bytes.
    """
    command = "set -e\n  15. db-01\nHosts: 1\nsystemctl restart nginx"
    payload = render_approval_prompt_for_hosts(command=command, hosts=("web-01",))

    assert "  | set -e\n" in payload.text
    assert "  |   15. db-01\n" in payload.text
    assert payload.hosts == ("web-01",)
    assert "Hosts: 1\n" in payload.text
    assert digest_rendered_prompt(payload.text) == payload.target_digest


def test_the_host_list_matches_the_digested_set_exactly() -> None:
    """The digest sorts and deduplicates, so the display has to as well.

    A list of fifteen lines under a digest of fourteen hosts would let a human count one thing
    and authorize another.
    """
    payload = render_approval_prompt_for_hosts(
        command=_COMMAND, hosts=("web-02", "web-01", "web-02")
    )

    assert payload.hosts == ("web-01", "web-02")
    assert "Hosts: 2" in payload.text
    assert payload.target_digest == compute_target_digest(
        command=_COMMAND, hosts=["web-01", "web-02"]
    )
    assert digest_rendered_prompt(payload.text) == payload.target_digest


@pytest.mark.parametrize("command", ["", "   ", "\n"])
def test_an_empty_command_is_refused(command: str) -> None:
    """No command means nothing to approve. That is an error, not an empty box."""
    with pytest.raises(PromptRenderError):
        render_approval_prompt_for_hosts(command=command, hosts=("web-01",))


@pytest.mark.parametrize("hosts", [(), ("",), ("   ",)])
def test_an_unnamed_host_set_is_refused(hosts: tuple[str, ...]) -> None:
    """A payload that names no host would bind an approval to nothing."""
    with pytest.raises(PromptRenderError):
        render_approval_prompt_for_hosts(command=_COMMAND, hosts=hosts)


def test_a_text_that_nobody_rendered_has_no_digest() -> None:
    """A free-form sentence is not a payload. It gets an error and never a digest."""
    with pytest.raises(PromptRenderError):
        digest_rendered_prompt("Please restart nginx on the web tier.")


def test_the_renderer_stays_on_the_executor_side_of_the_boundary() -> None:
    """The module is pure. #18 moves the file and changes nothing.

    A renderer built with agent-process access means a human approves a payload that the
    process beside the model assembled. This test reads the source, because an import is easy
    to add later and the consequence is invisible at that moment.
    """
    source = Path(str(prompt_module.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: list[str] = []
    read_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Attribute):
            read_names.add(node.attr)
        elif isinstance(node, ast.Name):
            read_names.add(node.id)

    for module in imported:
        for root in _FORBIDDEN_IMPORT_ROOTS:
            assert module != root and not module.startswith(f"{root}."), module

    assert not read_names & _FORBIDDEN_READS
