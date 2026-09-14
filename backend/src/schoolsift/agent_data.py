from __future__ import annotations

from datetime import datetime

from .content import ContentStore
from .documents import read_document
from .models import (
    CalendarRecord,
    DocumentContent,
    HouseholdContext,
    NormalizedMessage,
    RelatedAction,
)
from .store import SchoolSiftStore


class StoredAgentDataSource:
    def __init__(self, store: SchoolSiftStore, content: ContentStore) -> None:
        self._store = store
        self._content = content

    def _require_household(self, household_id: str) -> None:
        household = self._store.get_household()
        if household is None or household.id != household_id:
            raise PermissionError(f"Unknown household: {household_id}")

    def get_household_context(self, household_id: str) -> HouseholdContext:
        self._require_household(household_id)
        return HouseholdContext(
            household_id=household_id,
            children=[c.name for c in self._store.list_children(household_id)],
            connections=[
                c.email
                for c in self._store.list_connections(household_id)
                if c.status == "connected"
            ],
        )

    def get_message(self, household_id: str, message_id: str) -> NormalizedMessage:
        self._require_household(household_id)
        record = self._store.get_message_record(household_id, message_id)
        body = (
            self._content.get(household_id, record.body_ref).decode("utf-8", "replace")
            if record.body_ref
            else ""
        )
        return NormalizedMessage(
            message_id=record.id,
            thread_id=record.thread_id,
            sender_email=record.sender_email,
            reply_to=record.reply_to,
            subject=record.subject,
            received_at=record.received_at,
            source_confirmed=record.source_confirmed,
            body=body,
            attachment_ids=record.attachment_ids,
        )

    def get_document(self, household_id: str, document_id: str) -> DocumentContent:
        self._require_household(household_id)
        record = self._store.get_document_record(household_id, document_id)
        return read_document(
            record, self._content.get(household_id, record.content_ref)
        )

    def find_related_actions(
        self, household_id: str, thread_key: str
    ) -> list[RelatedAction]:
        self._require_household(household_id)
        thread_messages = {
            m.id
            for m in self._store.list_messages(household_id)
            if m.thread_id == thread_key
        }
        return [
            RelatedAction(packet_id=p.id, summary=p.summary, status="needs_review")
            for p in self._store.list_packets(household_id)
            if p.source_message_id in thread_messages
        ]

    def find_calendar_conflicts(
        self, household_id: str, start: datetime, end: datetime
    ) -> list[CalendarRecord]:
        self._require_household(household_id)
        return [
            r
            for r in self._store.list_calendar_records(household_id)
            if r.starts_at < end and start < r.ends_at
        ]
