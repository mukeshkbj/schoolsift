from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from schoolsift.domain import (
    ActionPacket,
    ReplyProposal,
    payload_digest,
)
from schoolsift.errors import (
    ConflictError,
    NotApprovableError,
    NotFoundError,
)
from schoolsift.sqlite_store import SQLiteStore


def make_packet(pid: str = "packet-1", mid: str = "msg-1") -> ActionPacket:
    payload = ReplyProposal(
        kind="reply",
        recipient="school@example.org",
        subject="Re: Question",
        body="Reply body.",
    )
    return ActionPacket(
        id=pid,
        source_message_id=mid,
        sender="School Office <school@example.org>",
        subject="Question",
        summary="Summary.",
        child=None,
        deadline=None,
        urgency="none",
        information_only=False,
        evidence=[],
        uncertainties=[],
        proposals=[
            {
                "id": f"{pid}-prop-1",
                "version": 1,
                "status": "proposed",
                "payload": payload.model_dump(mode="json"),
                "payload_hash": payload_digest(payload),
            }
        ],
    )


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(tmp_path / "db.sqlite")
    s.initialize()
    return s


def seed_message(
    store: SQLiteStore,
    household_id: str,
    *,
    status: str = "awaiting_agent",
    confirmed: int = 1,
) -> None:
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO connections (id, household_id, provider,"
            " provider_subject, email, status, last_sync_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                "conn-1",
                household_id,
                "gmail",
                "sub-1",
                "me@example.com",
                "connected",
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        con.execute(
            "INSERT INTO messages (id, household_id, connection_id,"
            " provider_message_id, thread_id, sender_name, sender_email, reply_to,"
            " subject, received_at, source_confirmed, status, body_ref,"
            " attachment_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "msg-1",
                household_id,
                "conn-1",
                "pm-1",
                "thread-1",
                "School Office",
                "school@example.org",
                "school@example.org",
                "Question",
                datetime.now(UTC).isoformat(),
                confirmed,
                status,
                "ref-body",
                '["doc-1", "doc-2"]',
            ),
        )
        con.execute(
            "INSERT INTO documents (id, household_id, message_id, name, mime,"
            " content_ref, acroform_fields) VALUES (?,?,?,?,?,?,?)",
            (
                "doc-1",
                household_id,
                "msg-1",
                "form.pdf",
                "application/pdf",
                "ref-pdf",
                '["student_name", "Parent Signature"]',
            ),
        )
        con.execute(
            "INSERT INTO documents (id, household_id, message_id, name, mime,"
            " content_ref, acroform_fields) VALUES (?,?,?,?,?,?,?)",
            (
                "doc-2",
                household_id,
                "msg-1",
                "note.txt",
                "text/plain",
                "ref-txt",
                "[]",
            ),
        )
        con.commit()
    finally:
        con.close()


def test_starts_empty(store):
    assert store.get_household() is None
    assert store.list_packets("any") == []


def test_household_persists_across_instances(tmp_path):
    db = tmp_path / "db.sqlite"
    SQLiteStore(db).initialize()
    s1 = SQLiteStore(db)
    h = s1.create_household(name="Home", timezone="America/Los_Angeles")
    s2 = SQLiteStore(db)
    assert s2.get_household() is not None
    assert s2.get_household().id == h.id


def test_local_owner_can_hold_multiple_households(store):
    a = store.create_household(name="A", timezone="UTC")
    b = store.create_household(name="B", timezone="UTC")
    memberships = store.list_memberships_for_user("local-caregiver")
    assert {m.household_id for m in memberships} == {a.id, b.id}


def test_bad_timezone_rejected(store):
    from schoolsift.errors import BadRequestError

    with pytest.raises(BadRequestError):
        store.create_household(name="A", timezone="Not/AZone")


def test_add_and_list_children(store):
    h = store.create_household(name="A", timezone="UTC")
    c = store.add_child(h.id, name="Kid", school="School", grade="2")
    kids = store.list_children(h.id)
    assert [k.id for k in kids] == [c.id]
    assert kids[0].school == "School"


def test_add_child_requires_known_household(store):
    with pytest.raises(ConflictError):
        store.add_child("nope", name="K", school="S", grade="1")


def test_packet_round_trip_and_proposal_flow(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    packet = make_packet()
    store.save_packet(h.id, packet)
    assert store.list_packets(h.id)[0].id == "packet-1"
    assert store.get_packet(h.id, "packet-1").proposals[0].payload.recipient == (
        "school@example.org"
    )

    prop = packet.proposals[0]
    edited = store.edit_proposal(
        h.id,
        prop.id,
        expected_version=1,
        payload=ReplyProposal(
            kind="reply",
            recipient="school@example.org",
            subject="Re: Question",
            body="Edited.",
        ),
    )
    assert edited.version == 2
    assert edited.status == "proposed"

    with pytest.raises(ConflictError):
        store.approve_proposal(h.id, prop.id, version=1, payload_hash=prop.payload_hash)

    approved, execution = store.approve_proposal(
        h.id, prop.id, version=2, payload_hash=edited.payload_hash
    )
    assert approved.status == "approved"
    assert execution.proposal_id == prop.id
    assert execution.proposal_version == 2
    assert execution.status == "pending_dispatch"

    with pytest.raises(ConflictError):
        store.reject_proposal(
            h.id, prop.id, version=2, payload_hash=edited.payload_hash
        )

    reloaded = SQLiteStore(store.path)
    head = next(
        v for v in reloaded.get_packet(h.id, "packet-1").proposals if v.version == 2
    )
    assert head.status == "approved"


def test_escalation_not_approvable_via_store(store):
    from schoolsift.domain import EscalationProposal, ProposalVersion

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    esc = EscalationProposal(kind="escalation", reason="payment", detail="fee")
    packet = make_packet("packet-e")
    packet.proposals = [
        ProposalVersion(
            id="packet-e-prop-1",
            version=1,
            status="proposed",
            payload=esc,
            payload_hash=payload_digest(esc),
        )
    ]
    store.save_packet(h.id, packet)
    with pytest.raises(NotApprovableError):
        store.approve_proposal(
            h.id,
            "packet-e-prop-1",
            version=1,
            payload_hash=packet.proposals[0].payload_hash,
        )


def test_record_getters_and_packet_lookup(store):
    from schoolsift.errors import NotFoundError

    h = store.create_household(name="A", timezone="UTC")
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO connections (id, household_id, provider,"
            " provider_subject, email, status, last_sync_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                "conn-1",
                h.id,
                "gmail",
                "sub-1",
                "me@example.com",
                "connected",
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        con.execute(
            "INSERT INTO messages (id, household_id, connection_id,"
            " provider_message_id, thread_id, sender_name, sender_email, reply_to,"
            " subject, received_at, source_confirmed, body_ref, attachment_ids)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "msg-1",
                h.id,
                "conn-1",
                "pm1",
                "thread-1",
                "Office",
                "o@example.org",
                "o@example.org",
                "Hi",
                datetime.now(UTC).isoformat(),
                1,
                None,
                '["doc-1"]',
            ),
        )
        con.execute(
            "INSERT INTO documents (id, household_id, message_id, name, mime,"
            " content_ref, acroform_fields) VALUES (?,?,?,?,?,?,?)",
            (
                "doc-1",
                h.id,
                "msg-1",
                "form.pdf",
                "application/pdf",
                "docs/form.pdf",
                '["student_name"]',
            ),
        )
        con.execute(
            "INSERT INTO calendar_records (id, household_id, title, starts_at,"
            " ends_at) VALUES (?,?,?,?,?)",
            (
                "evt-1",
                h.id,
                "Dentist",
                "2026-09-25T15:00:00+00:00",
                "2026-09-25T16:00:00+00:00",
            ),
        )
        con.commit()
    finally:
        con.close()

    record = store.get_message_record(h.id, "msg-1")
    assert record.thread_id == "thread-1"
    assert record.attachment_ids == ["doc-1"]

    doc = store.get_document_record(h.id, "doc-1")
    assert doc.acroform_fields == ["student_name"]
    assert doc.content_ref == "docs/form.pdf"
    docs = store.list_document_records(h.id, "msg-1")
    assert [d.id for d in docs] == ["doc-1"]

    assert [r.id for r in store.list_calendar_records(h.id)] == ["evt-1"]

    with pytest.raises(NotFoundError):
        store.get_message_record("hh-other", "msg-1")
    with pytest.raises(NotFoundError):
        store.get_document_record("hh-other", "doc-1")
    with pytest.raises(NotFoundError):
        store.list_document_records("hh-other", "msg-1")

    assert store.get_packet_for_message(h.id, "msg-1") is None
    store.save_packet(h.id, make_packet())
    packet = store.get_packet_for_message(h.id, "msg-1")
    assert packet is not None and packet.id == "packet-1"


def insert_household(store: SQLiteStore, hid: str) -> None:
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO households (id, name, timezone, created_at) VALUES (?,?,?,?)",
            (hid, "Other", "UTC", datetime.now(UTC).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def test_packet_get_is_tenant_scoped(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    insert_household(store, "hh-other")
    store.save_packet(h.id, make_packet())
    from schoolsift.errors import NotFoundError

    with pytest.raises(NotFoundError):
        store.get_packet("hh-other", "packet-1")


def test_proposal_mutation_denied_cross_household(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    insert_household(store, "hh-other")
    packet = make_packet()
    store.save_packet(h.id, packet)
    prop = packet.proposals[0]
    from schoolsift.errors import NotFoundError

    with pytest.raises(NotFoundError):
        store.approve_proposal(
            "hh-other", prop.id, version=1, payload_hash=prop.payload_hash
        )
    with pytest.raises(NotFoundError):
        store.reject_proposal(
            "hh-other", prop.id, version=1, payload_hash=prop.payload_hash
        )
    with pytest.raises(NotFoundError):
        store.edit_proposal(
            "hh-other",
            prop.id,
            expected_version=1,
            payload=prop.payload,
        )
    head = store.get_packet(h.id, packet.id).proposals[0]
    assert head.status == "proposed"


def test_save_packet_cannot_steal_other_tenants_id(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    insert_household(store, "hh-other")
    store.save_packet(h.id, make_packet())
    with pytest.raises(ConflictError):
        store.save_packet("hh-other", make_packet())
    assert store.list_packets("hh-other") == []
    assert store.list_packets(h.id)[0].id == "packet-1"


def test_save_packet_rejects_foreign_source_message(store):
    h = store.create_household(name="A", timezone="UTC")
    insert_household(store, "hh-other")
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO connections (id, household_id, provider,"
            " provider_subject, email, status, last_sync_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                "conn-x",
                "hh-other",
                "gmail",
                "sub-x",
                "x@example.com",
                "connected",
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        con.execute(
            "INSERT INTO messages (id, household_id, connection_id,"
            " provider_message_id, thread_id, sender_name, sender_email,"
            " reply_to, subject, received_at, source_confirmed, body_ref,"
            " attachment_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "msg-foreign",
                "hh-other",
                "conn-x",
                "pm",
                "t",
                "S",
                "s@x.org",
                "s@x.org",
                "Hi",
                datetime.now(UTC).isoformat(),
                1,
                None,
                "[]",
            ),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(ConflictError):
        store.save_packet(h.id, make_packet(mid="msg-foreign"))


def test_concurrent_approval_is_idempotent(store):
    import threading

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    packet = make_packet()
    store.save_packet(h.id, packet)
    prop = packet.proposals[0]
    barrier = threading.Barrier(2)
    executions: list[str] = []

    def attempt() -> None:
        barrier.wait(timeout=10)
        _, execution = store.approve_proposal(
            h.id, prop.id, version=1, payload_hash=prop.payload_hash
        )
        executions.append(execution.id)

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join(15)
    t2.join(15)
    assert len(executions) == 2
    assert len(set(executions)) == 1
    head = store.get_packet(h.id, packet.id).proposals[0]
    assert head.status == "approved"
    assert len(store.list_executions(h.id)) == 1


def test_oauth_state_single_use_and_expiry(store):
    h = store.create_household(name="A", timezone="UTC")
    raw = store.create_oauth_state(h.id, "gmail")
    assert isinstance(raw, str) and len(raw) > 20
    assert raw not in _state_hashes(store)

    consumed = store.consume_oauth_state(raw)
    assert consumed.household_id == h.id
    assert consumed.provider == "gmail"
    with pytest.raises(ConflictError):
        store.consume_oauth_state(raw)
    with pytest.raises(ConflictError):
        store.consume_oauth_state("never-issued")


def _state_hashes(store: SQLiteStore) -> list[str]:
    con = sqlite3.connect(store.path)
    try:
        return [
            r[0] for r in con.execute("SELECT state_hash FROM oauth_states").fetchall()
        ]
    finally:
        con.close()


def test_oauth_state_expires(store):
    h = store.create_household(name="A", timezone="UTC")
    raw = store.create_oauth_state(h.id, "outlook")
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "UPDATE oauth_states SET expires_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(ConflictError):
        store.consume_oauth_state(raw)


def test_upsert_connection_identity_unique_and_repeatable(store):
    h = store.create_household(name="A", timezone="UTC")
    c1 = store.upsert_connection(
        h.id, provider="gmail", provider_subject="sub-1", email="a@x.com"
    )
    assert c1.status == "pending"
    c1 = store.set_connection_status(h.id, c1.id, "connected")
    assert c1.status == "connected"
    again = store.upsert_connection(
        h.id, provider="gmail", provider_subject="sub-1", email="a@x.com"
    )
    assert again.id == c1.id
    assert again.status == "pending"
    other = store.upsert_connection(
        h.id, provider="gmail", provider_subject="sub-2", email="b@x.com"
    )
    assert other.id != c1.id
    assert len(store.list_connections(h.id)) == 2


def test_disconnect_connection_marks_status(store):
    h = store.create_household(name="A", timezone="UTC")
    insert_household(store, "hh-other")
    c = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="a@x.com"
    )
    out = store.disconnect_connection(h.id, c.id)
    assert out.status == "disconnected"
    from schoolsift.errors import NotFoundError

    with pytest.raises(NotFoundError):
        store.disconnect_connection("hh-other", c.id)
    with pytest.raises(NotFoundError):
        store.disconnect_connection(h.id, "missing")


def test_migration_preserves_existing_data(tmp_path):
    from schoolsift.sqlite_store import SCHEMA_V1, SCHEMA_VERSION

    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_V1)
    con.execute("DROP TABLE schema_version")
    con.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version (version) VALUES (1)")
    con.execute(
        "INSERT INTO households (id, name, timezone, created_at)"
        " VALUES ('hh-old', 'Old Family', 'UTC', '2026-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO children (id, household_id, name, school, grade,"
        " created_at) VALUES ('ch-old', 'hh-old', 'Kid', 'School', '3',"
        " '2026-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO connections (id, household_id, provider, email, status,"
        " last_sync_at, created_at) VALUES ('conn-old', 'hh-old', 'gmail',"
        " 'a@x.com', 'connected', NULL, '2026-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO messages (id, household_id, connection_id,"
        " provider_message_id, thread_id, sender_name, sender_email,"
        " reply_to, subject, received_at, source_confirmed, body_ref,"
        " attachment_ids) VALUES ('msg-old', 'hh-old', 'conn-old', 'pm-old',"
        " 't-old', 'Office', 'o@x.org', 'o@x.org', 'Hi',"
        " '2026-01-01T00:00:00+00:00', 0, NULL, '[]')"
    )
    con.commit()
    con.close()

    store = SQLiteStore(path)
    store.initialize()

    con = sqlite3.connect(path)
    try:
        assert con.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone() == (SCHEMA_VERSION,)
        conn_cols = {r[1] for r in con.execute("PRAGMA table_info(connections)")}
        assert {"provider_subject", "sync_cursor", "sync_status"} <= conn_cols
        msg_cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        assert {"status", "manual_review_reason"} <= msg_cols
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        assert {"oauth_states", "school_sources"} <= tables
    finally:
        con.close()

    assert store.get_household().id == "hh-old"
    assert store.list_children("hh-old")[0].id == "ch-old"
    conn = store.get_connection("hh-old", "conn-old")
    assert conn.status == "connected"
    assert conn.sync_status == "idle"
    msg = store.get_message_record("hh-old", "msg-old")
    assert msg.status == "awaiting_source"
    assert msg.manual_review_reason is None


def test_save_packet_rejects_missing_source(store):
    from schoolsift.errors import NotFoundError

    h = store.create_household(name="A", timezone="UTC")
    with pytest.raises(NotFoundError):
        store.save_packet(h.id, make_packet(mid="msg-missing"))


def test_save_packet_rejects_duplicate_source(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    store.save_packet(h.id, make_packet())
    with pytest.raises(ConflictError):
        store.save_packet(h.id, make_packet(pid="packet-2"))


def test_save_processed_packet_marks_processed(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    saved = store.save_processed_packet(h.id, "conn-1", "pm-1", make_packet())
    assert saved.id == "packet-1"
    record = store.get_message_record(h.id, "msg-1")
    assert record.status == "processed"
    assert record.manual_review_reason is None
    assert store.get_packet_for_message(h.id, "msg-1").id == "packet-1"


def test_save_processed_packet_is_idempotent(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    first = store.save_processed_packet(h.id, "conn-1", "pm-1", make_packet())
    again = store.save_processed_packet(
        h.id, "conn-1", "pm-1", make_packet(pid="packet-2")
    )
    assert again.id == first.id
    assert len(store.list_packets(h.id)) == 1


def test_save_processed_packet_requires_awaiting_agent(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id, status="awaiting_source", confirmed=0)
    with pytest.raises(ConflictError):
        store.save_processed_packet(h.id, "conn-1", "pm-1", make_packet())
    record = store.get_message_record(h.id, "msg-1")
    assert record.status == "awaiting_source"


def test_save_processed_packet_rejects_mismatched_source(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    with pytest.raises(ConflictError):
        store.save_processed_packet(
            h.id, "conn-1", "pm-1", make_packet(mid="msg-other")
        )
    assert store.get_message_record(h.id, "msg-1").status == "awaiting_agent"


def test_save_processed_packet_rolls_back_on_insert_failure(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO packets (id, household_id, source_message_id, data,"
            " updated_at) VALUES ('packet-1', ?, 'msg-elsewhere', '{}', ?)",
            (h.id, datetime.now(UTC).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(ConflictError):
        store.save_processed_packet(h.id, "conn-1", "pm-1", make_packet())
    assert store.get_message_record(h.id, "msg-1").status == "awaiting_agent"


def test_edit_proposal_rejects_arbitrary_recipient(store):
    from schoolsift.errors import UnsafeProposalError

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    packet = make_packet()
    store.save_packet(h.id, packet)
    prop = packet.proposals[0]
    with pytest.raises(UnsafeProposalError):
        store.edit_proposal(
            h.id,
            prop.id,
            expected_version=1,
            payload=ReplyProposal(
                kind="reply",
                recipient="attacker@evil.example",
                subject="Re: Question",
                body="Edited.",
            ),
        )
    edited = store.edit_proposal(
        h.id,
        prop.id,
        expected_version=1,
        payload=ReplyProposal(
            kind="reply",
            recipient="School Office <school@example.org>",
            subject="Re: Question",
            body="Edited.",
        ),
    )
    assert edited.version == 2


def test_edit_proposal_pdf_policy(store):
    from schoolsift.domain import PdfFormProposal
    from schoolsift.errors import UnsafeProposalError

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    packet = make_packet()
    store.save_packet(h.id, packet)
    prop = packet.proposals[0]

    def attempt(payload):
        return store.edit_proposal(h.id, prop.id, expected_version=1, payload=payload)

    def form(**over):
        base = {
            "kind": "pdf_form",
            "recipient": "school@example.org",
            "subject": "Completed form",
            "body": "Attached is the completed form.",
            "document_name": "form.pdf",
            "fields": {"student_name": "Kid"},
        }
        return PdfFormProposal(**{**base, **over})

    with pytest.raises(UnsafeProposalError):
        attempt(form(document_name="missing.pdf"))
    with pytest.raises(UnsafeProposalError):
        attempt(form(document_name="note.txt"))
    with pytest.raises(UnsafeProposalError):
        attempt(form(fields={"not_a_field": "x"}))
    with pytest.raises(UnsafeProposalError):
        attempt(form(fields={"Parent Signature": "Jane"}))
    with pytest.raises(UnsafeProposalError):
        attempt(form(recipient="attacker@evil.example"))
    edited = attempt(form())
    assert edited.version == 2


def test_edit_proposal_calendar_policy(store):
    from schoolsift.domain import CalendarProposal
    from schoolsift.errors import UnsafeProposalError

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    packet = make_packet()
    store.save_packet(h.id, packet)
    prop = packet.proposals[0]
    aware_start = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    aware_end = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)

    with pytest.raises(UnsafeProposalError):
        store.edit_proposal(
            h.id,
            prop.id,
            expected_version=1,
            payload=CalendarProposal(
                kind="calendar",
                title="Event",
                starts_at=datetime(2026, 9, 18, 15, 0),
                ends_at=aware_end,
            ),
        )
    with pytest.raises(UnsafeProposalError):
        store.edit_proposal(
            h.id,
            prop.id,
            expected_version=1,
            payload=CalendarProposal(
                kind="calendar",
                title="Event",
                starts_at=aware_end,
                ends_at=aware_start,
            ),
        )
    edited = store.edit_proposal(
        h.id,
        prop.id,
        expected_version=1,
        payload=CalendarProposal(
            kind="calendar",
            title="Event",
            starts_at=aware_start,
            ends_at=aware_end,
        ),
    )
    assert edited.version == 2


def test_migration_v5_rejects_duplicate_source_packets(tmp_path):
    from schoolsift.errors import MigrationError
    from schoolsift.sqlite_store import MIGRATIONS

    path = tmp_path / "v4.sqlite"
    con = sqlite3.connect(path)
    for migrate in MIGRATIONS[:4]:
        migrate(con)
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 4)")
    con.execute(
        "INSERT INTO households (id, name, timezone, created_at)"
        " VALUES ('hh-1', 'Fam', 'UTC', '2026-01-01T00:00:00+00:00')"
    )
    for pid in ("p1", "p2"):
        con.execute(
            "INSERT INTO packets (id, household_id, source_message_id, data,"
            " updated_at) VALUES (?, 'hh-1', 'msg-1', '{}',"
            " '2026-01-01T00:00:00+00:00')",
            (pid,),
        )
    con.commit()
    con.close()

    with pytest.raises(MigrationError, match="duplicate packets"):
        SQLiteStore(path).initialize()
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 2
    finally:
        con.close()


def test_migration_v5_creates_unique_index(store):
    con = sqlite3.connect(store.path)
    try:
        indexes = {r[1] for r in con.execute("PRAGMA index_list(packets)")}
        assert "packets_household_source_unique" in indexes
    finally:
        con.close()


def test_migration_v7_rejects_duplicate_identity_claims(tmp_path):
    from schoolsift.errors import MigrationError
    from schoolsift.sqlite_store import MIGRATIONS

    path = tmp_path / "v6.sqlite"
    con = sqlite3.connect(path)
    for migrate in MIGRATIONS[:6]:
        migrate(con)
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 6)")
    for hid in ("hh-1", "hh-2"):
        con.execute(
            "INSERT INTO households (id, name, timezone, created_at)"
            " VALUES (?, 'Fam', 'UTC', '2026-01-01T00:00:00+00:00')",
            (hid,),
        )
    for cid, hid in (("c1", "hh-1"), ("c2", "hh-2")):
        con.execute(
            "INSERT INTO connections (id, household_id, provider,"
            " provider_subject, email, status, last_sync_at, created_at)"
            " VALUES (?, ?, 'gmail', 'sub-1', 'a@x.com', 'connected', NULL,"
            " '2026-01-01T00:00:00+00:00')",
            (cid, hid),
        )
    con.commit()
    con.close()

    with pytest.raises(MigrationError, match="multiple households"):
        SQLiteStore(path).initialize()
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 2
    finally:
        con.close()


def test_migration_v7_allows_legacy_empty_subjects(tmp_path):
    from schoolsift.sqlite_store import MIGRATIONS, SCHEMA_VERSION

    path = tmp_path / "v6empty.sqlite"
    con = sqlite3.connect(path)
    for migrate in MIGRATIONS[:6]:
        migrate(con)
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 6)")
    for hid in ("hh-1", "hh-2"):
        con.execute(
            "INSERT INTO households (id, name, timezone, created_at)"
            " VALUES (?, 'Fam', 'UTC', '2026-01-01T00:00:00+00:00')",
            (hid,),
        )
        con.execute(
            "INSERT INTO connections (id, household_id, provider,"
            " provider_subject, email, status, last_sync_at, created_at)"
            " VALUES (?, ?, 'gmail', '', 'a@x.com', 'connected', NULL,"
            " '2026-01-01T00:00:00+00:00')",
            (f"c-{hid}", hid),
        )
    con.commit()
    con.close()

    SQLiteStore(path).initialize()
    con = sqlite3.connect(path)
    try:
        assert con.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone() == (SCHEMA_VERSION,)
    finally:
        con.close()


def test_identity_claim_conflict_across_households(store):
    h = store.create_household(name="A", timezone="UTC")
    insert_household(store, "hh-other")
    store.upsert_connection(
        h.id, provider="gmail", provider_subject="sub-shared", email="a@x.com"
    )
    with pytest.raises(ConflictError):
        store.upsert_connection(
            "hh-other",
            provider="gmail",
            provider_subject="sub-shared",
            email="b@y.com",
        )


def test_concurrent_identity_claim_single_winner(store):
    import threading

    h = store.create_household(name="A", timezone="UTC")
    insert_household(store, "hh-other")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def attempt(hid: str) -> None:
        barrier.wait(timeout=10)
        try:
            store.upsert_connection(
                hid,
                provider="gmail",
                provider_subject="sub-race",
                email="a@x.com",
            )
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    t1 = threading.Thread(target=attempt, args=(h.id,))
    t2 = threading.Thread(target=attempt, args=("hh-other",))
    t1.start()
    t2.start()
    t1.join(15)
    t2.join(15)
    assert sorted(results) == ["conflict", "ok"]


def _insert_raw_message(
    con: sqlite3.Connection,
    conn_id: str,
    pmid: str,
    name: str,
    email: str,
    received: str,
) -> None:
    con.execute(
        "INSERT INTO messages (id, household_id, connection_id,"
        " provider_message_id, thread_id, sender_name, sender_email, reply_to,"
        " subject, received_at, source_confirmed, status, body_ref,"
        " attachment_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"msg-{pmid}",
            "hh-1",
            conn_id,
            pmid,
            f"t-{pmid}",
            name,
            email,
            email,
            "Subject",
            received,
            0,
            "awaiting_source",
            None,
            "[]",
        ),
    )


def test_source_suggestion_stores_name_and_counts_messages(store):
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="a@x.com"
    )
    now = datetime.now(UTC)
    src = store.upsert_source_suggestion(
        h.id,
        conn.id,
        sender_email="Office@X.org",
        sender_name='" School Office "',
        seen_at=now,
    )
    assert src.sender_email == "office@x.org"
    assert src.sender_name == "School Office"
    assert src.message_count == 0

    con = sqlite3.connect(store.path)
    try:
        for pmid in ("p1", "p2"):
            _insert_raw_message(
                con,
                conn.id,
                pmid,
                "School Office",
                "office@x.org",
                now.isoformat(),
            )
        con.commit()
    finally:
        con.close()
    src = store.list_sources(h.id)[0]
    assert src.message_count == 2

    src = store.upsert_source_suggestion(
        h.id,
        conn.id,
        sender_email="office@x.org",
        sender_name="Main Office",
        seen_at=now,
    )
    assert src.sender_name == "Main Office"
    src = store.upsert_source_suggestion(
        h.id,
        conn.id,
        sender_email="office@x.org",
        sender_name="",
        seen_at=now,
    )
    assert src.sender_name == "Main Office"
    assert src.message_count == 2


def test_migration_v10_backfills_sender_names(tmp_path):
    from schoolsift.sqlite_store import MIGRATIONS

    path = tmp_path / "v9.sqlite"
    con = sqlite3.connect(path)
    for migrate in MIGRATIONS[:9]:
        migrate(con)
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 9)")
    con.execute(
        "INSERT INTO households (id, name, timezone, created_at)"
        " VALUES ('hh-1', 'Fam', 'UTC', '2026-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO connections (id, household_id, provider,"
        " provider_subject, email, status, last_sync_at, created_at)"
        " VALUES ('conn-1', 'hh-1', 'gmail', 'sub-1', 'a@x.com', 'connected',"
        " NULL, '2026-01-01T00:00:00+00:00')"
    )
    for sid, email in (("src-1", "office@x.org"), ("src-2", "quiet@y.org")):
        con.execute(
            "INSERT INTO school_sources (id, household_id, connection_id,"
            " sender_email, sender_domain, status, first_seen_at, last_seen_at)"
            " VALUES (?, 'hh-1', 'conn-1', ?, ?, 'suggested',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (sid, email, email.split("@")[1]),
        )
    _insert_raw_message(
        con,
        "conn-1",
        "old",
        "Old Name",
        "OFFICE@X.ORG",
        "2026-01-02T00:00:00+00:00",
    )
    _insert_raw_message(
        con,
        "conn-1",
        "new",
        "Maple Grove Office",
        "office@x.org",
        "2026-01-03T00:00:00+00:00",
    )
    con.commit()
    con.close()

    store = SQLiteStore(path)
    store.initialize()
    srcs = {s.sender_email: s for s in store.list_sources("hh-1")}
    assert srcs["office@x.org"].sender_name == "Maple Grove Office"
    assert srcs["office@x.org"].message_count == 2
    assert srcs["quiet@y.org"].sender_name == ""
    assert srcs["quiet@y.org"].message_count == 0


def test_packet_attachments_round_trip(store):
    from schoolsift.domain import PacketAttachment

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    packet = make_packet()
    packet.attachments = [
        PacketAttachment(name="form.pdf", mime="application/pdf", cited=True),
        PacketAttachment(name="note.txt", mime="text/plain", cited=False),
    ]
    store.save_packet(h.id, packet)
    loaded = store.get_packet(h.id, "packet-1")
    assert [(a.name, a.mime, a.cited) for a in loaded.attachments] == [
        ("form.pdf", "application/pdf", True),
        ("note.txt", "text/plain", False),
    ]


def test_packet_row_without_attachments_field_loads_empty(store):
    import json

    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id)
    data = make_packet().model_dump(mode="json")
    data.pop("attachments")
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO packets (id, household_id, source_message_id, data,"
            " updated_at) VALUES ('packet-1', ?, 'msg-1', ?, ?)",
            (h.id, json.dumps(data), datetime.now(UTC).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    loaded = store.get_packet(h.id, "packet-1")
    assert loaded.attachments == []
    assert loaded.proposals[0].id == "packet-1-prop-1"


def test_reset_processed_message_discards_packet(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id, status="processed")
    store.save_packet(h.id, make_packet())

    updated = store.reset_processed_message(h.id, "msg-1")
    assert updated.status == "awaiting_agent"
    assert updated.manual_review_reason is None
    assert store.get_packet_for_message(h.id, "msg-1") is None


def test_reset_processed_message_rejects_unsafe_states(store):
    h = store.create_household(name="A", timezone="UTC")
    seed_message(store, h.id, status="awaiting_agent")
    with pytest.raises(ConflictError):
        store.reset_processed_message(h.id, "msg-1")

    con = sqlite3.connect(store.path)
    try:
        con.execute("UPDATE messages SET status = 'processed' WHERE id = 'msg-1'")
        con.commit()
    finally:
        con.close()
    with pytest.raises(ConflictError):
        store.reset_processed_message(h.id, "msg-1")

    store.save_packet(h.id, make_packet())
    packet = store.get_packet(h.id, "packet-1")
    head = packet.proposals[0]
    store.approve_proposal(
        h.id, head.id, version=head.version, payload_hash=head.payload_hash
    )
    with pytest.raises(ConflictError):
        store.reset_processed_message(h.id, "msg-1")
    assert store.get_packet(h.id, "packet-1") is not None

    with pytest.raises(NotFoundError):
        store.reset_processed_message(h.id, "missing")
    with pytest.raises(NotFoundError):
        store.reset_processed_message("hh-other", "msg-1")
