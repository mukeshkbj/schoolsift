from __future__ import annotations

from datetime import UTC, datetime

import pytest

from schoolsift.domain import (
    ActionPacketDraft,
    CalendarProposal,
    EvidenceSpan,
    PdfFormProposal,
    ReplyProposal,
)
from schoolsift.errors import (
    AgentError,
    ConflictError,
    NotFoundError,
    UnsafeAgentOutputError,
)
from schoolsift.models import AttachmentContent, FetchedMessage, MessageHeader
from schoolsift.processing import process_message
from schoolsift.sqlite_store import SQLiteStore


class FakeAnalyzer:
    def __init__(
        self,
        draft: ActionPacketDraft | None = None,
        *,
        fail: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def analyze(self, household_id: str, message_id: str) -> ActionPacketDraft:
        self.calls.append((household_id, message_id))
        if self.fail:
            raise self.fail
        assert self.draft is not None
        return self.draft


def draft_for(message_id: str, proposals=None) -> ActionPacketDraft:
    return ActionPacketDraft(
        source_message_id=message_id,
        school_source_id=None,
        summary="Field trip permission needed.",
        child="Kid",
        deadline=datetime(2026, 9, 18, tzinfo=UTC),
        urgency="soon",
        evidence=[EvidenceSpan(source="body", quote="due Friday")],
        proposals=proposals
        if proposals is not None
        else [
            ReplyProposal(
                kind="reply",
                recipient="office@school.org",
                subject="Re: Trip",
                body="Yes, Kid will attend.",
            )
        ],
    )


@pytest.fixture()
def env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s1", email="me@x.com"
    )
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
                name="form.pdf",
                mime="application/pdf",
                content=b"pdf",
            )
        ],
    )
    record = store.save_fetched_message(
        h.id,
        conn.id,
        fetched,
        body_ref="ref-body",
        attachments=[
            (
                "form.pdf",
                "application/pdf",
                "ref-att",
                ["student_name", "Parent Signature"],
            ),
            ("note.txt", "text/plain", "ref-txt", []),
        ],
    )
    return store, h, conn, record


def test_process_creates_packet_and_marks_processed(env):
    store, h, _, record = env
    analyzer = FakeAnalyzer(draft_for(record.id))
    packet = process_message(h.id, record.id, store=store, analyzer=analyzer)
    assert analyzer.calls == [(h.id, record.id)]
    assert packet.source_message_id == record.id
    assert packet.sender == "Office <office@school.org>"
    assert packet.proposals[0].version == 1
    assert packet.proposals[0].status == "proposed"
    assert packet.proposals[0].id.startswith("prop-")
    assert packet.id.startswith("pkt-")
    assert store.get_message_record(h.id, record.id).status == "processed"
    assert store.get_packet(h.id, packet.id).summary == "Field trip permission needed."


def test_repeat_returns_existing_packet(env):
    store, h, _, record = env
    analyzer = FakeAnalyzer(draft_for(record.id))
    first = process_message(h.id, record.id, store=store, analyzer=analyzer)
    second = process_message(
        h.id, record.id, store=store, analyzer=FakeAnalyzer(fail=RuntimeError("nope"))
    )
    assert second.id == first.id
    assert analyzer.calls == [(h.id, record.id)]


def test_wrong_status_rejected(env):
    store, h, conn, record = env
    store.set_message_status(h.id, conn.id, "pm-1", "awaiting_source")
    con = __import__("sqlite3").connect(store.path)
    try:
        con.execute(
            "UPDATE messages SET source_confirmed = 0 WHERE id = ?",
            (record.id,),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(ConflictError):
        process_message(
            h.id, record.id, store=store, analyzer=FakeAnalyzer(draft_for(record.id))
        )


def test_cross_household_denied(env):
    store, _, _, record = env
    with pytest.raises(NotFoundError):
        process_message(
            "hh-other",
            record.id,
            store=store,
            analyzer=FakeAnalyzer(draft_for(record.id)),
        )


def test_wrong_source_message_id_marks_failed(env):
    store, h, _, record = env
    bad = draft_for("msg-elsewhere")
    with pytest.raises(UnsafeAgentOutputError):
        process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    msg = store.get_message_record(h.id, record.id)
    assert msg.status == "failed"
    assert msg.manual_review_reason is not None
    assert store.list_packets(h.id) == []


def test_evidence_unknown_source_rejected(env):
    store, h, _, record = env
    bad = draft_for(record.id)
    bad.evidence = [EvidenceSpan(source="evil.pdf", quote="q")]
    with pytest.raises(UnsafeAgentOutputError):
        process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    assert store.get_message_record(h.id, record.id).status == "failed"


def test_evidence_body_aliases_normalized(env):
    store, h, _, record = env
    draft = draft_for(record.id)
    draft.evidence = [
        EvidenceSpan(source="Email body", quote="q1"),
        EvidenceSpan(source=record.sender_email.upper(), quote="q2"),
    ]
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(draft))
    assert [e.source for e in packet.evidence] == ["body", "body"]


def test_summary_tool_markup_stripped(env):
    store, h, _, record = env
    draft = draft_for(record.id)
    draft.summary = 'Trip on Friday.</summary>\n<parameter name="child">Maya'
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(draft))
    assert packet.summary == "Trip on Friday."


def test_evidence_document_name_loose_match_and_unknown_child(env):
    store, h, _, record = env
    docs = store.list_document_records(h.id, record.id)
    assert docs, "fixture message must carry an attachment"
    draft = draft_for(record.id)
    mangled = docs[0].name.replace(".", "-").upper()
    draft.evidence = [EvidenceSpan(source=mangled, quote="q")]
    draft.child = "<UNKNOWN>"
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(draft))
    assert packet.evidence[0].source == docs[0].name
    assert packet.child is None


def test_naive_deadline_localized_to_household_timezone(env):
    store, h, _, record = env
    draft = draft_for(record.id)
    draft.deadline = datetime(2026, 9, 18, 15, 0)
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(draft))
    assert packet.deadline is not None
    assert packet.deadline.tzinfo is not None
    assert packet.deadline.isoformat().startswith("2026-09-18T15:00:00")


def test_reply_recipient_guard(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            ReplyProposal(
                kind="reply",
                recipient="attacker@evil.example",
                subject="Re: Trip",
                body="send money",
            )
        ],
    )
    with pytest.raises(UnsafeAgentOutputError):
        process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    assert store.get_message_record(h.id, record.id).status == "failed"


def test_reply_recipient_accepts_reply_to(env):
    store, h, _, record = env
    ok = draft_for(
        record.id,
        proposals=[
            ReplyProposal(
                kind="reply",
                recipient="Office <office@school.org>",
                subject="Re: Trip",
                body="ok",
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(ok))
    assert packet.proposals[0].payload.recipient == "Office <office@school.org>"


def test_pdf_form_sensitive_field_rejected(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            PdfFormProposal(
                kind="pdf_form",
                recipient="office@school.org",
                subject="Completed form",
                body="Attached is the completed form.",
                document_name="form.pdf",
                fields={"Parent Signature": "Jane Doe"},
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    payload = packet.proposals[0].payload
    assert payload.kind == "escalation"
    assert payload.reason == "unsupported_document"
    assert "Signature and payment fields" in payload.detail


def test_pdf_form_document_name_loose_match(env):
    store, h, _, record = env
    draft = draft_for(
        record.id,
        proposals=[
            PdfFormProposal(
                kind="pdf_form",
                recipient="office@school.org",
                subject="Completed form",
                body="Attached is the completed form.",
                document_name="Form-pdf",
                fields={"student_name": "Maya"},
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(draft))
    assert packet.proposals[0].payload.document_name == "form.pdf"


def test_pdf_form_unknown_document_rejected(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            PdfFormProposal(
                kind="pdf_form",
                recipient="office@school.org",
                subject="Completed form",
                body="Attached is the completed form.",
                document_name="not-here.pdf",
                fields={"name": "Kid"},
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    payload = packet.proposals[0].payload
    assert payload.kind == "escalation"
    assert "could not be matched" in payload.detail


def test_pdf_form_safe_field_accepted(env):
    store, h, _, record = env
    ok = draft_for(
        record.id,
        proposals=[
            PdfFormProposal(
                kind="pdf_form",
                recipient="office@school.org",
                subject="Completed form",
                body="Attached is the completed form.",
                document_name="form.pdf",
                fields={"student_name": "Kid"},
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(ok))
    assert packet.proposals[0].payload.kind == "pdf_form"


def test_calendar_proposal_naive_times_localized(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            CalendarProposal(
                kind="calendar",
                title="Trip",
                starts_at=datetime(2026, 9, 18, 9),
                ends_at=datetime(2026, 9, 18, 15),
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    payload = packet.proposals[0].payload
    assert payload.kind == "calendar"
    assert payload.starts_at.tzinfo is not None and payload.ends_at.tzinfo is not None


def test_analyzer_crash_marks_failed_sanitized(env):
    store, h, _, record = env
    with pytest.raises(AgentError) as e:
        process_message(
            h.id,
            record.id,
            store=store,
            analyzer=FakeAnalyzer(fail=RuntimeError("model internals tok-xyz")),
        )
    assert "tok-xyz" not in e.value.message
    assert store.get_message_record(h.id, record.id).status == "failed"
    assert store.list_packets(h.id) == []


def test_information_only_with_proposals_rejected(env):
    store, h, _, record = env
    bad = draft_for(record.id)
    bad.information_only = True
    with pytest.raises(UnsafeAgentOutputError):
        process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    assert store.get_message_record(h.id, record.id).status == "failed"
    assert store.list_packets(h.id) == []


def test_pdf_form_unknown_field_rejected(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            PdfFormProposal(
                kind="pdf_form",
                recipient="office@school.org",
                subject="Completed form",
                body="Attached is the completed form.",
                document_name="form.pdf",
                fields={"not_a_field": "x"},
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    payload = packet.proposals[0].payload
    assert payload.kind == "escalation"
    assert "not part of the PDF form" in payload.detail


def test_pdf_form_non_pdf_document_rejected(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            PdfFormProposal(
                kind="pdf_form",
                recipient="office@school.org",
                subject="Completed form",
                body="Attached is the completed form.",
                document_name="note.txt",
                fields={"student_name": "Kid"},
            )
        ],
    )
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))
    payload = packet.proposals[0].payload
    assert payload.kind == "escalation"
    assert "not a PDF form" in payload.detail


def test_calendar_end_before_start_rejected(env):
    store, h, _, record = env
    bad = draft_for(
        record.id,
        proposals=[
            CalendarProposal(
                kind="calendar",
                title="Trip",
                starts_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 14, tzinfo=UTC),
            )
        ],
    )
    with pytest.raises(UnsafeAgentOutputError):
        process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(bad))


def test_concurrent_processing_persists_single_packet(env):
    import threading

    store, h, _, record = env
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def attempt() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                process_message(
                    h.id,
                    record.id,
                    store=store,
                    analyzer=FakeAnalyzer(draft_for(record.id)),
                )
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    assert len(results) == 2
    assert results[0].id == results[1].id
    assert len(store.list_packets(h.id)) == 1
    assert store.get_message_record(h.id, record.id).status == "processed"


def test_model_access_error_leaves_message_awaiting(env):
    from schoolsift.errors import ModelAccessError

    store, h, _, record = env
    with pytest.raises(ModelAccessError):
        process_message(
            h.id,
            record.id,
            store=store,
            analyzer=FakeAnalyzer(
                fail=ModelAccessError("Bedrock is busy; try again in a moment.")
            ),
        )
    after = store.get_message_record(h.id, record.id)
    assert after.status == "awaiting_agent"
    assert after.manual_review_reason is None


def test_packet_records_attachments_and_cited_flags(env):
    store, h, _, record = env
    draft = draft_for(record.id)
    draft.evidence.append(EvidenceSpan(source="form.pdf", quote="sign here"))
    packet = process_message(h.id, record.id, store=store, analyzer=FakeAnalyzer(draft))
    by_name = {a.name: a for a in packet.attachments}
    assert by_name["form.pdf"].mime == "application/pdf"
    assert by_name["form.pdf"].cited is True
    assert by_name["note.txt"].cited is False

    reloaded = store.get_packet(h.id, packet.id)
    assert [a.name for a in reloaded.attachments] == [
        a.name for a in packet.attachments
    ]


def test_packet_attachments_empty_without_documents(env):
    store, h, _, record = env
    con = __import__("sqlite3").connect(store.path)
    try:
        con.execute("DELETE FROM documents WHERE message_id = ?", (record.id,))
        con.commit()
    finally:
        con.close()
    packet = process_message(
        h.id,
        record.id,
        store=store,
        analyzer=FakeAnalyzer(draft_for(record.id)),
    )
    assert packet.attachments == []
