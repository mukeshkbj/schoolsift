from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_providers import creds, gmail_client, outlook_client

from schoolsift.api import create_app
from schoolsift.config import Settings
from schoolsift.credentials import OAuthCredentials
from schoolsift.errors import (
    ConflictError,
    ProviderAuthError,
    ProviderError,
    WebhookNotConfiguredError,
)
from schoolsift.models import WebhookSubscription
from schoolsift.sqlite_store import SQLiteStore
from schoolsift.subscription_service import (
    ensure_subscription,
    renew_due_subscriptions,
)


def test_gmail_watch_posts_topic_and_returns_history_cursor():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"historyId": "777", "expiration": "1893456000000"}
        )

    p = gmail_client(handler)
    reg = p.create_notification_registration(
        creds(),
        notification_url="https://api.example/v1/webhooks/gmail",
        lifecycle_url="https://api.example/v1/webhooks/gmail",
        gmail_topic="projects/p/topics/t",
    )
    assert requests[0].url.path.endswith("/users/me/watch")
    body = json.loads(requests[0].content)
    assert body == {
        "topicName": "projects/p/topics/t",
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "include",
    }
    assert reg.cursor == "history:777"
    assert reg.expires_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert reg.provider_subscription_id == ""


def test_gmail_watch_requires_topic():
    p = gmail_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        p.create_notification_registration(
            creds(),
            notification_url="https://api.example/v1/webhooks/gmail",
            lifecycle_url="https://api.example/v1/webhooks/gmail",
            gmail_topic=None,
        )


def test_outlook_create_subscription_shape_and_hash():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "sub-graph-1",
                "expirationDateTime": "2030-01-03T23:00:00Z",
            },
        )

    p = outlook_client(handler)
    reg = p.create_notification_registration(
        creds(),
        notification_url="https://api.example/v1/webhooks/outlook",
        lifecycle_url="https://api.example/v1/webhooks/outlook",
        gmail_topic=None,
    )
    body = json.loads(requests[0].content)
    assert body["changeType"] == "created"
    assert body["resource"] == "/me/mailFolders('inbox')/messages"
    assert body["notificationUrl"] == "https://api.example/v1/webhooks/outlook"
    assert body["lifecycleNotificationUrl"] == "https://api.example/v1/webhooks/outlook"
    sent_state = body["clientState"]
    assert len(sent_state) >= 32
    assert reg.client_state_hash == hashlib.sha256(sent_state.encode()).hexdigest()
    assert sent_state not in reg.model_dump_json()
    expiry = datetime.fromisoformat(body["expirationDateTime"])
    assert expiry - datetime.now(UTC) <= timedelta(days=2, hours=23)
    assert reg.provider_subscription_id == "sub-graph-1"
    assert reg.expires_at == datetime(2030, 1, 3, 23, 0, tzinfo=UTC)


def test_outlook_rejects_http_urls():
    p = outlook_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        p.create_notification_registration(
            creds(),
            notification_url="http://api.example/insecure",
            lifecycle_url="https://api.example/v1/webhooks/outlook",
            gmail_topic=None,
        )


def _existing_outlook_sub() -> WebhookSubscription:
    return WebhookSubscription(
        id="sub-1",
        household_id="hh",
        connection_id="conn",
        provider="outlook",
        provider_subscription_id="sub-graph-1",
        client_state_hash=hashlib.sha256(b"keep").hexdigest(),
        cursor="delta:x",
        expires_at=datetime.now(UTC) + timedelta(hours=6),
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_outlook_renew_patches_expiration_only():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "sub-graph-1",
                "expirationDateTime": "2030-02-01T00:00:00Z",
            },
        )

    p = outlook_client(handler)
    existing = _existing_outlook_sub()
    reg = p.renew_notification_registration(
        creds(),
        existing,
        notification_url="https://api.example/v1/webhooks/outlook",
        lifecycle_url="https://api.example/v1/webhooks/outlook",
        gmail_topic=None,
    )
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1.0/subscriptions/sub-graph-1"
    assert set(json.loads(requests[0].content)) == {"expirationDateTime"}
    assert reg.client_state_hash == existing.client_state_hash
    assert reg.expires_at == datetime(2030, 2, 1, tzinfo=UTC)


def test_outlook_renew_rejects_unsafe_subscription_id():
    p = outlook_client(lambda r: httpx.Response(200, json={}))
    bad = _existing_outlook_sub().model_copy(
        update={"provider_subscription_id": "sub/../../admin"}
    )
    with pytest.raises(ProviderError):
        p.renew_notification_registration(
            creds(),
            bad,
            notification_url="https://api.example/v1/webhooks/outlook",
            lifecycle_url="https://api.example/v1/webhooks/outlook",
            gmail_topic=None,
        )


def test_outlook_auth_error_maps_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "InvalidToken"}})

    p = outlook_client(handler)
    with pytest.raises(ProviderAuthError):
        p.create_notification_registration(
            creds(),
            notification_url="https://api.example/v1/webhooks/outlook",
            lifecycle_url="https://api.example/v1/webhooks/outlook",
            gmail_topic=None,
        )


class _Vault:
    def __init__(self) -> None:
        self.data: dict[str, OAuthCredentials] = {}

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        self.data[connection_id] = credentials

    def get(self, connection_id: str) -> OAuthCredentials:
        return self.data[connection_id]

    def delete(self, connection_id: str) -> None:
        self.data.pop(connection_id, None)


@pytest.fixture()
def env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    vault = _Vault()
    vault.put(conn.id, creds())
    return store, h, conn, vault


def _settings(**overrides) -> Settings:
    base = {
        "public_api_url": "https://api.example",
        "gmail_topic": "projects/p/topics/t",
    }
    return Settings(**{**base, **overrides})


def _gmail_provider(watch_body=None, watch_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/watch"):
            return httpx.Response(
                watch_status,
                json=watch_body or {"historyId": "900", "expiration": "1893456000000"},
            )
        return httpx.Response(200, json={"messages": [], "historyId": "1"})

    return gmail_client(handler)


def test_ensure_subscription_creates_gmail_watch(env):
    store, h, conn, vault = env
    provider = _gmail_provider()
    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"gmail": provider},
        settings=_settings(),
    )
    assert sub.status == "active"
    assert sub.provider_subscription_id == f"gmail:{conn.id}"
    assert sub.cursor == "history:900"
    assert sub.expires_at == datetime(2030, 1, 1, tzinfo=UTC)


def test_ensure_subscription_requires_https_and_topic(env):
    store, h, conn, vault = env
    provider = _gmail_provider()
    with pytest.raises(WebhookNotConfiguredError):
        ensure_subscription(
            h.id,
            conn.id,
            store=store,
            vault=vault,
            providers={"gmail": provider},
            settings=Settings(public_api_url="http://127.0.0.1:8000"),
        )
    with pytest.raises(WebhookNotConfiguredError):
        ensure_subscription(
            h.id,
            conn.id,
            store=store,
            vault=vault,
            providers={"gmail": provider},
            settings=_settings(gmail_topic=None),
        )
    assert store.get_subscription_for_connection(h.id, conn.id) is None


def test_ensure_subscription_requires_connected(env):
    store, h, conn, vault = env
    store.set_connection_status(h.id, conn.id, "disconnected")
    with pytest.raises(ConflictError):
        ensure_subscription(
            h.id,
            conn.id,
            store=store,
            vault=vault,
            providers={"gmail": _gmail_provider()},
            settings=_settings(),
        )


def test_ensure_subscription_returns_fresh_existing_without_call(env):
    store, h, conn, vault = env
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, json={"historyId": "1", "expiration": "1893456000000"}
        )

    provider = gmail_client(handler)
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"gmail": provider},
        settings=_settings(),
    )
    assert calls == []
    assert sub.cursor == "history:5"


def test_outlook_ensure_stores_hash_not_secret(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="outlook", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    vault = _Vault()
    vault.put(conn.id, creds())
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "graph-sub-9",
                "expirationDateTime": "2030-01-03T23:00:00Z",
            },
        )

    provider = outlook_client(handler)
    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"outlook": provider},
        settings=_settings(),
    )
    raw_state = sent[0]["clientState"]
    assert sub.provider_subscription_id == "graph-sub-9"
    assert sub.client_state_hash == hashlib.sha256(raw_state.encode()).hexdigest()
    assert raw_state not in sub.client_state_hash


def test_renew_due_no_call_when_not_due(env):
    store, h, conn, vault = env
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    result = renew_due_subscriptions(
        datetime.now(UTC),
        store=store,
        vault=vault,
        providers={"gmail": gmail_client(handler)},
        settings=_settings(),
    )
    assert result.checked == 0 and result.renewed == 0 and result.failed == 0
    assert calls == []


def test_renew_due_gmail_expiring_soon(env):
    store, h, conn, vault = env
    sub = store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    result = renew_due_subscriptions(
        datetime.now(UTC),
        store=store,
        vault=vault,
        providers={"gmail": _gmail_provider()},
        settings=_settings(),
    )
    assert (result.checked, result.renewed, result.failed) == (1, 1, 0)
    updated = store.get_subscription_for_connection(h.id, conn.id)
    assert updated is not None and updated.id == sub.id
    assert updated.cursor == "history:900"
    assert updated.status == "active"


def test_renew_due_outlook_404_creates_replacement(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="outlook", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    vault = _Vault()
    vault.put(conn.id, creds())
    old = store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="outlook",
        provider_subscription_id="dead-sub",
        client_state_hash="h",
        cursor="https://graph.microsoft.com/v1.0/delta?t=1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return httpx.Response(
            200,
            json={
                "id": "new-sub-1",
                "expirationDateTime": "2030-02-01T00:00:00Z",
            },
        )

    result = renew_due_subscriptions(
        datetime.now(UTC),
        store=store,
        vault=vault,
        providers={"outlook": outlook_client(handler)},
        settings=_settings(),
    )
    assert (result.checked, result.renewed, result.failed) == (1, 1, 0)
    assert ("PATCH", "/v1.0/subscriptions/dead-sub") in calls
    assert ("POST", "/v1.0/subscriptions") in calls
    updated = store.get_subscription_for_connection(h.id, conn.id)
    assert updated is not None and updated.id == old.id
    assert updated.provider_subscription_id == "new-sub-1"
    assert updated.status == "active"


def test_renew_auth_failure_marks_reauthorization(env):
    store, h, conn, vault = env
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {}})

    result = renew_due_subscriptions(
        datetime.now(UTC),
        store=store,
        vault=vault,
        providers={"gmail": gmail_client(handler)},
        settings=_settings(),
    )
    assert result.failed == 1
    sub = store.get_subscription_for_connection(h.id, conn.id)
    assert sub is not None
    assert sub.status == "reauthorization_required"
    assert store.get_connection(h.id, conn.id).status == "reauthorization_required"


def test_renew_provider_failure_marks_renewal_due(env):
    store, h, conn, vault = env
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {}})

    result = renew_due_subscriptions(
        datetime.now(UTC),
        store=store,
        vault=vault,
        providers={"gmail": gmail_client(handler)},
        settings=_settings(),
    )
    assert result.failed == 1
    sub = store.get_subscription_for_connection(h.id, conn.id)
    assert sub is not None and sub.status == "renewal_due"
    assert store.get_connection(h.id, conn.id).status == "connected"


def test_renewal_failure_isolates_other_subscriptions(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    c1 = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s1", email="a@x.com"
    )
    c2 = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s2", email="b@x.com"
    )
    store.set_connection_status(h.id, c1.id, "connected")
    store.set_connection_status(h.id, c2.id, "connected")
    vault = _Vault()
    vault.put(c1.id, creds())
    vault.put(c2.id, creds())
    soon = datetime.now(UTC) + timedelta(hours=1)
    store.upsert_webhook_subscription(
        h.id,
        c1.id,
        provider="gmail",
        provider_subscription_id="gmail:c1",
        client_state_hash="",
        cursor="history:1",
        expires_at=soon,
    )
    store.upsert_webhook_subscription(
        h.id,
        c2.id,
        provider="gmail",
        provider_subscription_id="gmail:c2",
        client_state_hash="",
        cursor="history:2",
        expires_at=soon,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "unstable" in str(request.headers.get("x-test", "")):
            return httpx.Response(500, json={})
        return httpx.Response(
            200, json={"historyId": "999", "expiration": "1893456000000"}
        )

    provider = gmail_client(handler)
    original = provider.create_notification_registration

    state = {"calls": 0}

    def flaky(credentials, *args, **kw):
        state["calls"] += 1
        if state["calls"] == 1:
            raise ProviderError("boom")
        return original(credentials, **kw)

    provider.create_notification_registration = flaky  # type: ignore[method-assign]
    provider.renew_notification_registration = flaky  # type: ignore[method-assign]
    result = renew_due_subscriptions(
        datetime.now(UTC),
        store=store,
        vault=vault,
        providers={"gmail": provider},
        settings=_settings(),
    )
    assert (result.checked, result.renewed, result.failed) == (2, 1, 1)


def test_notifications_endpoint_local_returns_503(tmp_path):
    settings = Settings(database_path=tmp_path / "api.db")
    store = SQLiteStore(settings.database_path)
    store.initialize()
    client = TestClient(create_app(settings=settings, store=store))
    store.create_household(name="H", timezone="UTC")
    r = client.post("/v1/connections/conn-1/notifications")
    assert r.status_code in (404, 503)


def test_notifications_endpoint_returns_public_fields_only(tmp_path):
    settings = Settings(
        database_path=tmp_path / "api.db",
        public_api_url="https://api.example",
        gmail_topic="projects/p/topics/t",
    )
    store = SQLiteStore(settings.database_path)
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    vault = _Vault()
    vault.put(conn.id, creds())
    client = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": _gmail_provider()},
            vault=vault,
        )
    )
    r = client.post(
        f"/v1/connections/{conn.id}/notifications",
        headers={"X-SchoolSift-Household": h.id},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"provider", "status", "expires_at"}
    assert body["status"] == "active"
    assert "gmail:" not in json.dumps(body)
    assert "history:" not in json.dumps(body)


def test_notifications_endpoint_requires_membership(tmp_path):
    settings = Settings(
        database_path=tmp_path / "api.db",
        public_api_url="https://api.example",
        gmail_topic="projects/p/topics/t",
    )
    store = SQLiteStore(settings.database_path)
    store.initialize()
    from schoolsift.identity import LOCAL_PRINCIPAL

    h = store.create_household(name="H", timezone="UTC")
    other = store.create_household_for_owner(
        LOCAL_PRINCIPAL, name="Other", timezone="UTC"
    )
    conn = store.upsert_connection(
        other.id, provider="gmail", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(other.id, conn.id, "connected")
    client = TestClient(create_app(settings=settings, store=store))
    r = client.post(
        f"/v1/connections/{conn.id}/notifications",
        headers={"X-SchoolSift-Household": h.id},
    )
    assert r.status_code in (403, 404)


def test_ensure_subscription_gmail_expiring_within_24h_renews(env):
    store, h, conn, vault = env
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(hours=20),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, json={"historyId": "901", "expiration": "1893456000000"}
        )

    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"gmail": gmail_client(handler)},
        settings=_settings(),
    )
    assert calls and calls[0].endswith("/watch")
    assert sub.cursor == "history:901"


def test_ensure_subscription_gmail_beyond_24h_returns_existing(env):
    store, h, conn, vault = env
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(hours=30),
    )
    calls: list[str] = []
    provider = gmail_client(
        lambda r: calls.append(str(r.url)) or httpx.Response(200, json={})
    )
    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"gmail": provider},
        settings=_settings(),
    )
    assert calls == []
    assert sub.cursor == "history:5"


def _outlook_env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="outlook", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    vault = _Vault()
    vault.put(conn.id, creds())
    return store, h, conn, vault


def test_ensure_subscription_outlook_within_12h_renews(tmp_path):
    store, h, conn, vault = _outlook_env(tmp_path)
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="outlook",
        provider_subscription_id="sub-old",
        client_state_hash="h",
        cursor="https://graph.microsoft.com/v1.0/delta?t=1",
        expires_at=datetime.now(UTC) + timedelta(hours=10),
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "id": "sub-old",
                "expirationDateTime": "2030-02-01T00:00:00Z",
            },
        )

    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"outlook": outlook_client(handler)},
        settings=_settings(),
    )
    assert calls == [("PATCH", "/v1.0/subscriptions/sub-old")]
    assert sub.expires_at == datetime(2030, 2, 1, tzinfo=UTC)


def test_ensure_subscription_outlook_beyond_12h_returns_existing(tmp_path):
    store, h, conn, vault = _outlook_env(tmp_path)
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="outlook",
        provider_subscription_id="sub-old",
        client_state_hash="h",
        cursor="https://graph.microsoft.com/v1.0/delta?t=1",
        expires_at=datetime.now(UTC) + timedelta(hours=20),
    )
    calls: list[str] = []
    provider = outlook_client(
        lambda r: calls.append(str(r.url)) or httpx.Response(200, json={})
    )
    sub = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"outlook": provider},
        settings=_settings(),
    )
    assert calls == []
    assert sub.provider_subscription_id == "sub-old"


def test_ensure_subscription_non_active_always_attempts_renewal(env):
    store, h, conn, vault = env
    sub = store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(days=5),
    )
    store.set_webhook_subscription_status(h.id, sub.id, "renewal_due")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, json={"historyId": "902", "expiration": "1893456000000"}
        )

    renewed = ensure_subscription(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        providers={"gmail": gmail_client(handler)},
        settings=_settings(),
    )
    assert calls and calls[0].endswith("/watch")
    assert renewed.status == "active"
    assert renewed.cursor == "history:902"
