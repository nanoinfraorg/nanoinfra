"""Device memory: the store half (#223, #224, #225, #227, #228)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nanoinfra.servers.notes import (
    AUTHOR_AGENT,
    AUTHOR_OPERATOR,
    MAX_ENTRY_CHARS,
    MAX_FILE_CHARS,
    MAX_LIVE_CHARS,
    CredentialInNoteError,
    ServerNotesError,
    ServerNotesStore,
    parse_notes,
    sanitize_author,
    screen_for_credentials,
)
from nanoinfra.servers.store import ServerStore


def _server(tmp_path: Path, name: str = "barrahome") -> tuple[ServerStore, ServerNotesStore, str]:
    store = ServerStore(tmp_path)
    server = store.create({"name": name, "providerId": "ssh", "config": {"host": "10.0.0.5"}})
    return store, ServerNotesStore(tmp_path), server.id


def test_notes_file_is_a_sibling_keyed_by_id(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    notes.append(server_id, author="agent", kind=AUTHOR_AGENT, title="t", body="b")

    path = notes.path(server_id)
    assert path is not None
    assert path.name == f"{server_id}.NOTES.md"
    assert path.parent == tmp_path / "servers"
    # The store enumerates with glob("*.json"), so the notes file must stay invisible to it.
    assert [summary.id for summary in store.list_servers()] == [server_id]


def test_an_invalid_id_gets_no_path_and_no_write(tmp_path: Path) -> None:
    notes = ServerNotesStore(tmp_path)
    assert notes.path("../../etc/passwd") is None
    with pytest.raises(ServerNotesError):
        notes.append("../../etc/passwd", author="a", kind=AUTHOR_AGENT, title="t", body="b")


def test_authorship_is_in_the_entry_and_marks_a_human(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="alberto",
        kind=AUTHOR_OPERATOR,
        title="journald is deliberate",
        body="The debug level is on purpose. Do not change it.",
    )
    notes.append(
        server_id,
        author="sre-copilot",
        kind=AUTHOR_AGENT,
        title="disk pressure",
        body="Vacuumed /var/log/journal from 14G.",
    )

    text = notes.read(server_id)
    assert "· alberto (operator) · journald is deliberate" in text
    assert "· sre-copilot · disk pressure" in text

    human, agent = notes.entries(server_id)
    assert (human.author, human.is_operator) == ("alberto", True)
    assert (agent.author, agent.is_operator) == ("sre-copilot", False)


def test_an_agent_author_cannot_forge_the_operator_mark() -> None:
    """The precedence marker is not a string an author may end with (#228)."""
    assert sanitize_author("attacker (operator)", kind=AUTHOR_AGENT) == "attacker"
    assert sanitize_author("a · b", kind=AUTHOR_AGENT) == "a b"
    assert sanitize_author("   ", kind=AUTHOR_AGENT) == "agent"


def test_a_note_written_by_one_agent_is_readable_by_another(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="sre-copilot",
        kind=AUTHOR_AGENT,
        title="needs sudo -n",
        body="Interactive sudo prompts never answer here.",
    )

    # A different process, a different store instance, the same file.
    other = ServerNotesStore(tmp_path)
    entry = other.entries(server_id)[0]
    assert (entry.author, entry.title) == ("sre-copilot", "needs sudo -n")


# --- the race -------------------------------------------------------------------


def _rmw_append(path: Path, payload: str) -> None:
    """The implementation this design rejects: read the file, add, write it back.

    The yield stands in for the I/O a real read-modify-write does between its two halves. Without
    it the GIL can hide the lost update on a fast machine, which would make the assertion below
    pass for the wrong reason.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    time.sleep(0)
    path.write_text(existing + payload, encoding="utf-8")


def test_concurrent_appends_all_survive_with_their_authors(tmp_path: Path) -> None:
    """Two peers of one plan append at once and neither is lost (#223).

    The second half of this test is what makes the first half load-bearing: the same race, run
    against a read-modify-write writer, loses entries. ``O_APPEND`` is the reason the real one
    does not, and a regression that reverted to a whole-file rewrite would fail here.
    """
    _store, notes, server_id = _server(tmp_path)
    authors = [f"peer-{index % 2}" for index in range(40)]
    barrier = threading.Barrier(len(authors))

    def append(index: int) -> None:
        barrier.wait()
        notes.append(
            server_id,
            author=authors[index],
            kind=AUTHOR_AGENT,
            title=f"finding {index}",
            body=f"observed {index}",
        )

    with ThreadPoolExecutor(max_workers=len(authors)) as pool:
        list(pool.map(append, range(len(authors))))

    entries = notes.entries(server_id)
    assert len(entries) == len(authors)
    assert {entry.title for entry in entries} == {f"finding {i}" for i in range(len(authors))}
    assert {entry.author for entry in entries} == {"peer-0", "peer-1"}

    # The control. Same pressure, a read-modify-write writer, and it loses.
    rmw_path = tmp_path / "servers" / "rmw.md"
    rmw_path.write_text("", encoding="utf-8")
    rmw_barrier = threading.Barrier(len(authors))

    def rmw(index: int) -> None:
        rmw_barrier.wait()
        _rmw_append(rmw_path, f"\n## when · peer · finding {index}\nobserved {index}\n")

    with ThreadPoolExecutor(max_workers=len(authors)) as pool:
        list(pool.map(rmw, range(len(authors))))

    assert len(parse_notes(rmw_path.read_text(encoding="utf-8")).entries) < len(authors)


# --- credentials (#224) ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "the key starts with -----BEGIN OPENSSH PRIVATE KEY----- in /root",
        "connect with password=hunter2hunter2",
        "the token= value in /etc/app.conf",
        "the digest was 0123456789abcdef0123456789abcdef",
        "use AKIAIOSFODNN7EXAMPLE for the sync",
        "the key is ghp_abcdefghijklmnopqrstuvwxyz012345",
        "authorized_keys holds ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDlongenoughblobhere",
    ],
)
def test_a_credential_shaped_note_is_refused_with_a_reason(tmp_path: Path, body: str) -> None:
    _store, notes, server_id = _server(tmp_path)
    with pytest.raises(CredentialInNoteError) as excinfo:
        notes.append(server_id, author="agent", kind=AUTHOR_AGENT, title="creds", body=body)

    # Refused, not masked: nothing was written, and the reason names the shape.
    assert notes.read(server_id) == ""
    assert "Refusing to write this note" in str(excinfo.value)
    assert screen_for_credentials(body) is not None


def test_a_note_that_is_mostly_paths_is_not_refused(tmp_path: Path) -> None:
    """A path is not a credential, and a note about a box is mostly paths."""
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="agent",
        kind=AUTHOR_AGENT,
        title="where the data lives",
        body=(
            "Postgres data is at /var/lib/postgresql/data/base/16384 and the WAL archive at "
            "/srv/backups/postgres/wal/incoming/deep/nested/directory/tree."
        ),
    )
    assert "Postgres data" in notes.read(server_id)


def test_a_human_note_is_not_screened(tmp_path: Path) -> None:
    """The refusal exists for an agent pasting a config it just read, not for a person's own file."""
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="alberto",
        kind=AUTHOR_OPERATOR,
        title="legacy naming",
        body="Ignore the old token= line in /etc/app.conf; it is dead config.",
    )
    assert "token=" in notes.read(server_id)


def test_an_entry_over_the_cap_is_refused(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    with pytest.raises(ServerNotesError) as excinfo:
        notes.append(
            server_id,
            author="agent",
            kind=AUTHOR_AGENT,
            title="transcript",
            body="line\n" * MAX_ENTRY_CHARS,
        )
    assert str(MAX_ENTRY_CHARS) in str(excinfo.value)


# --- revise-own (#228) ----------------------------------------------------------


def test_revise_own_replaces_the_body_and_records_the_revision(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id, author="sre", kind=AUTHOR_AGENT, title="disk pressure", body="Vacuumed to 500M."
    )
    revised = notes.revise_own(
        server_id, author="sre", kind=AUTHOR_AGENT, title="disk pressure", body="It regrows weekly."
    )

    entries = notes.entries(server_id)
    assert len(entries) == 1
    assert entries[0].body == "It regrows weekly."
    assert "(revised " in revised.when


def test_an_agent_may_not_revise_an_operators_entry(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="alberto",
        kind=AUTHOR_OPERATOR,
        title="journald is deliberate",
        body="Do not change it.",
    )
    with pytest.raises(ServerNotesError) as excinfo:
        notes.revise_own(
            server_id,
            author="alberto",
            kind=AUTHOR_AGENT,
            title="journald is deliberate",
            body="actually it is a mistake",
        )

    assert "only its own entries" in str(excinfo.value)
    assert notes.entries(server_id)[0].body == "Do not change it."


def test_an_agent_may_not_revise_another_agents_entry(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.append(server_id, author="sre", kind=AUTHOR_AGENT, title="quirk", body="first")
    with pytest.raises(ServerNotesError):
        notes.revise_own(
            server_id, author="dba", kind=AUTHOR_AGENT, title="quirk", body="overwritten"
        )
    assert notes.entries(server_id)[0].body == "first"


# --- rotation (#227) ------------------------------------------------------------


def test_rotation_archives_the_oldest_and_deletes_nothing(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    for index in range(30):
        notes.append(
            server_id,
            author="agent",
            kind=AUTHOR_AGENT,
            title=f"finding {index}",
            body="x " * 900,
        )

    live = notes.read(server_id)
    archived = parse_notes(notes.read_archive(server_id)).entries
    assert len(live) <= MAX_LIVE_CHARS
    assert archived, "rotation moved entries out"
    # Nothing is lost: every title is in one file or the other, and the archive keeps file order.
    all_titles = {entry.title for entry in parse_notes(live).entries} | {
        entry.title for entry in archived
    }
    assert all_titles == {f"finding {index}" for index in range(30)}
    assert [entry.title for entry in archived] == sorted(
        (entry.title for entry in archived), key=lambda title: int(title.split()[1])
    )


def test_rotation_keeps_the_operators_entry_whatever_its_age(tmp_path: Path) -> None:
    """An operator's note is what an agent reads before it acts, so ageing it out is a loss (#228)."""
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="alberto",
        kind=AUTHOR_OPERATOR,
        title="do not touch grub",
        body="The serial console line is deliberate.",
    )
    for index in range(30):
        notes.append(
            server_id, author="agent", kind=AUTHOR_AGENT, title=f"f{index}", body="x " * 900
        )

    live = notes.entries(server_id)
    assert [entry.title for entry in live if entry.is_operator] == ["do not touch grub"]
    assert len(notes.read(server_id)) <= MAX_LIVE_CHARS


# --- the human's own file -------------------------------------------------------


def test_a_human_edit_is_not_overwritten_by_the_next_append(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.replace(
        server_id,
        "Hand-written context about this box.\n\n"
        "## 2026-01-01 · alberto (operator) · owner\n"
        "Ask #infra before rebooting.\n",
    )
    notes.append(server_id, author="agent", kind=AUTHOR_AGENT, title="uptime", body="94 days.")

    text = notes.read(server_id)
    assert "Hand-written context about this box." in text
    assert "Ask #infra before rebooting." in text
    assert "94 days." in text
    parsed = parse_notes(text)
    assert parsed.preamble == "Hand-written context about this box."
    assert [entry.title for entry in parsed.entries] == ["owner", "uptime"]


def test_a_preamble_survives_rotation(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.replace(server_id, "Owned by the platform team.\n")
    for index in range(30):
        notes.append(
            server_id, author="agent", kind=AUTHOR_AGENT, title=f"f{index}", body="x " * 900
        )
    assert parse_notes(notes.read(server_id)).preamble == "Owned by the platform team."


def test_a_whole_file_write_over_the_cap_is_refused(tmp_path: Path) -> None:
    _store, notes, server_id = _server(tmp_path)
    with pytest.raises(ServerNotesError):
        notes.replace(server_id, "x" * (MAX_FILE_CHARS + 1))


def test_a_heading_inside_a_body_is_demoted_rather_than_parsed_as_an_entry(
    tmp_path: Path,
) -> None:
    _store, notes, server_id = _server(tmp_path)
    notes.append(
        server_id,
        author="agent",
        kind=AUTHOR_AGENT,
        title="layout",
        body="## not an entry\nstill the same note",
    )
    entries = notes.entries(server_id)
    assert len(entries) == 1
    assert "### not an entry" in entries[0].body


# --- the record's scalar (#225) -------------------------------------------------


def test_appending_stamps_notes_updated_at_without_touching_updated_at(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    before = store.get(server_id)
    assert before is not None and before.notes_updated_at is None

    notes.append(server_id, author="agent", kind=AUTHOR_AGENT, title="t", body="b")

    after = store.get(server_id)
    assert after is not None
    assert after.notes_updated_at
    # A note is not the record, and the gallery sorts on updated_at.
    assert after.updated_at == before.updated_at
    assert store.list_servers()[0].notes_updated_at == after.notes_updated_at


def test_renaming_a_server_keeps_its_memory(tmp_path: Path) -> None:
    """Keyed by id, so a rename orphans nothing (#223) and the marker survives (#225)."""
    store, notes, server_id = _server(tmp_path)
    notes.append(server_id, author="agent", kind=AUTHOR_AGENT, title="quirk", body="b")
    stamped = store.get(server_id)
    assert stamped is not None

    store.update(server_id, {"name": "renamed", "providerId": "ssh"})

    renamed = store.get(server_id)
    assert renamed is not None
    assert renamed.name == "renamed"
    assert renamed.notes_updated_at == stamped.notes_updated_at
    assert notes.entries(server_id)[0].title == "quirk"


def test_a_client_payload_cannot_claim_a_server_has_memory(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    server = store.create({"name": "box", "providerId": "ssh", "notesUpdatedAt": "1999-01-01"})
    stored = store.get(server.id)
    assert stored is not None and stored.notes_updated_at is None
