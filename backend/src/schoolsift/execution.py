"""Approval-triggered execution: idempotent outbox dispatch and bounded
provider execution. Retries are limited to pre-side-effect failures;
ambiguous provider outcomes settle terminally as delivery_uncertain.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from .content import ContentStore
from .credentials import CredentialVault, OAuthCredentials
from .documents import fill_pdf_form
from .domain import (
    CalendarProposal,
    PdfFormProposal,
    ReplyProposal,
    payload_digest,
)
from .errors import (
    ConflictError,
    DeliveryUncertainError,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfiguredError,
    SchoolSiftError,
    VaultError,
)
from .models import ExecutionCommand, ExecutionRecord
from .proposal_policy import validate_proposal_payload
from .providers import (
    CreateEventCommand,
    MailProvider,
    Provider,
    SendEmailCommand,
)
from .store import SchoolSiftStore

LEASE_SECONDS = 300
MAX_ATTEMPTS = 5
MAX_DISPATCH = 50

_GENERIC_FAILURE = "The approved action could not be completed."


class ExecutionQueue(Protocol):
    def enqueue(self, command: ExecutionCommand, *, dedupe_key: str) -> bool: ...


class MemoryExecutionQueue:
    """In-process queue for local mode; the run endpoint executes directly."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.pending: list[ExecutionCommand] = []

    def enqueue(self, command: ExecutionCommand, *, dedupe_key: str) -> bool:
        if dedupe_key in self._seen:
            return False
        self._seen.add(dedupe_key)
        self.pending.append(command)
        return True


def dispatch_pending_executions(
    store: SchoolSiftStore, queue: ExecutionQueue, *, limit: int = MAX_DISPATCH
) -> int:
    dispatched = 0
    for record in store.list_dispatchable_executions(limit):
        try:
            queue.enqueue(
                ExecutionCommand(
                    execution_id=record.id, household_id=record.household_id
                ),
                dedupe_key=record.idempotency_key,
            )
        except Exception:
            store.record_outbox_failure(record.id)
            continue
        store.mark_execution_dispatched(record.household_id, record.id)
        dispatched += 1
    return dispatched


def _load_credentials(
    connection_id: str,
    *,
    adapter: MailProvider,
    vault: CredentialVault,
) -> OAuthCredentials:
    credentials = vault.get(connection_id)
    if credentials.expires_at is not None and credentials.expires_at <= datetime.now(
        UTC
    ) + timedelta(minutes=5):
        credentials = adapter.refresh(credentials)
        vault.put(connection_id, credentials)
    return credentials


def _pdf_attachment(
    record: ExecutionRecord,
    payload: PdfFormProposal,
    *,
    store: SchoolSiftStore,
    content: ContentStore,
    message_id: str,
) -> tuple[bytes, str, str]:
    docs = store.list_document_records(record.household_id, message_id)
    matches = [d for d in docs if d.name == payload.document_name]
    if len(matches) != 1:
        raise ConflictError("The selected attachment could not be matched.")
    doc = matches[0]
    if record.output_ref is not None:
        return (
            content.get(record.household_id, record.output_ref),
            doc.name,
            record.output_ref,
        )
    filled = fill_pdf_form(
        content.get(record.household_id, doc.content_ref),
        {str(k): str(v) for k, v in payload.fields.items()},
    )
    ref = content.put(record.household_id, filled)
    store.update_execution_output(record.household_id, record.id, ref)
    return filled, doc.name, ref


def _perform(
    record: ExecutionRecord,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
) -> tuple[str | None, str | None]:
    packet = store.get_packet_by_proposal(record.household_id, record.proposal_id)
    version = next(
        (
            v
            for v in packet.proposals
            if v.id == record.proposal_id and v.version == record.proposal_version
        ),
        None,
    )
    if (
        version is None
        or version.status != "approved"
        or version.payload_hash != payload_digest(version.payload)
    ):
        raise ConflictError("The approved proposal payload could not be verified.")
    message = store.get_message_record(record.household_id, packet.source_message_id)
    docs = store.list_document_records(record.household_id, message.id)
    validate_proposal_payload(message, docs, version.payload)
    conn = store.get_connection(record.household_id, record.connection_id)
    if conn.status != "connected":
        raise ConflictError(
            "This account is not connected; reconnect it before acting."
        )
    adapter = providers.get(conn.provider)
    if adapter is None:
        raise ProviderNotConfiguredError(
            f"{conn.provider} OAuth is not configured on this API."
        )
    credentials = _load_credentials(record.connection_id, adapter=adapter, vault=vault)
    payload = version.payload
    if isinstance(payload, CalendarProposal):
        result = adapter.create_calendar_event(
            credentials,
            CreateEventCommand(
                idempotency_key=record.idempotency_key,
                title=payload.title,
                starts_at=payload.starts_at,
                ends_at=payload.ends_at,
            ),
        )
        return result.operation_id, None
    if isinstance(payload, (ReplyProposal, PdfFormProposal)):
        pdf_bytes: bytes | None = None
        doc_name: str | None = None
        output_ref: str | None = None
        if isinstance(payload, PdfFormProposal):
            pdf_bytes, doc_name, output_ref = _pdf_attachment(
                record,
                payload,
                store=store,
                content=content,
                message_id=message.id,
            )
        result = adapter.send_email(
            credentials,
            SendEmailCommand(
                idempotency_key=record.idempotency_key,
                recipient=payload.recipient,
                subject=payload.subject,
                body=payload.body,
                attachment_bytes=pdf_bytes,
                attachment_name=doc_name,
                attachment_mime="application/pdf" if pdf_bytes else None,
                draft_id=record.provider_operation_id,
            ),
            checkpoint=lambda op: store.update_execution_checkpoint(
                record.household_id, record.id, op
            ),
        )
        return result.operation_id, output_ref
    raise ConflictError("This proposal kind cannot be executed.")


def _settle(
    store: SchoolSiftStore,
    record: ExecutionRecord,
    status: Literal["queued", "completed", "failed", "delivery_uncertain"],
    *,
    provider_operation_id: object = ...,
    output_ref: object = ...,
    safe_error: object = ...,
) -> None:
    kwargs: dict[str, object] = {}
    if provider_operation_id is not ...:
        kwargs["provider_operation_id"] = provider_operation_id
    if output_ref is not ...:
        kwargs["output_ref"] = output_ref
    if safe_error is not ...:
        kwargs["safe_error"] = safe_error
    store.settle_execution(
        record.household_id,
        record.id,
        attempts=record.attempts,
        status=status,
        **kwargs,
    )


def _reauth(store: SchoolSiftStore, record: ExecutionRecord) -> None:
    with contextlib.suppress(SchoolSiftError):
        store.set_connection_status(
            record.household_id, record.connection_id, "reauthorization_required"
        )


def execute_command(
    command: ExecutionCommand,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
    now: datetime | None = None,
) -> ExecutionRecord | None:
    record = store.claim_execution(
        command.household_id,
        command.execution_id,
        now or datetime.now(UTC),
        LEASE_SECONDS,
    )
    if record is None:
        return None
    try:
        operation_id, output_ref = _perform(
            record, store=store, vault=vault, content=content, providers=providers
        )
        _settle(
            store,
            record,
            "completed",
            provider_operation_id=operation_id,
            output_ref=output_ref if output_ref is not None else ...,
            safe_error=None,
        )
    except DeliveryUncertainError as e:
        _settle(
            store,
            record,
            "delivery_uncertain",
            provider_operation_id=e.operation_id or record.provider_operation_id,
            safe_error=e.message,
        )
    except (ProviderAuthError, VaultError) as e:
        _reauth(store, record)
        _settle(store, record, "failed", safe_error=e.message)
    except ProviderError as e:
        if record.attempts >= MAX_ATTEMPTS:
            _settle(store, record, "failed", safe_error=e.message)
        else:
            _settle(store, record, "queued", safe_error=e.message)
    except SchoolSiftError as e:
        _settle(store, record, "failed", safe_error=e.message)
    except Exception:
        status: Literal["failed", "queued"] = (
            "failed" if record.attempts >= MAX_ATTEMPTS else "queued"
        )
        _settle(store, record, status, safe_error=_GENERIC_FAILURE)
    return store.get_execution(record.household_id, record.id)
