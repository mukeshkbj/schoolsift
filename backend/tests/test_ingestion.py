from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from schoolsift.credentials import OAuthCredentials
from schoolsift.models import MessagePage
from schoolsift.notifications import (
    IngestionEvent,
    process_ingestion_event,
)
from schoolsift.sqlite_store import SQLiteStore


def make_event(
    event_id: str = "evt-1",
    *,
    kind: str = "mail_changed",
    household_id: str = "hh",
    connection_id: str = "conn",
    cursor: str | None = "12345",
) -> IngestionEvent:
    return IngestionEvent(
        id=event_id,
        provider="gmail",
        household_id=household_id,
        connection_id=connection_id,
        kind=kind,
        provider_cursor=cursor,
        received_at=datetime.now(UTC),
    )


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(tmp_path / "db.sqlite")
    s.initialize()
    return s


def test_enqueue_dedupes_by_key(store):
    assert store.enqueue_ingestion_event(make_event(), dedupe_key="k1") is True
    assert store.enqueue_ingestion_event(make_event("evt-2"), dedupe_key="k1") is False
    assert store.enqueue_ingestion_event(make_event("evt-3"), dedupe_key="k2") is True


def test_concurrent_enqueue_single_winner(store):
    barrier = threading.Barrier(4)
    results: list[bool] = []

    def attempt() -> None:
        barrier.wait(timeout=10)
        results.append(store.enqueue_ingestion_event(make_event(), dedupe_key="race"))

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert results.count(True) == 1
    assert results.count(False) == 3


def test_claim_leases_and_increments_attempts(store):
    store.enqueue_ingestion_event(make_event(), dedupe_key="k1")
    now = datetime.now(UTC)
    claimed = store.claim_ingestion_events(10, now, lease_seconds=60)
    assert [e.id for e in claimed] == ["evt-1"]
    assert claimed[0].provider_cursor == "12345"

    assert store.claim_ingestion_events(10, now, lease_seconds=60) == []

    later = now + timedelta(seconds=61)
    reclaimed = store.claim_ingestion_events(10, later, lease_seconds=60)
    assert [e.id for e in reclaimed] == ["evt-1"]

    con = sqlite3.connect(store.path)
    try:
        row = con.execute(
            "SELECT status, attempts FROM ingestion_events WHERE id = 'evt-1'"
        ).fetchone()
        assert row == ("processing", 2)
    finally:
        con.close()


def test_complete_marks_done(store):
    store.enqueue_ingestion_event(make_event(), dedupe_key="k1")
    store.claim_ingestion_events(10, datetime.now(UTC), lease_seconds=60)
    store.complete_ingestion_event("evt-1")
    later = datetime.now(UTC) + timedelta(hours=1)
    assert store.claim_ingestion_events(10, later, lease_seconds=60) == []


def test_fail_retries_then_terminates(store):
    store.enqueue_ingestion_event(make_event(), dedupe_key="k1")
    now = datetime.now(UTC)
    for _ in range(4):
        store.claim_ingestion_events(1, now, lease_seconds=0)
        store.fail_ingestion_event("evt-1", now, max_attempts=5)
        con = sqlite3.connect(store.path)
        row = con.execute(
            "SELECT status FROM ingestion_events WHERE id = 'evt-1'"
        ).fetchone()
        con.close()
        assert row[0] == "pending"

    store.claim_ingestion_events(1, now, lease_seconds=0)
    store.fail_ingestion_event("evt-1", now, max_attempts=5)
    con = sqlite3.connect(store.path)
    row = con.execute(
        "SELECT status, attempts FROM ingestion_events WHERE id = 'evt-1'"
    ).fetchone()
    con.close()
    assert row == ("failed", 5)
    assert (
        store.claim_ingestion_events(10, now + timedelta(days=1), lease_seconds=60)
        == []
    )


def test_queue_persists_across_instances(tmp_path):
    path = tmp_path / "db.sqlite"
    s1 = SQLiteStore(path)
    s1.initialize()
    s1.enqueue_ingestion_event(make_event(), dedupe_key="k1")
    s2 = SQLiteStore(path)
    claimed = s2.claim_ingestion_events(10, datetime.now(UTC), lease_seconds=60)
    assert [e.id for e in claimed] == ["evt-1"]


def test_event_rows_do_not_store_raw_bodies(store):
    store.enqueue_ingestion_event(make_event(), dedupe_key="k1")
    con = sqlite3.connect(store.path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(ingestion_events)")}
        assert "payload" in cols and "raw" not in " ".join(cols).lower()
    finally:
        con.close()


class _FakeVault:
    def __init__(self) -> None:
        from pydantic import SecretStr

        self.creds = OAuthCredentials(
            access_token=SecretStr("tok"),
            refresh_token=SecretStr("rt"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=(),
        )

    def get(self, connection_id: str) -> OAuthCredentials:
        return self.creds

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        self.creds = credentials

    def delete(self, connection_id: str) -> None:
        pass


class _FakeProvider:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_message_headers(self, credentials, *, cursor):
        self.list_calls += 1
        return MessagePage(messages=[], next_cursor=None, has_more=False)

    def fetch_message(self, credentials, provider_message_id):
        raise AssertionError("no fetch expected")

    def refresh(self, credentials):
        return credentials

    def authorization_url(self, *, state, redirect_uri):
        raise AssertionError

    def exchange_code(self, *, code, redirect_uri):
        raise AssertionError

    def identity(self, credentials):
        raise AssertionError


class _CursorProvider(_FakeProvider):
    def __init__(self, pages: list[MessagePage]) -> None:
        super().__init__()
        self.pages = list(pages)
        self.cursors: list[str | None] = []

    def list_message_headers(self, credentials, *, cursor):
        self.cursors.append(cursor)
        return (
            self.pages.pop(0)
            if self.pages
            else MessagePage(messages=[], next_cursor=None, has_more=False)
        )


class _FakeContent:
    def put(self, household_id: str, content: bytes) -> str:
        return "ref"

    def get(self, household_id: str, ref: str) -> bytes:
        raise KeyError(ref)

    def delete(self, household_id: str, ref: str) -> None:
        pass


def _seed_connection(store: SQLiteStore):
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="a@x.com"
    )
    return h, store.set_connection_status(h.id, conn.id, "connected")


def test_process_mail_changed_invokes_sync(store):
    h, conn = _seed_connection(store)
    provider = _FakeProvider()
    process_ingestion_event(
        make_event(household_id=h.id, connection_id=conn.id),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={"gmail": provider},
    )
    assert provider.list_calls == 1
    assert store.get_connection(h.id, conn.id).sync_status == "ready"


def test_mail_changed_uses_subscription_cursor_not_webhook_high_water(store):
    h, conn = _seed_connection(store)
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:100",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    provider = _CursorProvider(
        [MessagePage(messages=[], next_cursor="history:140", has_more=False)]
    )
    process_ingestion_event(
        make_event(household_id=h.id, connection_id=conn.id, cursor="history:999"),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={"gmail": provider},
    )
    assert provider.cursors == ["history:100"]
    sub = store.get_subscription_for_connection(h.id, conn.id)
    assert sub is not None and sub.cursor == "history:140"


def test_mail_changed_without_subscription_uses_connection_cursor(store):
    h, conn = _seed_connection(store)
    store.update_connection_sync(
        h.id, conn.id, sync_status="ready", sync_cursor="history:55"
    )
    provider = _CursorProvider([])
    process_ingestion_event(
        make_event(household_id=h.id, connection_id=conn.id, cursor="history:999"),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={"gmail": provider},
    )
    assert provider.cursors == ["history:55"]


def test_full_resync_retries_once_and_records_checkpoint(store):
    from schoolsift.errors import FullResyncRequiredError

    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="outlook", provider_subject="s", email="a@x.com"
    )
    conn = store.set_connection_status(h.id, conn.id, "connected")
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="outlook",
        provider_subscription_id="osub-1",
        client_state_hash=hashlib.sha256(b"s").hexdigest(),
        cursor="https://graph.microsoft.com/v1.0/delta?token=old",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class _ResyncProvider(_CursorProvider):
        def list_message_headers(self, credentials, *, cursor):
            self.cursors.append(cursor)
            if cursor is not None and "token=old" in cursor:
                raise FullResyncRequiredError("gone")
            return MessagePage(
                messages=[],
                next_cursor="https://graph.microsoft.com/v1.0/delta?token=new",
                has_more=False,
            )

    provider = _ResyncProvider([])
    process_ingestion_event(
        make_event(
            household_id=h.id,
            connection_id=conn.id,
            cursor="outlook-resource-id",
        ),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={"outlook": provider},
    )
    assert provider.cursors == [
        "https://graph.microsoft.com/v1.0/delta?token=old",
        None,
    ]
    sub = store.get_subscription_for_connection(h.id, conn.id)
    assert sub is not None
    assert sub.cursor == "https://graph.microsoft.com/v1.0/delta?token=new"


def test_gmail_resync_without_checkpoint_marks_subscription_renewal_due(store):
    from schoolsift.errors import FullResyncRequiredError

    h, conn = _seed_connection(store)
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:100",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )

    class _StaleHistory(_CursorProvider):
        def list_message_headers(self, credentials, *, cursor):
            self.cursors.append(cursor)
            if cursor == "history:100":
                raise FullResyncRequiredError("too old")
            return MessagePage(messages=[], next_cursor=None, has_more=False)

    provider = _StaleHistory([])
    process_ingestion_event(
        make_event(household_id=h.id, connection_id=conn.id),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={"gmail": provider},
    )
    assert provider.cursors == ["history:100", None]
    sub = store.get_subscription_for_connection(h.id, conn.id)
    assert sub is not None
    assert sub.status == "renewal_due"
    assert sub.cursor is None


def test_missed_event_resets_cursor_and_full_resyncs(store):
    h, conn = _seed_connection(store)
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:100",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    provider = _CursorProvider(
        [
            MessagePage(messages=[], next_cursor="page:tok", has_more=True),
            MessagePage(messages=[], next_cursor=None, has_more=False),
        ]
    )
    process_ingestion_event(
        make_event(kind="missed", household_id=h.id, connection_id=conn.id),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={"gmail": provider},
    )
    assert provider.cursors[0] is None
    sub = store.get_subscription_for_connection(h.id, conn.id)
    assert sub is not None and sub.status == "renewal_due"


def test_process_reauthorization_marks_connection(store):
    h, conn = _seed_connection(store)
    process_ingestion_event(
        make_event(
            kind="reauthorization_required", household_id=h.id, connection_id=conn.id
        ),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={},
    )
    updated = store.get_connection(h.id, conn.id)
    assert updated.status == "reauthorization_required"
    assert updated.sync_status == "reauthorization_required"


def test_process_subscription_removed_marks_renewal(store):
    h, conn = _seed_connection(store)
    sub = store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="outlook",
        provider_subscription_id="psub-1",
        client_state_hash=hashlib.sha256(b"state").hexdigest(),
    )
    process_ingestion_event(
        make_event(
            kind="subscription_removed", household_id=h.id, connection_id=conn.id
        ),
        store=store,
        vault=_FakeVault(),
        content=_FakeContent(),
        providers={},
    )
    updated = store.get_webhook_subscription("outlook", "psub-1")
    assert updated is not None and updated.id == sub.id
    assert updated.status == "renewal_due"
    assert store.get_connection(h.id, conn.id).status == "connected"


def test_subscription_store_roundtrip(store):
    h, conn = _seed_connection(store)
    sub = store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="g-1",
        client_state_hash=hashlib.sha256(b"x").hexdigest(),
        cursor="cur-1",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert sub.status == "active"
    found = store.get_webhook_subscription("gmail", "g-1")
    assert found is not None and found.id == sub.id
    assert store.get_active_subscription_for_connection(h.id, conn.id) is not None
    assert store.get_webhook_subscription("gmail", "nope") is None
    renewed = store.set_webhook_subscription_status(h.id, sub.id, "expired")
    assert renewed.status == "expired"
    assert store.get_active_subscription_for_connection(h.id, conn.id) is None
    from schoolsift.errors import NotFoundError

    with pytest.raises(NotFoundError):
        store.set_webhook_subscription_status("hh-other", sub.id, "active")
