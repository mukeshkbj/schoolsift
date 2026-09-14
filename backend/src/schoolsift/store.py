from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from .domain import ActionPacket, ProposalPayload, ProposalVersion
from .identity import Principal
from .models import (
    CalendarRecord,
    Child,
    Connection,
    DocumentRecord,
    ExecutionRecord,
    FetchedMessage,
    Household,
    Invitation,
    Membership,
    MessageHeader,
    MessageRecord,
    OAuthState,
    Role,
    SchoolSource,
    WebhookSubscription,
)

if TYPE_CHECKING:
    from .notifications import IngestionEvent


class SchoolSiftStore(Protocol):
    def initialize(self) -> None: ...

    def get_household(self) -> Household | None: ...

    def get_household_by_id(self, household_id: str) -> Household | None: ...

    def create_household(self, *, name: str, timezone: str) -> Household: ...

    def create_household_for_owner(
        self, principal: Principal, *, name: str, timezone: str
    ) -> Household: ...

    def list_memberships_for_user(self, user_id: str) -> list[Membership]: ...

    def get_membership(self, household_id: str, user_id: str) -> Membership | None: ...

    def list_memberships(self, household_id: str) -> list[Membership]: ...

    def create_invitation(
        self,
        household_id: str,
        *,
        email: str,
        role: Literal["editor", "viewer"],
        expires_at: datetime,
    ) -> tuple[Invitation, str]: ...

    def list_invitations(self, household_id: str) -> list[Invitation]: ...

    def revoke_invitation(
        self, household_id: str, invitation_id: str
    ) -> Invitation: ...

    def accept_invitation(self, principal: Principal, token: str) -> Membership: ...

    def change_member_role(
        self, household_id: str, user_id: str, role: Role
    ) -> Membership: ...

    def remove_member(self, household_id: str, user_id: str) -> None: ...

    def list_children(self, household_id: str) -> list[Child]: ...

    def add_child(
        self, household_id: str, *, name: str, school: str, grade: str
    ) -> Child: ...

    def list_connections(self, household_id: str) -> list[Connection]: ...

    def list_messages(self, household_id: str) -> list[MessageRecord]: ...

    def list_packets(self, household_id: str) -> list[ActionPacket]: ...

    def get_packet(self, household_id: str, packet_id: str) -> ActionPacket: ...

    def save_packet(self, household_id: str, packet: ActionPacket) -> ActionPacket: ...

    def save_processed_packet(
        self,
        household_id: str,
        connection_id: str,
        provider_message_id: str,
        packet: ActionPacket,
    ) -> ActionPacket: ...

    def edit_proposal(
        self,
        household_id: str,
        proposal_id: str,
        *,
        expected_version: int,
        payload: ProposalPayload,
    ) -> ProposalVersion: ...

    def approve_proposal(
        self,
        household_id: str,
        proposal_id: str,
        *,
        version: int,
        payload_hash: str,
    ) -> tuple[ProposalVersion, ExecutionRecord]: ...

    def reject_proposal(
        self,
        household_id: str,
        proposal_id: str,
        *,
        version: int,
        payload_hash: str,
    ) -> ProposalVersion: ...

    def create_oauth_state(self, household_id: str, provider: str) -> str: ...

    def consume_oauth_state(self, state: str) -> OAuthState: ...

    def upsert_connection(
        self,
        household_id: str,
        *,
        provider: str,
        provider_subject: str,
        email: str,
    ) -> Connection: ...

    def get_connection(self, household_id: str, connection_id: str) -> Connection: ...

    def set_connection_status(
        self,
        household_id: str,
        connection_id: str,
        status: Literal[
            "pending", "connected", "reauthorization_required", "disconnected"
        ],
    ) -> Connection: ...

    def disconnect_connection(
        self, household_id: str, connection_id: str
    ) -> Connection: ...

    def restore_connection(
        self,
        household_id: str,
        connection_id: str,
        *,
        email: str,
        status: Literal[
            "pending", "connected", "reauthorization_required", "disconnected"
        ],
    ) -> Connection: ...

    def update_connection_sync(
        self,
        household_id: str,
        connection_id: str,
        *,
        sync_status: object = ...,
        sync_cursor: object = ...,
        last_sync_at: object = ...,
    ) -> Connection: ...

    def upsert_source_suggestion(
        self,
        household_id: str,
        connection_id: str,
        *,
        sender_email: str,
        sender_name: str,
        seen_at: datetime,
    ) -> SchoolSource: ...

    def list_sources(self, household_id: str) -> list[SchoolSource]: ...

    def set_source_status(
        self,
        household_id: str,
        source_id: str,
        status: Literal["confirmed", "rejected"],
    ) -> SchoolSource: ...

    def upsert_message_header(
        self,
        household_id: str,
        connection_id: str,
        header: MessageHeader,
    ) -> MessageRecord: ...

    def save_fetched_message(
        self,
        household_id: str,
        connection_id: str,
        message: FetchedMessage,
        *,
        body_ref: str,
        attachments: list[tuple[str, str, str, list[str]]],
    ) -> MessageRecord: ...

    def set_message_status(
        self,
        household_id: str,
        connection_id: str,
        provider_message_id: str,
        status: Literal["awaiting_source", "awaiting_agent", "processed", "failed"],
        *,
        reason: str | None = None,
    ) -> None: ...

    def get_message_record(
        self, household_id: str, message_id: str
    ) -> MessageRecord: ...

    def reset_failed_message(
        self, household_id: str, message_id: str
    ) -> MessageRecord: ...

    def get_document_record(
        self, household_id: str, document_id: str
    ) -> DocumentRecord: ...

    def list_document_records(
        self, household_id: str, message_id: str
    ) -> list[DocumentRecord]: ...

    def list_calendar_records(self, household_id: str) -> list[CalendarRecord]: ...

    def get_packet_for_message(
        self, household_id: str, message_id: str
    ) -> ActionPacket | None: ...

    def find_connection_by_email(
        self, provider: str, email: str
    ) -> Connection | None: ...

    def enqueue_ingestion_event(
        self, event: IngestionEvent, *, dedupe_key: str
    ) -> bool: ...

    def claim_ingestion_events(
        self, limit: int, now: datetime, lease_seconds: int
    ) -> list[IngestionEvent]: ...

    def complete_ingestion_event(self, event_id: str) -> None: ...

    def fail_ingestion_event(
        self, event_id: str, retry_at: datetime, *, max_attempts: int = 5
    ) -> None: ...

    def upsert_webhook_subscription(
        self,
        household_id: str,
        connection_id: str,
        *,
        provider: str,
        provider_subscription_id: str,
        client_state_hash: str,
        cursor: str | None = None,
        expires_at: datetime | None = None,
    ) -> WebhookSubscription: ...

    def get_webhook_subscription(
        self, provider: str, provider_subscription_id: str
    ) -> WebhookSubscription | None: ...

    def get_subscription_for_connection(
        self, household_id: str, connection_id: str
    ) -> WebhookSubscription | None: ...

    def get_active_subscription_for_connection(
        self, household_id: str, connection_id: str
    ) -> WebhookSubscription | None: ...

    def set_webhook_subscription_status(
        self,
        household_id: str,
        subscription_id: str,
        status: Literal["active", "renewal_due", "expired", "reauthorization_required"],
    ) -> WebhookSubscription: ...

    def update_webhook_subscription(
        self,
        household_id: str,
        subscription_id: str,
        *,
        status: object = ...,
        cursor: object = ...,
    ) -> WebhookSubscription: ...

    def list_subscriptions_due(
        self,
        *,
        gmail_expiring_before: datetime,
        gmail_stale_before: datetime,
        outlook_expiring_before: datetime,
    ) -> list[WebhookSubscription]: ...

    def get_packet_by_proposal(
        self, household_id: str, proposal_id: str
    ) -> ActionPacket: ...

    def list_executions(self, household_id: str) -> list[ExecutionRecord]: ...

    def get_execution(
        self, household_id: str, execution_id: str
    ) -> ExecutionRecord: ...

    def claim_execution(
        self,
        household_id: str,
        execution_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ExecutionRecord | None: ...

    def list_dispatchable_executions(self, limit: int) -> list[ExecutionRecord]: ...

    def mark_execution_dispatched(
        self, household_id: str, execution_id: str
    ) -> ExecutionRecord: ...

    def record_outbox_failure(self, execution_id: str) -> None: ...

    def update_execution_checkpoint(
        self, household_id: str, execution_id: str, provider_operation_id: str
    ) -> None: ...

    def update_execution_output(
        self, household_id: str, execution_id: str, output_ref: str
    ) -> None: ...

    def settle_execution(
        self,
        household_id: str,
        execution_id: str,
        *,
        attempts: int,
        status: Literal["queued", "completed", "failed", "delivery_uncertain"],
        provider_operation_id: object = ...,
        output_ref: object = ...,
        safe_error: object = ...,
    ) -> bool: ...

    def record_audit(
        self, household_id: str, kind: str, entity_id: str, detail: str
    ) -> None: ...
