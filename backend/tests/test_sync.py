from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from schoolsift.content import LocalEncryptedContentStore
from schoolsift.credentials import OAuthCredentials
from schoolsift.errors import (
    ConflictError,
    ProviderAuthError,
    ProviderError,
)
from schoolsift.models import (
    AttachmentContent,
    FetchedMessage,
    MessageHeader,
    MessagePage,
)
from schoolsift.sqlite_store import SQLiteStore
from schoolsift.sync import sync_connection


class InMemoryVault:
    def __init__(self) -> None:
        self.data: dict[str, OAuthCredentials] = {}

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        self.data[connection_id] = credentials

    def get(self, connection_id: str) -> OAuthCredentials:
        return self.data[connection_id]

    def delete(self, connection_id: str) -> None:
        self.data.pop(connection_id, None)


class FakeMail:
    def __init__(
        self,
        pages: list[MessagePage],
        fetched: dict[str, FetchedMessage],
        *,
        refreshed: OAuthCredentials | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.pages = list(pages)
        self.fetched = fetched
        self.refreshed = refreshed
        self.fail = fail
        self.fetch_fail: Exception | None = None
        self.fetch_calls: list[str] = []
        self.refresh_calls = 0

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return "https://x/auth"

    def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthCredentials:
        raise NotImplementedError

    def identity(self, credentials: OAuthCredentials):
        raise NotImplementedError

    def refresh(self, credentials: OAuthCredentials) -> OAuthCredentials:
        self.refresh_calls += 1
        if self.fail:
            raise self.fail
        return self.refreshed or credentials

    def list_message_headers(
        self, credentials: OAuthCredentials, *, cursor: str | None
    ) -> MessagePage:
        if self.fail:
            raise self.fail
        return (
            self.pages.pop(0)
            if self.pages
            else MessagePage(messages=[], next_cursor=None, has_more=False)
        )

    def fetch_message(
        self, credentials: OAuthCredentials, provider_message_id: str
    ) -> FetchedMessage:
        if self.fetch_fail:
            raise self.fetch_fail
        self.fetch_calls.append(provider_message_id)
        return self.fetched[provider_message_id]


def header(pmid: str, sender: str) -> MessageHeader:
    return MessageHeader(
        provider_message_id=pmid,
        thread_id=f"t-{pmid}",
        sender_name=sender,
        sender_email=sender,
        reply_to=sender,
        subject=f"Subject {pmid}",
        received_at=datetime(2026, 9, 10, tzinfo=UTC),
    )


def fetched_for(pmid: str, sender: str) -> FetchedMessage:
    return FetchedMessage(
        **header(pmid, sender).model_dump(),
        body_text=f"body of {pmid}",
        attachments=[
            AttachmentContent(
                provider_attachment_id=f"{pmid}:0",
                name="note.txt",
                mime="text/plain",
                content=f"file {pmid}".encode(),
            )
        ],
    )


def creds(*, expires_in: int = 3600) -> OAuthCredentials:
    return OAuthCredentials(
        access_token=SecretStr("at"),
        refresh_token=SecretStr("rt"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=("openid",),
    )


@pytest.fixture()
def env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s1", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    vault = InMemoryVault()
    vault.put(conn.id, creds())
    content = LocalEncryptedContentStore(
        tmp_path / "content", key=Fernet.generate_key()
    )
    return store, h, conn, vault, content


def run(env, mail, **kw):
    store, h, conn, vault, content = env
    return sync_connection(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        content=content,
        providers={"gmail": mail},
        **kw,
    )


def test_suggested_senders_never_fetch_bodies(env):
    store, h, conn, _, _ = env
    mail = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            )
        ],
        {},
    )
    result = run(env, mail)
    assert result.discovered == 1
    assert result.imported == 0
    assert result.awaiting_source_confirmation == 1
    assert mail.fetch_calls == []
    msgs = store.list_messages(h.id)
    assert msgs[0].status == "awaiting_source"
    assert msgs[0].body_ref is None
    assert store.list_sources(h.id)[0].status == "suggested"
    assert store.get_connection(h.id, conn.id).sync_status == "ready"


def test_confirmed_sender_imports_encrypted_content(env):
    store, h, _, _, content = env
    mail = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
        ],
        {"m1": fetched_for("m1", "school@x.org")},
    )
    run(env, mail)
    src = store.list_sources(h.id)[0]
    store.set_source_status(h.id, src.id, "confirmed")
    result = run(env, mail)
    assert result.imported == 1
    msg = store.list_messages(h.id)[0]
    assert msg.status == "awaiting_agent"
    assert msg.body_ref is not None
    assert content.get(h.id, msg.body_ref) == b"body of m1"
    con = sqlite3.connect(store.path)
    try:
        docs = con.execute(
            "SELECT name, content_ref FROM documents WHERE message_id = ?",
            (msg.id,),
        ).fetchall()
    finally:
        con.close()
    assert docs[0][0] == "note.txt"
    assert content.get(h.id, docs[0][1]) == b"file m1"
    assert b"body of m1" not in store.path.read_bytes()


def test_repeat_sync_dedupes(env):
    store, h, _, _, _ = env
    mail = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
        ],
        {"m1": fetched_for("m1", "school@x.org")},
    )
    run(env, mail)
    src = store.list_sources(h.id)[0]
    store.set_source_status(h.id, src.id, "confirmed")
    run(env, mail)
    third = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            )
        ],
        {"m1": fetched_for("m1", "school@x.org")},
    )
    result = run(env, third)
    assert result.imported == 0
    assert third.fetch_calls == []
    assert len(store.list_messages(h.id)) == 1
    assert len(store.list_sources(h.id)) == 1
    con = sqlite3.connect(store.path)
    try:
        n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_expiring_credentials_refresh_first(env):
    _, _, conn, vault, _ = env
    vault.put(conn.id, creds(expires_in=60))
    mail = FakeMail(
        [MessagePage(messages=[], next_cursor=None, has_more=False)],
        {},
        refreshed=creds(expires_in=7200),
    )
    mail.refreshed = OAuthCredentials(
        access_token=SecretStr("at2"),
        refresh_token=SecretStr("rt2"),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        scopes=("openid",),
    )
    run(env, mail)
    assert mail.refresh_calls == 1
    assert vault.get(conn.id).access_token.get_secret_value() == "at2"


def test_auth_failure_marks_reauthorization(env):
    store, h, conn, _, _ = env
    mail = FakeMail([], {}, fail=ProviderAuthError("denied"))
    with pytest.raises(ProviderAuthError):
        run(env, mail)
    conn2 = store.get_connection(h.id, conn.id)
    assert conn2.status == "reauthorization_required"
    assert conn2.sync_status == "reauthorization_required"


def test_missing_vault_entry_marks_reauthorization(env):
    store, h, conn, vault, _ = env
    vault.delete(conn.id)
    with pytest.raises(ProviderAuthError):
        run(env, FakeMail([], {}))
    assert store.get_connection(h.id, conn.id).status == "reauthorization_required"


def test_provider_error_marks_failed_and_sanitized(env):
    store, h, conn, _, _ = env
    mail = FakeMail([], {}, fail=RuntimeError("raw provider guts tok-secret"))
    with pytest.raises(ProviderError) as ei:
        run(env, mail)
    assert "tok-secret" not in ei.value.message
    assert store.get_connection(h.id, conn.id).sync_status == "failed"


def test_fetch_failure_marks_message_not_connection(env):
    store, h, conn, _, _ = env
    mail = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
        ],
        {},
    )
    run(env, mail)
    src = store.list_sources(h.id)[0]
    store.set_source_status(h.id, src.id, "confirmed")
    mail.fetch_fail = ProviderError("fetch broke")
    result = run(env, mail)
    assert result.imported == 0
    assert result.failed == 1
    msg = store.list_messages(h.id)[0]
    assert msg.status == "failed"
    assert msg.manual_review_reason == "fetch broke"
    assert store.get_connection(h.id, conn.id).sync_status == "ready"


def test_sync_status_is_syncing_during_sync(env):
    store, h, conn, _, _ = env
    seen: list[str] = []

    class ObservingMail(FakeMail):
        def list_message_headers(self, credentials, *, cursor):
            seen.append(store.get_connection(h.id, conn.id).sync_status)
            return super().list_message_headers(credentials, cursor=cursor)

    run(
        env,
        ObservingMail([MessagePage(messages=[], next_cursor=None, has_more=False)], {}),
    )
    assert seen == ["syncing"]
    assert store.get_connection(h.id, conn.id).sync_status == "ready"


class TrackingContent:
    def __init__(self, inner):
        self.inner = inner
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def put(self, household_id, data):
        ref = self.inner.put(household_id, data)
        self.puts.append(ref)
        return ref

    def get(self, household_id, ref):
        return self.inner.get(household_id, ref)

    def delete(self, household_id, ref):
        self.deletes.append(ref)
        self.inner.delete(household_id, ref)


def test_failed_import_leaves_no_orphan_refs(env):
    store, h, conn, vault, content = env
    tracked = TrackingContent(content)
    mail = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
        ],
        {"m1": fetched_for("m1", "school@x.org")},
    )
    sync_connection(
        h.id,
        conn.id,
        store=store,
        vault=vault,
        content=tracked,
        providers={"gmail": mail},
    )
    src = store.list_sources(h.id)[0]
    store.set_source_status(h.id, src.id, "confirmed")

    class FailSave:
        def __init__(self, inner):
            self._s = inner

        def __getattr__(self, name):
            return getattr(self._s, name)

        def save_fetched_message(self, *args, **kwargs):
            raise RuntimeError("db gone")

    result = sync_connection(
        h.id,
        conn.id,
        store=FailSave(store),
        vault=vault,
        content=tracked,
        providers={"gmail": mail},
    )
    assert result.failed == 1
    assert result.imported == 0
    assert tracked.puts
    assert sorted(tracked.deletes) == sorted(tracked.puts)
    for ref in tracked.puts:
        with pytest.raises(KeyError):
            content.get(h.id, ref)


def test_oversized_attachment_marks_failed_with_reason(env):
    store, h, _, _, _ = env
    big = FetchedMessage(
        **header("m1", "school@x.org").model_dump(),
        body_text="see attached",
        attachments=[
            AttachmentContent(
                provider_attachment_id="m1:0",
                name="huge.zip",
                mime="application/zip",
                content=b"x" * (25 * 1024 * 1024 + 1),
            )
        ],
    )
    mail = FakeMail(
        [
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
            MessagePage(
                messages=[header("m1", "school@x.org")],
                next_cursor=None,
                has_more=False,
            ),
        ],
        {"m1": big},
    )
    run(env, mail)
    src = store.list_sources(h.id)[0]
    store.set_source_status(h.id, src.id, "confirmed")
    result = run(env, mail)
    assert result.imported == 0
    assert result.failed == 1
    msg = store.list_messages(h.id)[0]
    assert msg.status == "failed"
    assert "huge.zip" in (msg.manual_review_reason or "")
    assert "25 MB" in (msg.manual_review_reason or "")
    con = sqlite3.connect(store.path)
    try:
        n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_not_connected_rejected(env):
    store, h, conn, _, _ = env
    store.set_connection_status(h.id, conn.id, "disconnected")
    with pytest.raises(ConflictError):
        run(env, FakeMail([], {}))


def test_pagination_respects_max_pages(env):
    store, h, conn, _, _ = env
    pages = [
        MessagePage(
            messages=[header(f"m{i}", "school@x.org")],
            next_cursor=f"c{i}",
            has_more=True,
        )
        for i in range(3)
    ]
    mail = FakeMail(pages, {})
    result = run(env, mail, max_pages=2)
    assert result.discovered == 2
    assert result.next_cursor == "c1"
    assert store.get_connection(h.id, conn.id).sync_cursor == "c1"
    mail2 = FakeMail(pages[2:], {})
    result2 = run(env, mail2)
    assert result2.next_cursor is None


def test_pagination_marks_partial_has_more(env):
    store, h, conn, _, _ = env
    pages = [
        MessagePage(
            messages=[header("m1", "school@x.org")],
            next_cursor="c1",
            has_more=True,
        ),
        MessagePage(
            messages=[header("m2", "school@x.org")],
            next_cursor="c2",
            has_more=True,
        ),
    ]
    result = run(env, FakeMail(pages, {}), max_pages=1)
    assert result.has_more is True
    assert result.next_cursor == "c1"
    assert store.get_connection(h.id, conn.id).sync_cursor == "c1"


def test_repeated_cursor_fails_safely(env):
    store, h, conn, _, _ = env
    pages = [
        MessagePage(messages=[], next_cursor="page:same", has_more=True),
        MessagePage(messages=[], next_cursor="page:same", has_more=True),
    ]
    with pytest.raises(ProviderError):
        run(env, FakeMail(pages, {}))
    assert store.get_connection(h.id, conn.id).sync_status == "failed"


def test_terminal_page_clears_cursor(env):
    pages = [
        MessagePage(
            messages=[header("m1", "school@x.org")],
            next_cursor=None,
            has_more=False,
        )
    ]
    result = run(env, FakeMail(pages, {}))
    assert result.has_more is False
    assert result.next_cursor is None
