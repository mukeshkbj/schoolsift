from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from schoolsift.content import LocalEncryptedContentStore
from schoolsift.credentials import OAuthCredentials
from schoolsift.domain import (
    ActionPacket,
    CalendarProposal,
    EscalationProposal,
    PdfFormProposal,
    ProposalVersion,
    ReplyProposal,
    payload_digest,
)
from schoolsift.errors import (
    DeliveryUncertainError,
    NotApprovableError,
    NotFoundError,
    ProviderAuthError,
    ProviderError,
)
from schoolsift.execution import (
    MemoryExecutionQueue,
    dispatch_pending_executions,
    execute_command,
)
from schoolsift.models import ExecutionCommand
from schoolsift.providers import OperationResult
from schoolsift.sqlite_store import SQLiteStore


def make_packet(
    pid: str = "packet-1",
    mid: str = "msg-1",
    payload=None,
) -> ActionPacket:
    payload = payload or ReplyProposal(
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
            ProposalVersion(
                id=f"{pid}-prop-1",
                version=1,
                status="proposed",
                payload=payload,
                payload_hash=payload_digest(payload),
            )
        ],
    )


def seed_message(store: SQLiteStore, household_id: str) -> None:
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
            " provider_message_id, thread_id, sender_name, sender_email,"
            " reply_to, subject, received_at, source_confirmed, status,"
            " body_ref, attachment_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                1,
                "awaiting_agent",
                "ref-body",
                '["doc-1"]',
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
                '["student_name"]',
            ),
        )
        con.commit()
    finally:
        con.close()


def creds() -> OAuthCredentials:
    return OAuthCredentials(
        access_token=SecretStr("at"),
        refresh_token=SecretStr("rt"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("openid",),
    )


class InMemoryVault:
    def __init__(self) -> None:
        self.data: dict[str, OAuthCredentials] = {}

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        self.data[connection_id] = credentials

    def get(self, connection_id: str) -> OAuthCredentials:
        return self.data[connection_id]

    def delete(self, connection_id: str) -> None:
        self.data.pop(connection_id, None)


class FakeProvider:
    def __init__(self) -> None:
        self.sent: list = []
        self.events: list = []
        self.send_error: Exception | None = None
        self.event_error: Exception | None = None

    def send_email(self, credentials, command, *, checkpoint=None):
        if checkpoint is not None:
            checkpoint("draft-1")
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(command)
        return OperationResult(operation_id="op-send-1")

    def create_calendar_event(self, credentials, command):
        if self.event_error is not None:
            raise self.event_error
        self.events.append(command)
        return OperationResult(operation_id="op-event-1")

    def refresh(self, credentials):
        return credentials


class FakeQueue:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[ExecutionCommand, str]] = []
        self.fail = fail

    def enqueue(self, command: ExecutionCommand, *, dedupe_key: str) -> bool:
        if self.fail:
            raise RuntimeError("queue down")
        self.calls.append((command, dedupe_key))
        return True


def form_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("student_name"),
            NameObject("/V"): TextStringObject(""),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(20)]
            ),
            NameObject("/P"): page.indirect_reference,
        }
    )
    field_ref = writer._add_object(field)
    page[NameObject("/Annots")] = ArrayObject([field_ref])
    font_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    font_dict_ref = writer._add_object(
        DictionaryObject({NameObject("/Helv"): font_ref})
    )
    dr_ref = writer._add_object(DictionaryObject({NameObject("/Font"): font_dict_ref}))
    acro_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Fields"): ArrayObject([field_ref]),
                NameObject("/DR"): dr_ref,
            }
        )
    )
    writer._root_object[NameObject("/AcroForm")] = acro_ref
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture()
def env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    seed_message(store, h.id)
    vault = InMemoryVault()
    vault.put("conn-1", creds())
    content = LocalEncryptedContentStore(
        tmp_path / "content", key=Fernet.generate_key()
    )
    provider = FakeProvider()
    return store, h, vault, content, provider


def approve(store, h, packet, prop_index=0):
    prop = packet.proposals[prop_index]
    return store.approve_proposal(
        h.id, prop.id, version=prop.version, payload_hash=prop.payload_hash
    )


class TestApprovalCreatesExecution:
    def test_approve_creates_execution_and_outbox(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        approved, execution = approve(store, h, packet)
        assert approved.status == "approved"
        assert execution.household_id == h.id
        assert execution.proposal_id == "packet-1-prop-1"
        assert execution.proposal_version == 1
        assert execution.connection_id == "conn-1"
        assert execution.status == "pending_dispatch"
        assert execution.attempts == 0
        assert execution.idempotency_key
        con = sqlite3.connect(store.path)
        try:
            rows = con.execute(
                "SELECT status FROM execution_outbox WHERE execution_id = ?",
                (execution.id,),
            ).fetchall()
            assert rows == [("pending",)]
        finally:
            con.close()

    def test_repeated_approve_returns_same_execution(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, first = approve(store, h, packet)
        prop = packet.proposals[0]
        _, second = store.approve_proposal(
            h.id, prop.id, version=1, payload_hash=prop.payload_hash
        )
        assert second.id == first.id
        assert len(store.list_executions(h.id)) == 1

    def test_reject_creates_no_execution(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        prop = packet.proposals[0]
        store.reject_proposal(h.id, prop.id, version=1, payload_hash=prop.payload_hash)
        assert store.list_executions(h.id) == []

    def test_escalation_never_executes(self, env):
        store, h, *_ = env
        esc = EscalationProposal(kind="escalation", reason="payment", detail="x")
        packet = make_packet("packet-e", payload=esc)
        store.save_packet(h.id, packet)
        with pytest.raises(NotApprovableError):
            approve(store, h, packet)
        assert store.list_executions(h.id) == []

    def test_executions_household_scoped(self, env):
        store, h, *_ = env
        other = store.create_household(name="B", timezone="UTC")
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        assert store.list_executions(other.id) == []
        with pytest.raises(NotFoundError):
            store.get_execution(other.id, execution.id)

    def test_output_ref_not_serialized(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        dumped = execution.model_dump()
        assert "output_ref" not in dumped
        assert "output_ref" not in execution.model_dump_json()

    def test_approval_writes_audit(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        approve(store, h, packet)
        con = sqlite3.connect(store.path)
        try:
            rows = con.execute(
                "SELECT kind, entity_id FROM audit_events"
                " WHERE household_id = ? ORDER BY created_at",
                (h.id,),
            ).fetchall()
        finally:
            con.close()
        kinds = [r[0] for r in rows]
        assert "proposal_approved" in kinds
        assert "execution_created" in kinds


class TestClaimAndSettle:
    def _execution(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        return approve(store, h, packet)[1]

    def test_claim_transitions_to_executing(self, env):
        store, h, *_ = env
        execution = self._execution(env)
        claimed = store.claim_execution(h.id, execution.id, datetime.now(UTC), 300)
        assert claimed is not None
        assert claimed.status == "executing"
        assert claimed.attempts == 1
        again = store.claim_execution(h.id, execution.id, datetime.now(UTC), 300)
        assert again is None

    def test_expired_lease_reclaimable(self, env):
        store, h, *_ = env
        execution = self._execution(env)
        now = datetime.now(UTC)
        store.claim_execution(h.id, execution.id, now, 1)
        reclaimed = store.claim_execution(
            h.id, execution.id, now + timedelta(seconds=5), 300
        )
        assert reclaimed is not None
        assert reclaimed.attempts == 2

    def test_settle_requires_current_attempt(self, env):
        store, h, *_ = env
        execution = self._execution(env)
        claimed = store.claim_execution(h.id, execution.id, datetime.now(UTC), 300)
        assert claimed is not None
        stale = store.settle_execution(
            h.id,
            execution.id,
            attempts=claimed.attempts + 9,
            status="completed",
        )
        assert stale is False
        ok = store.settle_execution(
            h.id,
            execution.id,
            attempts=claimed.attempts,
            status="completed",
            provider_operation_id="op-1",
        )
        assert ok is True
        done = store.get_execution(h.id, execution.id)
        assert done.status == "completed"
        assert done.provider_operation_id == "op-1"

    def test_settle_audits_terminal_result(self, env):
        store, h, *_ = env
        execution = self._execution(env)
        claimed = store.claim_execution(h.id, execution.id, datetime.now(UTC), 300)
        store.settle_execution(
            h.id,
            execution.id,
            attempts=claimed.attempts,
            status="failed",
            safe_error="boom",
        )
        con = sqlite3.connect(store.path)
        try:
            rows = con.execute(
                "SELECT kind FROM audit_events WHERE entity_id = ?",
                (execution.id,),
            ).fetchall()
        finally:
            con.close()
        assert ("execution_failed",) in rows


class TestDispatch:
    def test_dispatch_sends_and_marks(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        queue = FakeQueue()
        dispatched = dispatch_pending_executions(store, queue)
        assert dispatched == 1
        command, dedupe = queue.calls[0]
        assert command.execution_id == execution.id
        assert command.household_id == h.id
        assert dedupe == execution.idempotency_key
        after = store.get_execution(h.id, execution.id)
        assert after.status == "queued"
        con = sqlite3.connect(store.path)
        try:
            row = con.execute(
                "SELECT status FROM execution_outbox WHERE execution_id = ?",
                (execution.id,),
            ).fetchone()
            assert row == ("dispatched",)
        finally:
            con.close()
        assert dispatch_pending_executions(store, queue) == 0

    def test_queue_failure_leaves_pending(self, env):
        store, h, *_ = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        dispatched = dispatch_pending_executions(store, FakeQueue(fail=True))
        assert dispatched == 0
        after = store.get_execution(h.id, execution.id)
        assert after.status == "pending_dispatch"
        con = sqlite3.connect(store.path)
        try:
            row = con.execute(
                "SELECT status, attempts FROM execution_outbox WHERE execution_id = ?",
                (execution.id,),
            ).fetchone()
            assert row == ("pending", 1)
        finally:
            con.close()

    def test_memory_queue_dedupes(self):
        queue = MemoryExecutionQueue()
        cmd = ExecutionCommand(execution_id="e1", household_id="h1")
        assert queue.enqueue(cmd, dedupe_key="k") is True
        assert queue.enqueue(cmd, dedupe_key="k") is False
        assert len(queue.pending) == 1


class TestExecuteCommand:
    def _command(self, execution) -> ExecutionCommand:
        return ExecutionCommand(
            execution_id=execution.id, household_id=execution.household_id
        )

    def test_reply_sends_and_completes(self, env):
        store, h, vault, content, provider = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "completed"
        assert result.provider_operation_id == "op-send-1"
        assert len(provider.sent) == 1
        command = provider.sent[0]
        assert command.recipient == "school@example.org"
        assert command.idempotency_key == execution.idempotency_key

    def test_calendar_creates_event(self, env):
        store, h, vault, content, provider = env
        cal = CalendarProposal(
            kind="calendar",
            title="Trip",
            starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
            ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
        )
        packet = make_packet("packet-c", payload=cal)
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "completed"
        assert result.provider_operation_id == "op-event-1"
        assert provider.events[0].idempotency_key == execution.idempotency_key

    def test_pdf_form_fills_and_emails(self, env):
        store, h, vault, content, provider = env
        ref = content.put(h.id, form_pdf())
        con = sqlite3.connect(store.path)
        try:
            con.execute(
                "UPDATE documents SET content_ref = ? WHERE id = 'doc-1'",
                (ref,),
            )
            con.commit()
        finally:
            con.close()
        pdf = PdfFormProposal(
            kind="pdf_form",
            recipient="school@example.org",
            subject="Completed form",
            body="Attached is the completed form.",
            document_name="form.pdf",
            fields={"student_name": "Kid"},
        )
        packet = make_packet("packet-p", payload=pdf)
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "completed"
        command = provider.sent[0]
        assert command.attachment_bytes is not None
        assert command.attachment_name == "form.pdf"
        assert command.attachment_mime == "application/pdf"
        from pypdf import PdfReader

        filled = PdfReader(BytesIO(command.attachment_bytes))
        fields = filled.get_fields()
        assert fields["student_name"]["/V"] == "Kid"
        stored = store.get_execution(h.id, execution.id)
        assert stored.output_ref is not None
        assert content.get(h.id, stored.output_ref) == command.attachment_bytes

    def test_pdf_failure_prevents_send(self, env):
        store, h, vault, content, provider = env
        content.put(h.id, b"not a pdf")
        ref = content.put(h.id, b"not a pdf")
        con = sqlite3.connect(store.path)
        try:
            con.execute(
                "UPDATE documents SET content_ref = ? WHERE id = 'doc-1'",
                (ref,),
            )
            con.commit()
        finally:
            con.close()
        pdf = PdfFormProposal(
            kind="pdf_form",
            recipient="school@example.org",
            subject="Completed form",
            body="Attached.",
            document_name="form.pdf",
            fields={"student_name": "Kid"},
        )
        packet = make_packet("packet-p", payload=pdf)
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "failed"
        assert provider.sent == []
        assert result.safe_error is not None

    def test_uncertain_delivery_is_terminal(self, env):
        store, h, vault, content, provider = env
        provider.send_error = DeliveryUncertainError(
            "maybe sent", operation_id="op-ambiguous"
        )
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "delivery_uncertain"
        assert result.provider_operation_id == "op-ambiguous"
        provider.send_error = None
        again = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert again is None
        assert provider.sent == []

    def test_transient_failure_requeues(self, env):
        store, h, vault, content, provider = env
        provider.send_error = ProviderError("temporary")
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "queued"
        assert result.attempts == 1
        provider.send_error = None
        again = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert again is not None
        assert again.status == "completed"
        assert len(provider.sent) == 1

    def test_attempts_exhausted_fails(self, env):
        store, h, vault, content, provider = env
        provider.send_error = ProviderError("always fails")
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        for _ in range(5):
            result = execute_command(
                self._command(execution),
                store=store,
                vault=vault,
                content=content,
                providers={"gmail": provider},
            )
        assert result is not None
        assert result.status == "failed"
        assert result.attempts == 5
        again = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert again is None

    def test_concurrent_execution_single_claim(self, env):
        import threading

        store, h, vault, content, provider = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def run() -> None:
            barrier.wait(timeout=10)
            outcomes.append(
                execute_command(
                    self._command(execution),
                    store=store,
                    vault=vault,
                    content=content,
                    providers={"gmail": provider},
                )
            )

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join(15)
        t2.join(15)
        completed = [o for o in outcomes if o is not None]
        assert len(completed) == 1
        assert completed[0].status == "completed"
        assert len(provider.sent) == 1

    def test_auth_failure_marks_reauthorization(self, env):
        store, h, vault, content, provider = env
        provider.send_error = ProviderAuthError("expired")
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "failed"
        conn = store.get_connection(h.id, "conn-1")
        assert conn.status == "reauthorization_required"

    def test_tampered_payload_fails_closed(self, env):
        store, h, vault, content, provider = env
        packet = make_packet()
        store.save_packet(h.id, packet)
        _, execution = approve(store, h, packet)
        con = sqlite3.connect(store.path)
        try:
            row = con.execute(
                "SELECT data FROM packets WHERE id = 'packet-1'"
            ).fetchone()
            import json as _json

            data = _json.loads(row[0])
            data["proposals"][0]["payload"]["body"] = "tampered"
            con.execute(
                "UPDATE packets SET data = ? WHERE id = 'packet-1'",
                (_json.dumps(data),),
            )
            con.commit()
        finally:
            con.close()
        result = execute_command(
            self._command(execution),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is not None
        assert result.status == "failed"
        assert provider.sent == []

    def test_no_claim_returns_none(self, env):
        store, h, vault, content, provider = env
        result = execute_command(
            ExecutionCommand(execution_id="exec-nope", household_id=h.id),
            store=store,
            vault=vault,
            content=content,
            providers={"gmail": provider},
        )
        assert result is None
