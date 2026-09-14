from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from .config import Settings
from .credentials import CredentialVault, OAuthCredentials
from .errors import (
    ConflictError,
    ProviderAuthError,
    ProviderNotConfiguredError,
    ProviderNotFoundError,
    WebhookNotConfiguredError,
)
from .models import RenewalResult, WebhookSubscription
from .providers import MailProvider, Provider
from .store import SchoolSiftStore
from .sync import REFRESH_WINDOW

GMAIL_RENEW_AFTER = timedelta(hours=24)
GMAIL_EXPIRING_WITHIN = timedelta(hours=24)
OUTLOOK_EXPIRING_WITHIN = timedelta(hours=12)
OUTLOOK_SUBSCRIPTION_LIFE = timedelta(days=2, hours=23)


def _notification_urls(settings: Settings, provider: Provider) -> tuple[str, str]:
    base = str(settings.public_api_url).rstrip("/")
    if not base.startswith("https://"):
        raise WebhookNotConfiguredError(
            "Webhook notifications require an HTTPS public API URL."
        )
    if provider == "gmail" and settings.gmail_topic is None:
        raise WebhookNotConfiguredError(
            "Gmail notifications require a Pub/Sub topic configuration."
        )
    return f"{base}/v1/webhooks/{provider}", f"{base}/v1/webhooks/{provider}"


def _fresh_credentials(
    connection_id: str,
    adapter: MailProvider,
    vault: CredentialVault,
) -> OAuthCredentials:
    credentials = vault.get(connection_id)
    if (
        credentials.expires_at is not None
        and credentials.expires_at <= datetime.now(UTC) + REFRESH_WINDOW
    ):
        credentials = adapter.refresh(credentials)
        vault.put(connection_id, credentials)
    return credentials


def _register(
    subscription: WebhookSubscription | None,
    *,
    adapter: MailProvider,
    credentials: OAuthCredentials,
    connection_id: str,
    settings: Settings,
    provider: Provider,
) -> tuple[str, str | None, str, datetime | None]:
    notification_url, lifecycle_url = _notification_urls(settings, provider)
    if subscription is not None:
        try:
            reg = adapter.renew_notification_registration(
                credentials,
                subscription,
                notification_url=notification_url,
                lifecycle_url=lifecycle_url,
                gmail_topic=settings.gmail_topic,
            )
        except ProviderNotFoundError:
            reg = adapter.create_notification_registration(
                credentials,
                notification_url=notification_url,
                lifecycle_url=lifecycle_url,
                gmail_topic=settings.gmail_topic,
            )
    else:
        reg = adapter.create_notification_registration(
            credentials,
            notification_url=notification_url,
            lifecycle_url=lifecycle_url,
            gmail_topic=settings.gmail_topic,
        )
    provider_subscription_id = (
        reg.provider_subscription_id or f"{provider}:{connection_id}"
    )
    client_state_hash = reg.client_state_hash or (
        subscription.client_state_hash if subscription is not None else ""
    )
    return (
        provider_subscription_id,
        reg.cursor,
        client_state_hash,
        reg.expires_at,
    )


def ensure_subscription(
    household_id: str,
    connection_id: str,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    providers: Mapping[Provider, MailProvider],
    settings: Settings,
) -> WebhookSubscription:
    conn = store.get_connection(household_id, connection_id)
    if conn.status != "connected":
        raise ConflictError("Notifications can only be enabled on a connected account.")
    provider = conn.provider
    adapter = providers.get(provider)
    if adapter is None:
        raise ProviderNotConfiguredError(
            f"{provider} OAuth is not configured on this API."
        )
    _notification_urls(settings, provider)
    credentials = _fresh_credentials(connection_id, adapter, vault)
    existing = store.get_active_subscription_for_connection(household_id, connection_id)
    threshold = (
        GMAIL_EXPIRING_WITHIN if provider == "gmail" else OUTLOOK_EXPIRING_WITHIN
    )
    if (
        existing is not None
        and existing.expires_at is not None
        and existing.expires_at > datetime.now(UTC) + threshold
    ):
        return existing
    subscription = store.get_subscription_for_connection(household_id, connection_id)
    provider_id, cursor, state_hash, expires_at = _register(
        subscription,
        adapter=adapter,
        credentials=credentials,
        connection_id=connection_id,
        settings=settings,
        provider=provider,
    )
    return store.upsert_webhook_subscription(
        household_id,
        connection_id,
        provider=provider,
        provider_subscription_id=provider_id,
        client_state_hash=state_hash,
        cursor=cursor,
        expires_at=expires_at,
    )


def _renew_one(
    subscription: WebhookSubscription,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    providers: Mapping[Provider, MailProvider],
    settings: Settings,
) -> None:
    provider = subscription.provider
    adapter = providers.get(provider)
    if adapter is None:
        raise ProviderNotConfiguredError(
            f"{provider} OAuth is not configured on this API."
        )
    credentials = _fresh_credentials(subscription.connection_id, adapter, vault)
    provider_id, cursor, state_hash, expires_at = _register(
        subscription,
        adapter=adapter,
        credentials=credentials,
        connection_id=subscription.connection_id,
        settings=settings,
        provider=provider,
    )
    store.upsert_webhook_subscription(
        subscription.household_id,
        subscription.connection_id,
        provider=provider,
        provider_subscription_id=provider_id,
        client_state_hash=state_hash,
        cursor=cursor,
        expires_at=expires_at,
    )


def renew_due_subscriptions(
    now: datetime,
    *,
    store: SchoolSiftStore,
    vault: CredentialVault,
    providers: Mapping[Provider, MailProvider],
    settings: Settings,
) -> RenewalResult:
    due = store.list_subscriptions_due(
        gmail_expiring_before=now + GMAIL_EXPIRING_WITHIN,
        gmail_stale_before=now - GMAIL_RENEW_AFTER,
        outlook_expiring_before=now + OUTLOOK_EXPIRING_WITHIN,
    )
    renewed = 0
    failed = 0
    for subscription in due:
        try:
            _renew_one(
                subscription,
                store=store,
                vault=vault,
                providers=providers,
                settings=settings,
            )
            renewed += 1
        except ProviderAuthError:
            failed += 1
            with contextlib.suppress(Exception):
                store.update_webhook_subscription(
                    subscription.household_id,
                    subscription.id,
                    status="reauthorization_required",
                )
                store.set_connection_status(
                    subscription.household_id,
                    subscription.connection_id,
                    "reauthorization_required",
                )
        except Exception:
            failed += 1
            with contextlib.suppress(Exception):
                store.update_webhook_subscription(
                    subscription.household_id,
                    subscription.id,
                    status="renewal_due",
                )
    return RenewalResult(checked=len(due), renewed=renewed, failed=failed)
