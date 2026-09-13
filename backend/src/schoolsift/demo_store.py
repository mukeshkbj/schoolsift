from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from . import domain
from .domain import (
    ActionPacket,
    CalendarProposal,
    PdfFormProposal,
    ProposalPayload,
    ProposalVersion,
    ReplyProposal,
    payload_digest,
)
from .errors import NotFoundError
from .processor import process_message

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEMO_DIR = REPO_ROOT / "demo"


class Sender(BaseModel):
    name: str
    email: str


class Attachment(BaseModel):
    id: str
    name: str
    mime: str
    path: str


class MessageSource(BaseModel):
    id: str
    confirmed: bool
    domain: str


class DemoMessage(BaseModel):
    id: str
    provider: str
    account: str
    thread_id: str
    sender: Sender
    reply_to: str
    subject: str
    received_at: datetime
    source: MessageSource
    body: str
    attachments: list[Attachment]


class Caregiver(BaseModel):
    id: str
    name: str
    email: str
    role: str


class Child(BaseModel):
    id: str
    name: str
    school: str
    grade: str


class FamilyEvent(BaseModel):
    id: str
    title: str
    starts_at: datetime
    ends_at: datetime


class DemoContext(BaseModel):
    household_id: str
    label: str
    caregivers: list[Caregiver]
    children: list[Child]
    confirmed_sources: list[MessageSource]
    family_calendar: list[FamilyEvent]


class DemoOutcome(BaseModel):
    id: str
    kind: str
    label: str = "Demo data"
    proposal_id: str
    version: int
    detail: str
    created_at: datetime


class DemoState(BaseModel):
    label: str
    messages: list[DemoMessage]
    packets: list[ActionPacket]
    outcomes: list[DemoOutcome]


class DemoStore:
    def __init__(self, demo_dir: Path = DEFAULT_DEMO_DIR) -> None:
        self.demo_dir = Path(demo_dir)
        self.context = DemoContext.model_validate(
            json.loads((self.demo_dir / "context.json").read_text())
        )
        self.messages = [
            DemoMessage.model_validate(json.loads(p.read_text()))
            for p in sorted((self.demo_dir / "messages").glob("*.json"))
        ]
        self.reset()

    def reset(self) -> None:
        self._packets: dict[str, ActionPacket] = {}
        self._outcomes: list[DemoOutcome] = []

    def state(self) -> DemoState:
        return DemoState(
            label=self.context.label,
            messages=self.messages,
            packets=list(self._packets.values()),
            outcomes=list(self._outcomes),
        )

    def process(self, message_ids: list[str] | None) -> list[ActionPacket]:
        wanted = [m.id for m in self.messages] if message_ids is None else message_ids
        packets: list[ActionPacket] = []
        for message_id in wanted:
            if message_id in self._packets:
                packets.append(self._packets[message_id])
                continue
            message = next((m for m in self.messages if m.id == message_id), None)
            if message is None:
                raise NotFoundError(f"Unknown demo message: {message_id}")
            draft = process_message(message, self.context, self.demo_dir)
            packet = self._persist_draft(message, draft)
            packets.append(packet)
        return packets

    def _persist_draft(
        self, message: DemoMessage, draft: domain.ActionPacketDraft
    ) -> ActionPacket:
        proposals = [
            ProposalVersion(
                id=f"{message.id}-prop-{i}-{payload.kind}",
                version=1,
                status="proposed",
                payload=payload,
                payload_hash=payload_digest(payload),
            )
            for i, payload in enumerate(draft.proposals, start=1)
        ]
        packet = ActionPacket(
            id=f"packet-{message.id}",
            source_message_id=message.id,
            sender=f"{message.sender.name} <{message.sender.email}>",
            subject=message.subject,
            summary=draft.summary,
            child=draft.child,
            deadline=draft.deadline,
            urgency=draft.urgency,
            information_only=draft.information_only,
            evidence=draft.evidence,
            uncertainties=draft.uncertainties,
            proposals=proposals,
        )
        self._packets[message.id] = packet
        return packet

    def _versions(self, proposal_id: str) -> list[ProposalVersion]:
        for packet in self._packets.values():
            versions = packet.versions_for(proposal_id)
            if versions:
                return versions
        raise NotFoundError(f"Unknown proposal: {proposal_id}")

    def _packet_of(self, proposal_id: str) -> ActionPacket:
        for packet in self._packets.values():
            if packet.versions_for(proposal_id):
                return packet
        raise NotFoundError(f"Unknown proposal: {proposal_id}")

    def edit(
        self, proposal_id: str, *, expected_version: int, payload: ProposalPayload
    ) -> ProposalVersion:
        packet = self._packet_of(proposal_id)
        current = packet.versions_for(proposal_id)
        updated = domain.edit_proposal(
            current,
            proposal_id=proposal_id,
            expected_version=expected_version,
            new_payload=payload,
        )
        packet.proposals = [v for v in packet.proposals if v.id != proposal_id] + [
            v for v in updated if v.id == proposal_id
        ]
        return updated[-1]

    def approve(
        self, proposal_id: str, *, version: int, payload_hash: str
    ) -> tuple[ProposalVersion, DemoOutcome]:
        packet = self._packet_of(proposal_id)
        versions = packet.versions_for(proposal_id)
        domain.approve_proposal(versions, version=version, payload_hash=payload_hash)
        approved = next(v for v in versions if v.version == version)
        outcome = self._record_outcome(approved)
        return approved, outcome

    def reject(
        self, proposal_id: str, *, version: int, payload_hash: str
    ) -> ProposalVersion:
        versions = self._versions(proposal_id)
        domain.reject_proposal(versions, version=version, payload_hash=payload_hash)
        return next(v for v in versions if v.version == version)

    def _record_outcome(self, version: ProposalVersion) -> DemoOutcome:
        payload = version.payload
        if isinstance(payload, ReplyProposal):
            kind = "demo_reply"
            detail = f"Demo reply to {payload.recipient}: {payload.subject}"
        elif isinstance(payload, CalendarProposal):
            kind = "demo_calendar_event"
            detail = (
                f"Demo calendar event: {payload.title} "
                f"({payload.starts_at.isoformat()} to {payload.ends_at.isoformat()})"
            )
        elif isinstance(payload, PdfFormProposal):
            kind = "demo_form"
            detail = f"Demo completed form: {payload.document_name}"
        else:  # pragma: no cover - escalations cannot be approved
            raise NotFoundError("Escalation has no demo outcome.")
        outcome = DemoOutcome(
            id=f"out-{version.id}-v{version.version}",
            kind=kind,
            proposal_id=version.id,
            version=version.version,
            detail=detail,
            created_at=datetime.now(UTC),
        )
        self._outcomes.append(outcome)
        return outcome
