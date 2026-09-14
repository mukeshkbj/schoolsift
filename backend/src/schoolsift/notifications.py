from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .content import ContentStore
from .credentials import CredentialVault
from .errors import FullResyncRequiredError
from .models import SyncResult
from .providers import MailProvider, Provider
from .store import SchoolSiftStore
from .sync import sync_connection

IngestionEventKind = Literal[
    "mail_changed", "subscription_removed", "reauthorization_required", "missed"
]


class IngestionEvent(BaseModel):
    id: str = Field(max_length=200)
    provider: Literal["gmail", "outlook"]
    household_id: str = Field(max_length=200)
    connection_id: str = Field(max_length=200)
    kind: IngestionEventKind
    provider_cursor: str | None = Field(default=None, max_length=2_000)
    received_at: datetime


class IngestionQueue(Protocol):
    def enqueue(self, event: IngestionEvent, *, dedupe_key: str) -> bool: ...


class SQLiteIngestionQueue:
    def __init__(self, store: SchoolSiftStore) -> None:
        self._store = store

    def enqueue(self, event: IngestionEvent, *, dedupe_key: str) -> bool:
        return self._store.enqueue_ingestion_event(event, dedupe_key=dedupe_key)


def _sync_for_event(
    event: IngestionEvent,
    cursor: str | None,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
    max_pages: int,
) -> SyncResult:
    return sync_connection(
        event.household_id,
        event.connection_id,
        store=store,
        vault=vault,
        content=content,
        providers=providers,
        max_pages=max_pages,
        cursor=cursor,
    )


def _record_checkpoint(
    event: IngestionEvent,
    result: SyncResult,
    store: SchoolSiftStore,
) -> None:
    subscription = store.get_active_subscription_for_connection(
        event.household_id, event.connection_id
    )
    if subscription is not None and result.next_cursor is not None:
        store.update_webhook_subscription(
            event.household_id, subscription.id, cursor=result.next_cursor
        )


def process_ingestion_event(
    event: IngestionEvent,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
    max_pages: int = 10,
) -> None:
    if event.kind == "mail_changed":
        subscription = store.get_active_subscription_for_connection(
            event.household_id, event.connection_id
        )
        if subscription is not None and subscription.cursor is not None:
            cursor: str | None = subscription.cursor
        else:
            cursor = store.get_connection(
                event.household_id, event.connection_id
            ).sync_cursor
        try:
            result = _sync_for_event(
                event,
                cursor,
                store=store,
                vault=vault,
                content=content,
                providers=providers,
                max_pages=max_pages,
            )
        except FullResyncRequiredError:
            result = _sync_for_event(
                event,
                None,
                store=store,
                vault=vault,
                content=content,
                providers=providers,
                max_pages=max_pages,
            )
            if result.next_cursor is None and subscription is not None:
                store.update_webhook_subscription(
                    event.household_id,
                    subscription.id,
                    status="renewal_due",
                    cursor=None,
                )
        _record_checkpoint(event, result, store)
    elif event.kind == "missed":
        result = _sync_for_event(
            event,
            None,
            store=store,
            vault=vault,
            content=content,
            providers=providers,
            max_pages=max_pages,
        )
        subscription = store.get_active_subscription_for_connection(
            event.household_id, event.connection_id
        )
        if subscription is not None:
            if result.next_cursor is None:
                store.update_webhook_subscription(
                    event.household_id,
                    subscription.id,
                    status="renewal_due",
                    cursor=None,
                )
            else:
                store.update_webhook_subscription(
                    event.household_id,
                    subscription.id,
                    cursor=result.next_cursor,
                )
    elif event.kind == "reauthorization_required":
        store.set_connection_status(
            event.household_id, event.connection_id, "reauthorization_required"
        )
        store.update_connection_sync(
            event.household_id,
            event.connection_id,
            sync_status="reauthorization_required",
        )
    elif event.kind == "subscription_removed":
        subscription = store.get_active_subscription_for_connection(
            event.household_id, event.connection_id
        )
        if subscription is not None:
            store.set_webhook_subscription_status(
                event.household_id, subscription.id, "renewal_due"
            )
