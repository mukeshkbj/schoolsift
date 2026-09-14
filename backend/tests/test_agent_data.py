from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from schoolsift.agent_data import StoredAgentDataSource
from schoolsift.content import LocalEncryptedContentStore
from schoolsift.models import AttachmentContent, FetchedMessage, MessageHeader
from schoolsift.sqlite_store import SQLiteStore


@pytest.fixture()
def env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    store.add_child(h.id, name="Kid", school="School", grade="2")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s1", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    content = LocalEncryptedContentStore(
        tmp_path / "content", key=Fernet.generate_key()
    )
    return store, h, conn, content


def _import(env) -> str:
    store, h, conn, content = env
    header = MessageHeader(
        provider_message_id="pm-1",
        thread_id="thread-1",
        sender_name="Office",
        sender_email="office@school.org",
        reply_to="office@school.org",
        subject="Field trip",
        received_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    fetched = FetchedMessage(
        **header.model_dump(),
        body_text="Trip money due Friday",
        attachments=[
            AttachmentContent(
                provider_attachment_id="pm-1:0",
                name="note.txt",
                mime="text/plain",
                content=b"Bring a water bottle",
            )
        ],
    )
    body_ref = content.put(h.id, fetched.body_text.encode())
    att_ref = content.put(h.id, b"Bring a water bottle")
    record = store.save_fetched_message(
        h.id,
        conn.id,
        fetched,
        body_ref=body_ref,
        attachments=[("note.txt", "text/plain", att_ref, [])],
    )
    return record.id


def test_get_message_resolves_encrypted_body(env):
    store, h, _, content = env
    message_id = _import(env)
    ds = StoredAgentDataSource(store, content)
    msg = ds.get_message(h.id, message_id)
    assert msg.body == "Trip money due Friday"
    assert msg.attachment_ids


def test_get_document_resolves_and_reads(env):
    store, h, _, content = env
    message_id = _import(env)
    ds = StoredAgentDataSource(store, content)
    msg = ds.get_message(h.id, message_id)
    doc = ds.get_document(h.id, msg.attachment_ids[0])
    assert doc.text == "Bring a water bottle"
    assert doc.source_bytes == b"Bring a water bottle"


def test_cross_household_reads_denied(env):
    store, _, _, content = env
    message_id = _import(env)
    ds = StoredAgentDataSource(store, content)
    with pytest.raises(PermissionError):
        ds.get_message("hh-other", message_id)
    with pytest.raises(PermissionError):
        ds.get_document("hh-other", "doc-1")
    with pytest.raises(PermissionError):
        ds.get_household_context("hh-other")
    with pytest.raises(PermissionError):
        ds.find_related_actions("hh-other", "thread-1")
    with pytest.raises(PermissionError):
        ds.find_calendar_conflicts(
            "hh-other",
            datetime(2026, 9, 10, tzinfo=UTC),
            datetime(2026, 9, 11, tzinfo=UTC),
        )


def test_household_context_lists_children_and_connections(env):
    store, h, _, content = env
    ds = StoredAgentDataSource(store, content)
    ctx = ds.get_household_context(h.id)
    assert ctx.children == ["Kid"]
    assert ctx.connections == ["me@x.com"]


def test_related_actions_and_calendar(env):
    store, h, _, content = env
    _import(env)
    ds = StoredAgentDataSource(store, content)
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO calendar_records (id, household_id, title, starts_at,"
            " ends_at) VALUES ('evt-1', ?, 'Dentist', ?, ?)",
            (h.id, "2026-09-25T15:00:00+00:00", "2026-09-25T16:00:00+00:00"),
        )
        con.commit()
    finally:
        con.close()
    conflicts = ds.find_calendar_conflicts(
        h.id,
        datetime(2026, 9, 25, 15, 30, tzinfo=UTC),
        datetime(2026, 9, 25, 16, 30, tzinfo=UTC),
    )
    assert [c.id for c in conflicts] == ["evt-1"]
    assert ds.find_related_actions(h.id, "thread-1") == []


def test_unknown_message_raises_not_found(env):
    from schoolsift.errors import NotFoundError

    store, h, _, content = env
    ds = StoredAgentDataSource(store, content)
    with pytest.raises(NotFoundError):
        ds.get_message(h.id, "msg-missing")
    with pytest.raises(NotFoundError):
        ds.get_document(h.id, "doc-missing")
