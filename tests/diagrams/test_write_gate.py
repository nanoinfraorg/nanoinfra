"""The diagram write gate -- nanoinfraorg/nanoinfra#95, #96.

``if dry_run:`` was the whole gate, and ``dry_run`` is a parameter the **model** supplies in its own
call. Nothing recorded that a preview happened, nothing compared the applied payload against the
previewed one, and nothing represented an operator's answer. Measured: preview one payload, apply a
different one, accepted with no complaint -- and a first-and-only call with ``dry_run=false`` emptied a
diagram completely. No gate, no audit, no undo.

`capability_class = "mutate.local"` is not in `_GATED_CLASSES`, so the capability gate never sees a
diagram write either, and the audit viewer offers a `mutate.local` filter that had nothing to show.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.diagrams.write_gate import (
    PreviewOutcome,
    consume_preview,
    payload_digest,
    record_preview,
)


def _payload(node_ids: list[str]) -> dict:
    return {
        "name": "Topology",
        "targets": [],
        "nodes": [{"id": node_id, "position": {"x": 0, "y": 0}} for node_id in node_ids],
        "edges": [],
    }


class TestAPreviewBindsTheApply:
    def test_an_apply_with_no_preview_is_refused(self, tmp_path: Path) -> None:
        """The sharp end of #95: one call, the first call, and a diagram's content is gone."""
        outcome = consume_preview(tmp_path, "abc", payload_digest(_payload(["web"])))

        assert outcome is PreviewOutcome.NO_PREVIEW

    def test_an_apply_of_what_was_previewed_is_allowed(self, tmp_path: Path) -> None:
        payload = _payload(["web", "cache"])
        record_preview(tmp_path, "abc", payload_digest(payload), revision=3)

        assert consume_preview(tmp_path, "abc", payload_digest(payload)) is PreviewOutcome.OK

    def test_an_apply_of_a_different_payload_is_refused(self, tmp_path: Path) -> None:
        record_preview(tmp_path, "abc", payload_digest(_payload(["web", "cache"])), revision=3)

        outcome = consume_preview(tmp_path, "abc", payload_digest(_payload(["web"])))

        assert outcome is PreviewOutcome.MISMATCH

    def test_a_preview_is_spent_once(self, tmp_path: Path) -> None:
        """A second apply has to preview again, so one answer authorizes one write."""
        payload = _payload(["web"])
        record_preview(tmp_path, "abc", payload_digest(payload), revision=1)

        assert consume_preview(tmp_path, "abc", payload_digest(payload)) is PreviewOutcome.OK
        assert consume_preview(tmp_path, "abc", payload_digest(payload)) is PreviewOutcome.NO_PREVIEW

    def test_a_preview_for_another_diagram_does_not_authorize_this_one(self, tmp_path: Path) -> None:
        payload = _payload(["web"])
        record_preview(tmp_path, "other", payload_digest(payload), revision=1)

        assert consume_preview(tmp_path, "abc", payload_digest(payload)) is PreviewOutcome.NO_PREVIEW

    def test_a_stale_preview_is_refused(self, tmp_path: Path) -> None:
        """A confirmation from hours ago is not a confirmation of what is on disk now."""
        payload = _payload(["web"])
        record_preview(tmp_path, "abc", payload_digest(payload), revision=1)
        path = next((tmp_path / ".diagram-previews").glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_at"] = 0.0
        path.write_text(json.dumps(record), encoding="utf-8")

        assert consume_preview(tmp_path, "abc", payload_digest(payload)) is PreviewOutcome.STALE

    def test_the_digest_survives_key_order(self, tmp_path: Path) -> None:
        """The model does not control field order, so order must not decide the answer."""
        first = {"name": "A", "nodes": [], "edges": [], "targets": []}
        second = {"edges": [], "targets": [], "nodes": [], "name": "A"}

        assert payload_digest(first) == payload_digest(second)

    def test_the_record_holds_no_payload(self, tmp_path: Path) -> None:
        """A digest, not a copy: the record sits in the workspace the agent can read."""
        payload = _payload(["web-with-a-recognisable-name"])
        record_preview(tmp_path, "abc", payload_digest(payload), revision=1)

        text = next((tmp_path / ".diagram-previews").glob("*.json")).read_text(encoding="utf-8")

        assert "web-with-a-recognisable-name" not in text


class TestTheStoreDetectsAWriteItDidNotMake:
    """Four writers reach the files and nothing policed three of them -- #96.

    `write_file` is ungated and `exec` can `rm`, so "a diagram change must be approved" had no
    chokepoint at all. That cannot be prevented from inside the store, so the honest half is
    detection: a diagram whose content the store did not write is reported rather than rendered as
    the operator's own. A direct write can forge `updated_at` to any history.
    """

    def test_a_hand_written_change_is_flagged(self, tmp_path: Path) -> None:
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        diagram = store.create({"name": "Real", "nodes": [], "edges": []})
        path = next((tmp_path / "diagrams").glob("*.json"))
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["diagram"]["name"] = "Rewritten by the agent"
        wrapper["updated_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(wrapper), encoding="utf-8")

        summary = next(s for s in store.list_diagrams() if s.id == diagram.id)

        assert summary.status == "modified_outside"

    def test_a_store_write_is_not_flagged(self, tmp_path: Path) -> None:
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        diagram = store.create({"name": "Real", "nodes": [], "edges": []})
        store.update(diagram.id, {"name": "Renamed"})

        summary = next(s for s in store.list_diagrams() if s.id == diagram.id)

        assert summary.status == "ok"

    def test_an_unreadable_file_appears_in_the_gallery_rather_than_vanishing(
        self,
        tmp_path: Path,
    ) -> None:
        """A silent disappearance is the worst answer available."""
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        diagram = store.create({"name": "Real", "nodes": [], "edges": []})
        path = next((tmp_path / "diagrams").glob("*.json"))
        path.write_text("{ this is not json", encoding="utf-8")

        summaries = store.list_diagrams()

        entry = next((s for s in summaries if s.id == diagram.id), None)
        assert entry is not None, "the operator got no error at all before this"
        assert entry.status == "unreadable"
        assert entry.name, "and it still has something to click on"


class TestADiagramWriteIsAudited:
    def test_a_write_reaches_the_audit_log(self, tmp_path: Path, monkeypatch) -> None:
        """The viewer offers a `mutate.local` filter that had nothing to show (#95)."""
        from nanoinfra.diagrams.write_gate import record_diagram_write

        written: list[dict] = []

        class _Audit:
            def record(self, **fields: object) -> dict:
                written.append(dict(fields))
                return dict(fields)

        monkeypatch.setattr(
            "nanoinfra.diagrams.write_gate._audit_store",
            lambda: _Audit(),
        )

        record_diagram_write(diagram_id="abc", tool="update_diagram", summary="+ node 'web'")

        assert len(written) == 1
        assert written[0]["capability_class"] == "mutate.local"
        assert written[0]["tool"] == "update_diagram"
        assert "abc" in str(written[0]["command"])

    def test_a_failed_audit_write_does_not_stop_the_write(self, tmp_path: Path, monkeypatch) -> None:
        """An operator-confirmed edit must not be lost because the audit disk is full.

        The opposite rule holds for a *gated* action, where a missing record turns a refusal into a
        pass. A diagram write is not gated, so the risk here runs the other way.
        """
        from nanoinfra.diagrams.write_gate import record_diagram_write

        def explode() -> object:
            raise OSError("no space left on device")

        monkeypatch.setattr("nanoinfra.diagrams.write_gate._audit_store", explode)

        record_diagram_write(diagram_id="abc", tool="update_diagram", summary="+ node 'web'")
