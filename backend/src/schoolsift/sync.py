from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from .content import ContentStore
from .credentials import CredentialVault, OAuthCredentials
from .documents import read_document
from .errors import (
    ConflictError,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfiguredError,
    SchoolSiftError,
    UnsupportedDocumentError,
)
from .models import DocumentRecord, MessageHeader, SyncResult
from .providers import MailProvider, Provider
from .store import SchoolSiftStore

REFRESH_WINDOW = timedelta(minutes=5)
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_GENERIC_FAILURE = "Message could not be imported; review it in your inbox."


class _StoredCursor:
    pass


STORED_CURSOR = _StoredCursor()


def _drop_refs(content: ContentStore, household_id: str, refs: list[str]) -> None:
    for ref in refs:
        with contextlib.suppress(Exception):
            content.delete(household_id, ref)


def sync_connection(
    household_id: str,
    connection_id: str,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
    max_pages: int = 10,
    cursor: str | None | _StoredCursor = STORED_CURSOR,
) -> SyncResult:
    conn = store.get_connection(household_id, connection_id)
    if conn.status != "connected":
        raise ConflictError(
            "This account is not connected; reconnect it before syncing."
        )
    adapter = providers.get(conn.provider)
    if adapter is None:
        raise ProviderNotConfiguredError(
            f"{conn.provider} OAuth is not configured on this API."
        )
    store.update_connection_sync(household_id, connection_id, sync_status="syncing")
    try:
        credentials = vault.get(connection_id)
    except Exception as e:
        store.set_connection_status(
            household_id, connection_id, "reauthorization_required"
        )
        store.update_connection_sync(
            household_id,
            connection_id,
            sync_status="reauthorization_required",
        )
        raise ProviderAuthError(
            "Stored credentials are unavailable; reconnect the account."
        ) from e
    try:
        if (
            credentials.expires_at is not None
            and credentials.expires_at <= datetime.now(UTC) + REFRESH_WINDOW
        ):
            credentials = adapter.refresh(credentials)
            vault.put(connection_id, credentials)
        return _sync_pages(
            household_id,
            conn.sync_cursor if isinstance(cursor, _StoredCursor) else cursor,
            connection_id,
            adapter=adapter,
            credentials=credentials,
            store=store,
            content=content,
            max_pages=max_pages,
        )
    except ProviderAuthError:
        store.set_connection_status(
            household_id, connection_id, "reauthorization_required"
        )
        store.update_connection_sync(
            household_id,
            connection_id,
            sync_status="reauthorization_required",
        )
        raise
    except Exception as e:
        store.update_connection_sync(household_id, connection_id, sync_status="failed")
        if isinstance(e, SchoolSiftError):
            raise
        raise ProviderError("Inbox sync failed.") from e


def _sync_pages(
    household_id: str,
    cursor: str | None,
    connection_id: str,
    *,
    adapter: MailProvider,
    credentials: OAuthCredentials,
    store: SchoolSiftStore,
    content: ContentStore,
    max_pages: int,
) -> SyncResult:
    discovered = 0
    imported = 0
    awaiting = 0
    failed = 0
    has_more = False
    seen_cursors: set[str | None] = set()
    pages = 0
    while True:
        if cursor in seen_cursors:
            raise ProviderError("Provider sync cursor did not advance.")
        seen_cursors.add(cursor)
        page = adapter.list_message_headers(credentials, cursor=cursor)
        pages += 1
        discovered += len(page.messages)
        records: list[tuple[MessageHeader, str, str]] = []
        for header in page.messages:
            store.upsert_source_suggestion(
                household_id,
                connection_id,
                sender_email=header.sender_email,
                sender_name=header.sender_name,
                seen_at=header.received_at,
            )
            record = store.upsert_message_header(household_id, connection_id, header)
            records.append((header, record.id, record.status))
        sources = {s.sender_email: s.status for s in store.list_sources(household_id)}
        for header, _record_id, record_status in records:
            if sources.get(header.sender_email) != "confirmed":
                awaiting += 1
                continue
            if record_status in ("awaiting_agent", "processed"):
                continue
            refs: list[str] = []
            try:
                fetched = adapter.fetch_message(credentials, header.provider_message_id)
                fetched = fetched.model_copy(
                    update={"thread_id": fetched.thread_id or header.thread_id}
                )
                oversized = [
                    a.name
                    for a in fetched.attachments
                    if len(a.content) > MAX_ATTACHMENT_BYTES
                ]
                if oversized:
                    raise UnsupportedDocumentError(
                        f"Attachment {', '.join(oversized)} exceeds the 25 MB"
                        " limit; review it in your inbox."
                    )
                refs.append(content.put(household_id, fetched.body_text.encode()))
                attachments = []
                for a in fetched.attachments:
                    doc = read_document(
                        DocumentRecord(
                            id="",
                            household_id=household_id,
                            message_id="",
                            name=a.name,
                            mime=a.mime,
                            content_ref="",
                            acroform_fields=[],
                        ),
                        a.content,
                    )
                    refs.append(content.put(household_id, a.content))
                    attachments.append(
                        (a.name, doc.mime, refs[-1], doc.acroform_fields)
                    )
                store.save_fetched_message(
                    household_id,
                    connection_id,
                    fetched,
                    body_ref=refs[0],
                    attachments=attachments,
                )
                imported += 1
            except ProviderAuthError:
                _drop_refs(content, household_id, refs)
                raise
            except Exception as e:
                _drop_refs(content, household_id, refs)
                reason = (
                    e.message if isinstance(e, SchoolSiftError) else _GENERIC_FAILURE
                )
                store.set_message_status(
                    household_id,
                    connection_id,
                    header.provider_message_id,
                    "failed",
                    reason=reason,
                )
                failed += 1
        cursor = page.next_cursor
        if not page.has_more:
            has_more = False
            break
        if pages >= max_pages:
            has_more = True
            break
    next_cursor = cursor
    store.update_connection_sync(
        household_id,
        connection_id,
        sync_status="ready",
        sync_cursor=next_cursor,
        last_sync_at=datetime.now(UTC),
    )
    return SyncResult(
        discovered=discovered,
        imported=imported,
        awaiting_source_confirmation=awaiting,
        failed=failed,
        next_cursor=next_cursor,
        has_more=has_more,
    )
