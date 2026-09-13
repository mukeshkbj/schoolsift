from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from schoolsift.demo_store import DemoStore

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"

EXPECTED_FILES = sorted((DEMO_DIR / "expected").glob("*.json"))


@pytest.fixture()
def store() -> DemoStore:
    return DemoStore(DEMO_DIR)


@pytest.fixture(params=EXPECTED_FILES, ids=lambda p: p.stem)
def expected(request) -> dict:
    return json.loads(request.param.read_text())


def process(store: DemoStore, message_id: str):
    return store.process([message_id])[0]


def test_all_six_fixtures_present(store: DemoStore):
    assert len(EXPECTED_FILES) == 6
    assert {m.id for m in store.messages} == {
        "msg-field-trip",
        "msg-early-dismissal",
        "msg-newsletter",
        "msg-unknown-source",
        "msg-date-conflict",
        "msg-payment-signature",
    }


class TestFixtureSemantics:
    def test_expected_semantics(self, store: DemoStore, expected: dict):
        packet = process(store, expected["message_id"])
        assert packet.source_message_id == expected["message_id"]
        assert packet.child == expected["child"]
        assert packet.information_only == expected["information_only"]
        assert packet.urgency == expected["urgency"]

        if expected["deadline"] is None:
            assert packet.deadline is None
        else:
            assert packet.deadline == datetime.fromisoformat(
                expected["deadline"].replace("Z", "+00:00")
            )

        heads = packet.head_proposals()
        assert sorted(p.payload.kind for p in heads) == sorted(
            expected["proposal_kinds"]
        )
        assert sorted(
            p.payload.reason for p in heads if p.payload.kind == "escalation"
        ) == sorted(expected["escalation_reasons"])
        for forbidden in expected["forbidden_kinds"]:
            assert all(p.payload.kind != forbidden for p in heads)

        if needle := expected.get("uncertainties_include"):
            assert any(needle.lower() in u.lower() for u in packet.uncertainties), (
                packet.uncertainties
            )

        assert len(packet.evidence) >= expected["min_evidence"]

    def test_field_trip_pdf_fields_come_from_real_acroform(self, store: DemoStore):
        packet = process(store, "msg-field-trip")
        pdf = next(p for p in packet.head_proposals() if p.payload.kind == "pdf_form")
        assert pdf.payload.document_name == "field-trip-permission.pdf"
        assert {"student_name", "guardian_name"} <= set(pdf.payload.fields)
        assert pdf.payload.fields["student_name"] == "Maya"

    def test_early_dismissal_calendar_time(self, store: DemoStore):
        packet = process(store, "msg-early-dismissal")
        cal = next(p for p in packet.head_proposals() if p.payload.kind == "calendar")
        assert cal.payload.starts_at == datetime.fromisoformat(
            "2026-09-17T12:40:00+00:00"
        )
        assert "Leo" in cal.payload.title

    def test_injection_never_produces_executable_proposal(self, store: DemoStore):
        packet = process(store, "msg-payment-signature")
        heads = packet.head_proposals()
        assert heads, "expected escalation proposals"
        assert all(p.payload.kind == "escalation" for p in heads)
        reasons = {p.payload.reason for p in heads}
        assert {"payment", "signature"} <= reasons

    def test_date_conflict_never_proposes_calendar(self, store: DemoStore):
        packet = process(store, "msg-date-conflict")
        heads = packet.head_proposals()
        assert all(p.payload.kind == "escalation" for p in heads)
        assert packet.uncertainties


class TestProcessingBehavior:
    def test_process_all_and_idempotent(self, store: DemoStore):
        first = store.process(None)
        assert len(first) == 6
        again = store.process(None)
        assert [p.id for p in again] == [p.id for p in first]

    def test_unknown_message_rejected(self, store: DemoStore):
        from schoolsift.errors import NotFoundError

        with pytest.raises(NotFoundError):
            store.process(["msg-does-not-exist"])

    def test_reset_clears_packets_and_outcomes(self, store: DemoStore):
        store.process(None)
        packet = store.process(["msg-field-trip"])[0]
        head = packet.head_proposals()[0]
        store.approve(head.id, version=head.version, payload_hash=head.payload_hash)
        assert store.state().outcomes
        store.reset()
        assert store.state().packets == []
        assert store.state().outcomes == []
