from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .errors import (
    ConflictError,
    HashMismatchError,
    NotApprovableError,
    NotFoundError,
)

BoundedStr = Annotated[str, Field(max_length=1_000)]
FieldValue = Annotated[str, Field(max_length=10_000)]


class EvidenceSpan(BaseModel):
    source: str = Field(
        max_length=1_000,
        description="Exactly 'body' for the email text, or the attachment filename.",
    )
    quote: str = Field(max_length=10_000)


class ReplyProposal(BaseModel):
    kind: Literal["reply"]
    recipient: str = Field(max_length=500)
    subject: str = Field(max_length=1_000)
    body: str = Field(max_length=100_000)


class CalendarProposal(BaseModel):
    kind: Literal["calendar"]
    title: str = Field(max_length=1_000)
    starts_at: datetime
    ends_at: datetime


class PdfFormProposal(BaseModel):
    kind: Literal["pdf_form"]
    recipient: str = Field(max_length=500)
    subject: str = Field(max_length=1_000)
    body: str = Field(max_length=100_000)
    document_name: str = Field(max_length=1_000)
    fields: dict[FieldValue, FieldValue] = Field(max_length=200)


class EscalationProposal(BaseModel):
    kind: Literal["escalation"]
    reason: Literal["payment", "signature", "uncertain", "unsupported_document"]
    detail: str = Field(max_length=10_000)


ProposalPayload = Annotated[
    ReplyProposal | CalendarProposal | PdfFormProposal | EscalationProposal,
    Field(discriminator="kind"),
]

ProposalStatus = Literal["proposed", "approved", "rejected", "superseded", "completed"]


class ProposalVersion(BaseModel):
    id: str
    version: int
    status: ProposalStatus
    payload: ProposalPayload
    payload_hash: str


class PacketAttachment(BaseModel):
    name: str = Field(max_length=1_000)
    mime: str = Field(max_length=200)
    cited: bool = False


class ActionPacket(BaseModel):
    id: str = Field(max_length=200)
    source_message_id: str = Field(max_length=500)
    sender: str = Field(max_length=1_000)
    subject: str = Field(max_length=1_000)
    summary: str = Field(max_length=10_000)
    child: str | None = Field(max_length=500)
    deadline: datetime | None
    urgency: Literal["none", "soon", "urgent"]
    information_only: bool
    evidence: list[EvidenceSpan] = Field(max_length=100)
    uncertainties: list[BoundedStr] = Field(max_length=100)
    attachments: list[PacketAttachment] = Field(default_factory=list, max_length=100)
    proposals: list[ProposalVersion] = Field(max_length=500)

    def head_proposals(self) -> list[ProposalVersion]:
        heads: dict[str, ProposalVersion] = {}
        for version in self.proposals:
            heads[version.id] = version
        return list(heads.values())

    def versions_for(self, proposal_id: str) -> list[ProposalVersion]:
        return [v for v in self.proposals if v.id == proposal_id]


class ActionPacketDraft(BaseModel):
    source_message_id: str = Field(max_length=500)
    school_source_id: str | None = Field(default=None, max_length=500)
    summary: str = Field(max_length=10_000)
    child: str | None = Field(default=None, max_length=500)
    deadline: datetime | None = None
    urgency: Literal["none", "soon", "urgent"] = "none"
    information_only: bool = False
    evidence: list[EvidenceSpan] = Field(default_factory=list, max_length=100)
    uncertainties: list[BoundedStr] = Field(default_factory=list, max_length=100)
    proposals: list[ProposalPayload] = Field(default_factory=list, max_length=50)


def payload_digest(payload: ProposalPayload) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _head(versions: list[ProposalVersion]) -> ProposalVersion:
    return max(versions, key=lambda v: v.version)


def edit_proposal(
    versions: list[ProposalVersion],
    *,
    proposal_id: str,
    expected_version: int,
    new_payload: ProposalPayload,
) -> list[ProposalVersion]:
    head = _head(versions)
    if head.version != expected_version or head.status != "proposed":
        raise ConflictError(
            f"Proposal {proposal_id} is no longer at proposed version "
            f"{expected_version}; refresh and reapply the edit."
        )
    head.status = "superseded"
    new_version = ProposalVersion(
        id=proposal_id,
        version=head.version + 1,
        status="proposed",
        payload=new_payload,
        payload_hash=payload_digest(new_payload),
    )
    return [*versions, new_version]


def _decidable(versions: list[ProposalVersion], version: int) -> ProposalVersion:
    for v in versions:
        if v.version == version:
            target = v
            break
    else:
        raise NotFoundError(f"Proposal version {version} does not exist.")
    if target is not _head(versions) or target.status != "proposed":
        raise ConflictError(
            f"Version {version} is not the current proposed version; "
            "another decision or edit already won."
        )
    return target


def approve_proposal(
    versions: list[ProposalVersion], *, version: int, payload_hash: str
) -> list[ProposalVersion]:
    target = _decidable(versions, version)
    if target.payload.kind == "escalation":
        raise NotApprovableError(
            "Escalations require a human; they can never be approved."
        )
    if target.payload_hash != payload_hash:
        raise HashMismatchError(
            "The reviewed payload has changed; refresh and review again."
        )
    target.status = "approved"
    return versions


def reject_proposal(
    versions: list[ProposalVersion], *, version: int, payload_hash: str
) -> list[ProposalVersion]:
    target = _decidable(versions, version)
    if target.payload_hash != payload_hash:
        raise HashMismatchError(
            "The reviewed payload has changed; refresh and review again."
        )
    target.status = "rejected"
    return versions
