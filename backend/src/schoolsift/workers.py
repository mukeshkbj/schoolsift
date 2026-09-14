from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .content import ContentStore
from .credentials import CredentialVault
from .execution import execute_command
from .models import ExecutionCommand, RenewalResult
from .notifications import IngestionEvent, process_ingestion_event
from .providers import MailProvider, Provider
from .store import SchoolSiftStore
from .subscription_service import renew_due_subscriptions

MAX_BATCH_RECORDS = 10

BatchResponse = dict[str, list[dict[str, str]]]


def _record_identity(record: object) -> tuple[str, str]:
    if not isinstance(record, dict):
        raise ValueError("Malformed SQS record.")
    message_id = record.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("SQS record is missing a message identifier.")
    attributes = record.get("attributes")
    group_id: object = None
    if attributes is not None:
        if not isinstance(attributes, dict):
            raise ValueError("SQS record attributes are malformed.")
        group_id = attributes.get("MessageGroupId")
        if group_id is not None and not isinstance(group_id, str):
            raise ValueError("SQS record attributes are malformed.")
    return message_id, group_id if isinstance(group_id, str) else message_id


def create_ingestion_handler(
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
    max_pages: int = 10,
) -> Callable[[dict[str, Any], object], BatchResponse]:
    def handler(event: dict[str, Any], _context: object) -> BatchResponse:
        records = event.get("Records") if isinstance(event, dict) else None
        if not isinstance(records, list):
            raise ValueError("SQS event is missing a Records list.")
        failures: list[str] = []
        failed_groups: set[str] = set()
        for record in records[:MAX_BATCH_RECORDS]:
            message_id, group_key = _record_identity(record)
            if group_key in failed_groups:
                failures.append(message_id)
                continue
            try:
                parsed = IngestionEvent.model_validate_json(record.get("body", ""))
                process_ingestion_event(
                    parsed,
                    store=store,
                    vault=vault,
                    content=content,
                    providers=providers,
                    max_pages=max_pages,
                )
            except Exception:
                failed_groups.add(group_key)
                failures.append(message_id)
        for record in records[MAX_BATCH_RECORDS:]:
            failures.append(_record_identity(record)[0])
        return {
            "batchItemFailures": [
                {"itemIdentifier": message_id} for message_id in failures
            ]
        }

    return handler


def create_renewal_handler(
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    providers: Mapping[Provider, MailProvider],
    settings: Settings,
) -> Callable[[dict[str, Any], object], dict[str, int]]:
    def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
        result: RenewalResult = renew_due_subscriptions(
            datetime.now(UTC),
            store=store,
            vault=vault,
            providers=providers,
            settings=settings,
        )
        return result.model_dump()

    return handler


def create_execution_handler(
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    content: ContentStore,
    providers: Mapping[Provider, MailProvider],
) -> Callable[[dict[str, Any], object], BatchResponse]:
    def handler(event: dict[str, Any], _context: object) -> BatchResponse:
        records = event.get("Records") if isinstance(event, dict) else None
        if not isinstance(records, list):
            raise ValueError("SQS event is missing a Records list.")
        failures: list[str] = []
        failed_groups: set[str] = set()
        for record in records[:MAX_BATCH_RECORDS]:
            message_id, group_key = _record_identity(record)
            if group_key in failed_groups:
                failures.append(message_id)
                continue
            try:
                parsed = ExecutionCommand.model_validate_json(record.get("body", ""))
                result = execute_command(
                    parsed,
                    store=store,
                    vault=vault,
                    content=content,
                    providers=providers,
                )
            except Exception:
                failed_groups.add(group_key)
                failures.append(message_id)
                continue
            if result is not None and result.status == "queued":
                failed_groups.add(group_key)
                failures.append(message_id)
        for record in records[MAX_BATCH_RECORDS:]:
            failures.append(_record_identity(record)[0])
        return {
            "batchItemFailures": [
                {"itemIdentifier": message_id} for message_id in failures
            ]
        }

    return handler
