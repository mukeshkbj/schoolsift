from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from email.utils import parseaddr

from .domain import (
    CalendarProposal,
    PdfFormProposal,
    ProposalPayload,
    ReplyProposal,
)
from .errors import UnsafeProposalError
from .models import DocumentRecord, MessageRecord

_SENSITIVE_FIELD = re.compile(r"signature|payment|card|bank")


def _mailbox(value: str) -> str:
    return parseaddr(value)[1].strip().lower()


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None


def validate_proposal_payload(
    record: MessageRecord,
    docs: Sequence[DocumentRecord],
    payload: ProposalPayload,
) -> None:
    if isinstance(payload, (ReplyProposal, PdfFormProposal)):
        allowed = {_mailbox(record.sender_email)}
        if record.reply_to:
            allowed.add(_mailbox(record.reply_to))
        allowed.discard("")
        if _mailbox(payload.recipient) not in allowed:
            raise UnsafeProposalError(
                "Deliveries may only go to the original school sender."
            )
    if isinstance(payload, CalendarProposal):
        if not _aware(payload.starts_at) or not _aware(payload.ends_at):
            raise UnsafeProposalError("Event times must include a timezone.")
        if payload.ends_at <= payload.starts_at:
            raise UnsafeProposalError("Event end must be after its start.")
    if isinstance(payload, PdfFormProposal):
        matches = [d for d in docs if d.name == payload.document_name]
        if len(matches) != 1:
            raise UnsafeProposalError("The selected attachment could not be matched.")
        doc = matches[0]
        if doc.mime.split(";")[0].strip().lower() != "application/pdf":
            raise UnsafeProposalError("The selected attachment is not a PDF form.")
        known = set(doc.acroform_fields)
        for name in payload.fields:
            if name not in known:
                raise UnsafeProposalError(
                    "The proposed field is not part of the PDF form."
                )
            normalized = re.sub(r"[^a-z0-9]", "", name.lower())
            if _SENSITIVE_FIELD.search(normalized):
                raise UnsafeProposalError(
                    "Signature and payment fields cannot be filled here."
                )
