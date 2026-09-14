from __future__ import annotations

import re
import secrets
from datetime import datetime

from .agent import MessageAnalyzer
from .domain import (
    ActionPacket,
    ActionPacketDraft,
    EscalationProposal,
    PacketAttachment,
    PdfFormProposal,
    ProposalPayload,
    ProposalVersion,
    payload_digest,
)
from .errors import (
    AgentError,
    ConflictError,
    ModelAccessError,
    SchoolSiftError,
    UnsafeAgentOutputError,
    UnsafeProposalError,
)
from .models import DocumentRecord, MessageRecord
from .proposal_policy import validate_proposal_payload
from .store import SchoolSiftStore

_GENERIC_FAILURE = (
    "SchoolSift could not safely process this message; it needs manual review."
)


_TOOL_MARKUP = re.compile(r"</?(?:summary|parameter|invoke|function_calls)\b", re.I)

_UNKNOWN_TOKENS = {"", "unknown", "none", "na", "notspecified", "notprovided", "null"}


def _token(value: str) -> str:
    """Loose match key: models often drop dots, case, or punctuation in names."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


_BODY_ALIASES = {"body", "email body", "message body", "email", "message", "email text"}


def _aware(value: datetime) -> bool:
    return value.utcoffset() is not None


def _validate_draft(
    record: MessageRecord, docs: list[DocumentRecord], draft: ActionPacketDraft
) -> None:
    def unsafe(detail: str) -> UnsafeAgentOutputError:
        return UnsafeAgentOutputError(f"Unsafe agent output rejected: {detail}")

    if draft.source_message_id != record.id:
        raise unsafe("draft is not bound to the source message.")
    if markup := _TOOL_MARKUP.search(draft.summary):
        # Some models leak tool-call markup into free-text fields; keep the prose.
        draft.summary = draft.summary[: markup.start()].strip()
        if not draft.summary:
            raise unsafe("summary contained only tool markup.")
    if draft.information_only and draft.proposals:
        raise unsafe("draft is information-only but includes proposals.")
    known_sources = {"body"} | {d.name for d in docs}
    by_token = {_token(d.name): d.name for d in docs}
    for span in draft.evidence:
        label = span.source.strip().lower()
        if label in _BODY_ALIASES or label == record.sender_email.lower():
            span.source = "body"
        elif span.source not in known_sources:
            span.source = by_token.get(_token(span.source), span.source)
        if span.source not in known_sources:
            raise unsafe(f"evidence cites unknown source '{span.source}'.")
    if draft.child is not None and _token(draft.child) in _UNKNOWN_TOKENS:
        draft.child = None
    if draft.deadline is not None and not _aware(draft.deadline):
        raise unsafe("deadline is not timezone-aware.")
    proposals: list[ProposalPayload] = []
    for payload in draft.proposals:
        if (
            isinstance(payload, PdfFormProposal)
            and payload.document_name not in known_sources
        ):
            payload.document_name = by_token.get(
                _token(payload.document_name), payload.document_name
            )
        try:
            validate_proposal_payload(record, docs, payload)
        except UnsafeProposalError as e:
            if isinstance(payload, PdfFormProposal):
                # A form we cannot fill safely is still worth surfacing to a
                # person; the message itself is fine.
                proposals.append(
                    EscalationProposal(
                        kind="escalation",
                        reason="unsupported_document",
                        detail=f"'{payload.document_name}' needs to be completed"
                        f" by hand: {e.message}",
                    )
                )
                continue
            raise unsafe(e.message) from e
        proposals.append(payload)
    draft.proposals = proposals


def _mark_failed(
    store: SchoolSiftStore, household_id: str, record: MessageRecord
) -> None:
    store.set_message_status(
        household_id,
        record.connection_id,
        record.provider_message_id,
        "failed",
        reason=_GENERIC_FAILURE,
    )


def process_message(
    household_id: str,
    message_id: str,
    *,
    store: SchoolSiftStore,
    analyzer: MessageAnalyzer,
) -> ActionPacket:
    record = store.get_message_record(household_id, message_id)
    if record.status == "processed":
        packet = store.get_packet_for_message(household_id, record.id)
        if packet is not None and packet.source_message_id == record.id:
            return packet
        raise ConflictError(
            "This message is marked processed but its packet is missing."
        )
    if record.status != "awaiting_agent" or not record.source_confirmed:
        raise ConflictError(
            "This message is not ready to process; trust its sender and sync first."
        )
    docs = store.list_document_records(household_id, record.id)
    try:
        draft = analyzer.analyze(household_id, message_id)
        _validate_draft(record, docs, draft)
    except ModelAccessError:
        # Infrastructure failure, not a message defect — leave the message
        # awaiting_agent so a later retry can process it.
        raise
    except SchoolSiftError:
        _mark_failed(store, household_id, record)
        raise
    except Exception as e:
        _mark_failed(store, household_id, record)
        raise AgentError("The agent could not process this message.") from e
    cited_sources = {e.source for e in draft.evidence}
    sender = (
        f"{record.sender_name} <{record.sender_email}>"
        if record.sender_name and record.sender_name != record.sender_email
        else record.sender_email
    )
    packet = ActionPacket(
        id=f"pkt-{secrets.token_hex(8)}",
        source_message_id=record.id,
        sender=sender,
        subject=record.subject,
        summary=draft.summary,
        child=draft.child,
        deadline=draft.deadline,
        urgency=draft.urgency,
        information_only=draft.information_only,
        evidence=draft.evidence,
        uncertainties=draft.uncertainties,
        attachments=[
            PacketAttachment(
                name=d.name,
                mime=d.mime,
                cited=d.name in cited_sources,
            )
            for d in docs
        ],
        proposals=[
            ProposalVersion(
                id=f"prop-{secrets.token_hex(8)}",
                version=1,
                status="proposed",
                payload=payload,
                payload_hash=payload_digest(payload),
            )
            for payload in draft.proposals
        ],
    )
    return store.save_processed_packet(
        household_id,
        record.connection_id,
        record.provider_message_id,
        packet,
    )
