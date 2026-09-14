from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from .domain import ProposalVersion

Role = Literal["owner", "editor", "viewer"]
InvitationRole = Literal["editor", "viewer"]


class Household(BaseModel):
    id: str
    name: str
    timezone: str
    created_at: datetime


class Child(BaseModel):
    id: str
    household_id: str
    name: str
    school: str
    grade: str
    created_at: datetime


class Connection(BaseModel):
    id: str
    household_id: str
    provider: Literal["gmail", "outlook"]
    email: str
    status: Literal["pending", "connected", "reauthorization_required", "disconnected"]
    last_sync_at: datetime | None
    created_at: datetime
    sync_status: Literal[
        "idle", "syncing", "ready", "reauthorization_required", "failed"
    ] = "idle"
    provider_subject: str = Field(default="", exclude=True)
    sync_cursor: str | None = Field(default=None, exclude=True)


class SchoolSource(BaseModel):
    id: str
    household_id: str
    connection_id: str
    sender_email: str
    sender_domain: str
    sender_name: str = ""
    message_count: int = 0
    status: Literal["suggested", "confirmed", "rejected"]
    first_seen_at: datetime
    last_seen_at: datetime


class MessageRecord(BaseModel):
    id: str
    household_id: str
    connection_id: str
    provider_message_id: str
    thread_id: str
    sender_name: str
    sender_email: str
    reply_to: str
    subject: str
    received_at: datetime
    source_confirmed: bool
    status: Literal["awaiting_source", "awaiting_agent", "processed", "failed"] = (
        "awaiting_source"
    )
    manual_review_reason: str | None = None
    body_ref: str | None = Field(default=None, exclude=True)
    attachment_ids: list[str] = Field(default_factory=list, exclude=True)


class MessageHeader(BaseModel):
    provider_message_id: str
    thread_id: str
    sender_name: str
    sender_email: str
    reply_to: str
    subject: str
    received_at: datetime


class AttachmentContent(BaseModel):
    provider_attachment_id: str
    name: str
    mime: str
    content: bytes


class FetchedMessage(MessageHeader):
    body_text: str
    attachments: list[AttachmentContent]


class MessagePage(BaseModel):
    messages: list[MessageHeader]
    next_cursor: str | None
    has_more: bool


class SyncResult(BaseModel):
    discovered: int
    imported: int
    awaiting_source_confirmation: int
    failed: int = 0
    next_cursor: str | None
    has_more: bool = False


class NotificationRegistration(BaseModel):
    provider_subscription_id: str
    client_state_hash: str = Field(default="", exclude=True)
    cursor: str | None = Field(default=None, exclude=True)
    expires_at: datetime | None


class SubscriptionPublic(BaseModel):
    provider: Literal["gmail", "outlook"]
    status: Literal["active", "renewal_due", "expired", "reauthorization_required"]
    expires_at: datetime | None


class ConnectionView(Connection):
    subscription: SubscriptionPublic | None = None


class RenewalResult(BaseModel):
    checked: int
    renewed: int
    failed: int


class DocumentRecord(BaseModel):
    id: str
    household_id: str
    message_id: str
    name: str
    mime: str
    content_ref: str
    acroform_fields: list[str]


class CalendarRecord(BaseModel):
    id: str
    household_id: str
    title: str
    starts_at: datetime
    ends_at: datetime


class HouseholdContext(BaseModel):
    household_id: str
    children: list[str]
    connections: list[str]
    timezone: str = "UTC"


class NormalizedMessage(BaseModel):
    message_id: str
    thread_id: str
    sender_email: str
    reply_to: str
    subject: str
    received_at: datetime
    source_confirmed: bool
    body: str
    attachment_ids: list[str]


class DocumentContent(BaseModel):
    document_id: str
    name: str
    mime: str
    text: str
    acroform_fields: list[str]
    source_bytes: bytes = Field(default=b"", exclude=True)


class RelatedAction(BaseModel):
    packet_id: str
    summary: str
    status: str


class Membership(BaseModel):
    household_id: str
    user_id: str
    email: str
    role: Role
    created_at: datetime


class Invitation(BaseModel):
    id: str
    household_id: str
    email: str
    role: InvitationRole
    status: Literal["pending", "accepted", "revoked", "expired"]
    expires_at: datetime
    created_at: datetime


class InvitationCreated(BaseModel):
    invitation: Invitation
    token: SecretStr


class OAuthState(BaseModel):
    household_id: str
    provider: Literal["gmail", "outlook"]


class WebhookSubscription(BaseModel):
    id: str
    household_id: str
    connection_id: str
    provider: Literal["gmail", "outlook"]
    provider_subscription_id: str
    client_state_hash: str = Field(exclude=True)
    cursor: str | None = Field(default=None, exclude=True)
    expires_at: datetime | None
    status: Literal["active", "renewal_due", "expired", "reauthorization_required"]
    created_at: datetime
    updated_at: datetime


ExecutionStatus = Literal[
    "pending_dispatch",
    "queued",
    "executing",
    "completed",
    "failed",
    "delivery_uncertain",
]


class ExecutionRecord(BaseModel):
    id: str
    household_id: str
    proposal_id: str
    proposal_version: int
    connection_id: str
    idempotency_key: str
    status: ExecutionStatus
    attempts: int
    provider_operation_id: str | None
    output_ref: str | None = Field(default=None, exclude=True)
    safe_error: str | None
    created_at: datetime
    updated_at: datetime


class ExecutionCommand(BaseModel):
    execution_id: str = Field(max_length=200)
    household_id: str = Field(max_length=200)


class PendingDispatch(BaseModel):
    execution_id: str
    household_id: str
    idempotency_key: str


class ApprovalResponse(BaseModel):
    proposal: ProposalVersion
    execution: ExecutionRecord | None


class RunRequest(BaseModel):
    model_config = {"extra": "forbid"}
    confirm: bool


class DispatchResult(BaseModel):
    dispatched: int
