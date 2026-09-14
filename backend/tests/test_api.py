from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from schoolsift.api import create_app
from schoolsift.config import Settings
from schoolsift.domain import (
    ActionPacket,
    EscalationProposal,
    ReplyProposal,
    payload_digest,
)
from schoolsift.identity import Principal
from schoolsift.sqlite_store import SQLiteStore


class FakeVerifier:
    def __init__(
        self,
        principal: Principal | None = None,
        fail: Exception | None = None,
    ):
        self.principal = principal
        self.fail = fail

    def verify(self, bearer_token: str) -> Principal:
        if self.fail is not None:
            raise self.fail
        assert self.principal is not None
        return self.principal


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "api.db",
        allowed_origins=("http://localhost:3210",),
    )


@pytest.fixture()
def store(settings) -> SQLiteStore:
    s = SQLiteStore(settings.database_path)
    s.initialize()
    return s


@pytest.fixture()
def client(settings, store) -> TestClient:
    return TestClient(create_app(settings=settings, store=store))


def seed_packet(store: SQLiteStore, household_id: str, payload=None) -> ActionPacket:
    import sqlite3
    from datetime import UTC, datetime

    payload = payload or ReplyProposal(
        kind="reply", recipient="school@example.org", subject="Re: X", body="b"
    )
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO connections (id, household_id, provider,"
            " provider_subject, email, status, last_sync_at, created_at)"
            " VALUES ('conn-1', ?, 'gmail', 'sub-1', 'me@example.com',"
            " 'connected', NULL, ?)",
            (household_id, datetime.now(UTC).isoformat()),
        )
        con.execute(
            "INSERT OR IGNORE INTO messages (id, household_id, connection_id,"
            " provider_message_id, thread_id, sender_name, sender_email,"
            " reply_to, subject, received_at, source_confirmed, status, body_ref,"
            " attachment_ids) VALUES ('msg-1', ?, 'conn-1', 'pm-1', 't-1',"
            " 'School Office', 'school@example.org', 'school@example.org',"
            " 'X', ?, 1, 'processed', 'ref-body', '[\"doc-1\"]')",
            (household_id, datetime.now(UTC).isoformat()),
        )
        con.execute(
            "INSERT OR IGNORE INTO documents (id, household_id, message_id, name,"
            " mime, content_ref, acroform_fields) VALUES ('doc-1', ?, 'msg-1',"
            " 'form.pdf', 'application/pdf', 'ref-pdf',"
            ' \'["student_name", "student_signature"]\')',
            (household_id,),
        )
        con.commit()
    finally:
        con.close()
    packet = ActionPacket(
        id="packet-1",
        source_message_id="msg-1",
        sender="School Office <school@example.org>",
        subject="X",
        summary="s",
        child=None,
        deadline=None,
        urgency="none",
        information_only=False,
        evidence=[],
        uncertainties=[],
        proposals=[
            {
                "id": "prop-1",
                "version": 1,
                "status": "proposed",
                "payload": payload.model_dump(mode="json"),
                "payload_hash": payload_digest(payload),
            }
        ],
    )
    store.save_packet(household_id, packet)
    return packet


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mode": "local"}


def test_bootstrap_empty(client):
    r = client.get("/v1/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "local"
    assert body["household"] is None
    assert body["children"] == []
    assert body["connections"] == []
    assert body["packets"] == []
    assert body["capabilities"] == {
        "gmail": False,
        "outlook": False,
        "agent": False,
        "aws": False,
    }


def test_household_create_and_conflict(client):
    r = client.post(
        "/v1/household", json={"name": "Home", "timezone": "America/Los_Angeles"}
    )
    assert r.status_code == 200
    hid = r.json()["id"]
    assert client.get("/v1/bootstrap").json()["household"]["id"] == hid
    r2 = client.post("/v1/household", json={"name": "Again", "timezone": "UTC"})
    assert r2.status_code == 200
    boot = client.get("/v1/bootstrap").json()
    assert boot["household"] is None
    assert boot["active_household_id"] is None
    assert len(boot["memberships"]) == 2


def test_children_flow(client):
    r = client.post("/v1/children", json={"name": "K", "school": "S", "grade": "1"})
    assert r.status_code == 409
    client.post("/v1/household", json={"name": "H", "timezone": "UTC"})
    r = client.post("/v1/children", json={"name": "K", "school": "S", "grade": "1"})
    assert r.status_code == 200
    assert r.json()["name"] == "K"
    assert client.get("/v1/bootstrap").json()["children"][0]["grade"] == "1"


def test_bad_timezone_400(client):
    r = client.post("/v1/household", json={"name": "H", "timezone": "Mars/Olympus"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_action_packets_empty_then_listed(client, store):
    assert client.get("/v1/action-packets").json() == []
    h = store.create_household(name="H", timezone="UTC")
    seed_packet(store, h.id)
    packets = client.get("/v1/action-packets").json()
    assert packets[0]["id"] == "packet-1"


def test_proposal_edit_approve_flow(client, store):
    h = store.create_household(name="H", timezone="UTC")
    packet = seed_packet(store, h.id)
    prop = packet.proposals[0]

    r = client.post(
        "/v1/proposals/prop-1/versions",
        json={
            "expected_version": 1,
            "payload": {
                "kind": "reply",
                "recipient": "school@example.org",
                "subject": "Re: X",
                "body": "edited",
            },
        },
    )
    assert r.status_code == 200
    v2 = r.json()
    assert v2["version"] == 2

    stale = client.post(
        "/v1/proposals/prop-1/versions/1/approve",
        json={"payload_hash": prop.payload_hash},
    )
    assert stale.status_code == 409

    ok = client.post(
        "/v1/proposals/prop-1/versions/2/approve",
        json={"payload_hash": v2["payload_hash"]},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["proposal"]["status"] == "approved"
    assert body["execution"]["status"] == "pending_dispatch"
    assert body["execution"]["proposal_version"] == 2
    assert "output_ref" not in body["execution"]

    again = client.post(
        "/v1/proposals/prop-1/versions/2/approve",
        json={"payload_hash": v2["payload_hash"]},
    )
    assert again.status_code == 200
    assert again.json()["execution"]["id"] == body["execution"]["id"]


def test_proposal_reject(client, store):
    h = store.create_household(name="H", timezone="UTC")
    packet = seed_packet(store, h.id)
    prop = packet.proposals[0]
    r = client.post(
        "/v1/proposals/prop-1/versions/1/reject",
        json={"payload_hash": prop.payload_hash},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


def test_unknown_proposal_404(client, store):
    store.create_household(name="H", timezone="UTC")
    r = client.post("/v1/proposals/nope/versions/1/reject", json={"payload_hash": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_escalation_409(client, store):
    h = store.create_household(name="H", timezone="UTC")
    esc = EscalationProposal(kind="escalation", reason="payment", detail="fee")
    packet = seed_packet(store, h.id, payload=esc)
    r = client.post(
        "/v1/proposals/prop-1/versions/1/approve",
        json={"payload_hash": packet.proposals[0].payload_hash},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ESCALATION_NOT_APPROVABLE"


def test_cors(client):
    r = client.get("/health", headers={"Origin": "http://localhost:3210"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3210"
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_aws_mode_reports_mode(settings):
    s = Settings(
        environment="aws",
        aws_region="us-west-2",
        bedrock_model_id="m",
        cognito_issuer="https://issuer.example",
        cognito_audience="aud",
        auth_mode="cognito",
        gmail_push_audience="aud",
        gmail_push_service_account="push@gserviceaccount.com",
        gmail_topic="projects/p/topics/t",
        database_path=settings.database_path,
        public_web_url="https://app.example",
        public_api_url="https://api.example",
    )
    verifier = FakeVerifier(Principal(user_id="u1", email="u@example.com"))
    c = TestClient(
        create_app(
            settings=s,
            store=SQLiteStore(settings.database_path),
            token_verifier=verifier,
        )
    )
    assert c.get("/v1/bootstrap").status_code == 401
    body = c.get("/v1/bootstrap", headers={"Authorization": "Bearer tok"}).json()
    assert body["mode"] == "aws"
    assert body["capabilities"]["aws"] is True


class FakeProvider:
    def __init__(self, *, fail_exchange: bool = False) -> None:
        self.fail_exchange = fail_exchange
        self.states: list[str] = []
        self.email = "me@x.com"

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        self.states.append(state)
        return (
            f"https://auth.example/authorize?state={state}&redirect_uri={redirect_uri}"
        )

    def exchange_code(self, *, code: str, redirect_uri: str):
        from pydantic import SecretStr

        from schoolsift.credentials import OAuthCredentials

        if self.fail_exchange:
            from schoolsift.errors import ProviderError

            raise ProviderError("provider rejected the code")
        return OAuthCredentials(
            access_token=SecretStr("at"),
            refresh_token=SecretStr("rt"),
            expires_at=None,
            scopes=("openid",),
        )

    def identity(self, credentials):
        from schoolsift.providers import ProviderIdentity

        return ProviderIdentity(provider_subject="sub-1", email=self.email)


class FakeVault:
    def __init__(self, *, fail_put: bool = False) -> None:
        self.data = {}
        self.fail_put = fail_put

    def put(self, connection_id, credentials) -> None:
        if self.fail_put:
            raise RuntimeError("keychain unavailable")
        self.data[connection_id] = credentials

    def get(self, connection_id):
        return self.data[connection_id]

    def delete(self, connection_id) -> None:
        self.data.pop(connection_id, None)


def oauth_client(settings, store, *, provider=None, vault=None):
    provider = provider if provider is not None else FakeProvider()
    vault = vault if vault is not None else FakeVault()
    c = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": provider},
            vault=vault,
        )
    )
    return c, provider, vault


def test_authorize_requires_household(settings, store):
    c, _, _ = oauth_client(settings, store)
    r = c.post("/v1/connections/gmail/authorize")
    assert r.status_code == 409


def test_authorize_unconfigured_provider(settings, store):
    c, _, _ = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    r = c.post("/v1/connections/outlook/authorize")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"


def test_authorize_returns_url_with_state(settings, store):
    c, provider, _ = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    r = c.post("/v1/connections/gmail/authorize")
    assert r.status_code == 200
    url = r.json()["authorization_url"]
    assert url.startswith("https://auth.example/authorize")
    assert provider.states[0] in url


def test_callback_rejects_bad_state(settings, store):
    c, _, _ = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    r = c.get("/v1/connections/gmail/callback?code=x&state=bogus")
    assert r.status_code == 409


def test_callback_links_connection_and_redirects(settings, store):
    c, provider, vault = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    c.post("/v1/connections/gmail/authorize")
    state = provider.states[0]
    r = c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/app?connected=gmail")
    conns = c.get("/v1/connections").json()
    assert len(conns) == 1
    assert conns[0]["provider"] == "gmail"
    assert conns[0]["status"] == "connected"
    assert conns[0]["email"] == "me@x.com"
    assert vault.get(conns[0]["id"]).access_token.get_secret_value() == "at"
    assert "at" not in r.text and "rt" not in r.text


def test_callback_replay_fails(settings, store):
    c, provider, _ = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    c.post("/v1/connections/gmail/authorize")
    state = provider.states[0]
    c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    r = c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 409


def test_callback_vault_failure_leaves_no_connected_account(settings, store):
    c, provider, _ = oauth_client(settings, store, vault=FakeVault(fail_put=True))
    store.create_household(name="H", timezone="UTC")
    c.post("/v1/connections/gmail/authorize")
    state = provider.states[0]
    r = c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert r.status_code >= 400
    conns = c.get("/v1/connections").json()
    assert conns == [] or conns[0]["status"] != "connected"


def test_disconnect_marks_and_removes_credentials(settings, store):
    c, provider, vault = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    c.post("/v1/connections/gmail/authorize")
    state = provider.states[0]
    c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    conn_id = c.get("/v1/connections").json()[0]["id"]
    r = c.delete(f"/v1/connections/{conn_id}")
    assert r.status_code == 204
    assert c.get("/v1/connections").json()[0]["status"] == "disconnected"
    with pytest.raises(KeyError):
        vault.get(conn_id)


def test_delete_unknown_connection_404(settings, store):
    c, _, _ = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    r = c.delete("/v1/connections/nope")
    assert r.status_code == 404


class FakeMailProvider(FakeProvider):
    def __init__(self, pages=None, fetched=None, **kw):
        super().__init__(**kw)
        self.pages = list(pages or [])
        self.fetched = dict(fetched or {})
        self.fetch_calls: list[str] = []

    def refresh(self, credentials):
        return credentials

    def list_message_headers(self, credentials, *, cursor):
        from schoolsift.models import MessagePage

        return (
            self.pages.pop(0)
            if self.pages
            else MessagePage(messages=[], next_cursor=None, has_more=False)
        )

    def fetch_message(self, credentials, provider_message_id):
        self.fetch_calls.append(provider_message_id)
        return self.fetched[provider_message_id]


def link_gmail(c, provider):
    c.post("/v1/connections/gmail/authorize")
    state = provider.states[-1]
    return c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )


def test_unknown_provider_404(settings, store):
    c, _, _ = oauth_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    r = c.post("/v1/connections/yahoo/authorize")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_callback_vault_failure_restores_prior_status(settings, store):
    good_vault = FakeVault()
    c, provider, _ = oauth_client(settings, store, vault=good_vault)
    store.create_household(name="H", timezone="UTC")
    link_gmail(c, provider)
    conn = c.get("/v1/connections").json()[0]
    assert conn["status"] == "connected"

    class FlakyVault(FakeVault):
        def put(self, connection_id, credentials) -> None:
            raise RuntimeError("keychain unavailable")

    c2 = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": provider},
            vault=FlakyVault(),
        )
    )
    c2.post("/v1/connections/gmail/authorize")
    state = provider.states[-1]
    r = c2.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert r.status_code >= 400
    assert c.get("/v1/connections").json()[0]["status"] == "connected"


def test_callback_vault_failure_restores_prior_email(settings, store):
    good_vault = FakeVault()
    c, provider, _ = oauth_client(settings, store, vault=good_vault)
    store.create_household(name="H", timezone="UTC")
    link_gmail(c, provider)
    conn = c.get("/v1/connections").json()[0]
    assert conn["email"] == "me@x.com"

    provider.email = "renamed@x.com"
    c2 = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": provider},
            vault=FakeVault(fail_put=True),
        )
    )
    c2.post("/v1/connections/gmail/authorize")
    state = provider.states[-1]
    r = c2.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert r.status_code >= 400
    after = c.get("/v1/connections").json()[0]
    assert after["status"] == "connected"
    assert after["email"] == "me@x.com"


def test_callback_finalize_failure_removes_orphan_credential(settings, store):
    class FailFinalize:
        def __init__(self, inner):
            self._s = inner

        def __getattr__(self, name):
            return getattr(self._s, name)

        def set_connection_status(self, *args, **kwargs):
            raise RuntimeError("db gone")

    provider = FakeProvider()
    vault = FakeVault()
    c = TestClient(
        create_app(
            settings=settings,
            store=FailFinalize(store),
            providers={"gmail": provider},
            vault=vault,
        )
    )
    store.create_household(name="H", timezone="UTC")
    c.post("/v1/connections/gmail/authorize")
    state = provider.states[-1]
    r = c.get(
        f"/v1/connections/gmail/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert r.status_code >= 400
    assert vault.data == {}


def test_sync_endpoint_and_sources_flow(settings, store):
    from datetime import UTC, datetime

    from schoolsift.models import FetchedMessage, MessageHeader, MessagePage

    header = MessageHeader(
        provider_message_id="m1",
        thread_id="t1",
        sender_name="Office",
        sender_email="office@school.org",
        reply_to="office@school.org",
        subject="Hi",
        received_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    fetched = FetchedMessage(**header.model_dump(), body_text="body", attachments=[])
    provider = FakeMailProvider(
        pages=[
            MessagePage(messages=[header], next_cursor=None, has_more=False),
            MessagePage(messages=[header], next_cursor=None, has_more=False),
        ],
        fetched={"m1": fetched},
    )
    vault = FakeVault()
    c = TestClient(
        create_app(
            settings=settings, store=store, providers={"gmail": provider}, vault=vault
        )
    )
    store.create_household(name="H", timezone="UTC")
    link_gmail(c, provider)
    conn = c.get("/v1/connections").json()[0]

    r = c.post(f"/v1/connections/{conn['id']}/sync")
    assert r.status_code == 200
    result = r.json()
    assert result["discovered"] == 1
    assert result["imported"] == 0
    assert provider.fetch_calls == []

    srcs = c.get("/v1/sources").json()
    assert srcs[0]["sender_email"] == "office@school.org"
    assert srcs[0]["sender_name"] == "Office"
    assert srcs[0]["message_count"] == 1
    assert srcs[0]["status"] == "suggested"

    r = c.post(f"/v1/sources/{srcs[0]['id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    r = c.post(f"/v1/connections/{conn['id']}/sync")
    assert r.json()["imported"] == 1
    assert provider.fetch_calls == ["m1"]

    boot = c.get("/v1/bootstrap").json()
    assert boot["messages"][0]["status"] == "awaiting_agent"
    assert "body_ref" not in boot["messages"][0]
    assert "attachment_ids" not in boot["messages"][0]
    assert "sync_cursor" not in boot["connections"][0]
    assert "provider_subject" not in boot["connections"][0]


def test_sync_requires_connected_connection(settings, store):
    provider = FakeMailProvider()
    c = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": provider},
            vault=FakeVault(),
        )
    )
    store.create_household(name="H", timezone="UTC")
    link_gmail(c, provider)
    conn = c.get("/v1/connections").json()[0]
    c.delete(f"/v1/connections/{conn['id']}")
    r = c.post(f"/v1/connections/{conn['id']}/sync")
    assert r.status_code == 409


def test_sources_reject_and_cross_tenant(settings, store):
    provider = FakeMailProvider()
    c = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": provider},
            vault=FakeVault(),
        )
    )
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="a@x.com"
    )
    from datetime import UTC, datetime

    src = store.upsert_source_suggestion(
        h.id,
        conn.id,
        sender_email="x@y.org",
        sender_name="Office",
        seen_at=datetime.now(UTC),
    )
    r = c.post(f"/v1/sources/{src.id}/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    r = c.post("/v1/sources/nonexistent/confirm")
    assert r.status_code == 404


def _seed_awaiting_message(store, h_id: str):
    from datetime import UTC, datetime

    from schoolsift.models import FetchedMessage, MessageHeader

    conn = store.upsert_connection(
        h_id, provider="gmail", provider_subject="s", email="a@x.com"
    )
    header = MessageHeader(
        provider_message_id="pm-1",
        thread_id="t-1",
        sender_name="Office",
        sender_email="office@school.org",
        reply_to="office@school.org",
        subject="Field trip",
        received_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    fetched = FetchedMessage(
        **header.model_dump(), body_text="due Friday", attachments=[]
    )
    return store.save_fetched_message(
        h_id, conn.id, fetched, body_ref="ref-body", attachments=[]
    )


class ApiFakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def analyze(self, household_id: str, message_id: str):
        from schoolsift.domain import ActionPacketDraft

        self.calls.append((household_id, message_id))
        return ActionPacketDraft(
            source_message_id=message_id,
            school_source_id=None,
            summary="A packet.",
        )


def test_process_message_endpoint(settings, store):
    analyzer = ApiFakeAnalyzer()
    c = TestClient(create_app(settings=settings, store=store, analyzer=analyzer))
    h = store.create_household(name="H", timezone="UTC")
    record = _seed_awaiting_message(store, h.id)
    boot = c.get("/v1/bootstrap").json()
    assert boot["capabilities"]["agent"] is True

    r = c.post(f"/v1/messages/{record.id}/process")
    assert r.status_code == 200
    packet = r.json()
    assert packet["source_message_id"] == record.id
    assert packet["summary"] == "A packet."
    assert analyzer.calls == [(h.id, record.id)]
    assert store.get_message_record(h.id, record.id).status == "processed"

    again = c.post(f"/v1/messages/{record.id}/process")
    assert again.status_code == 200
    assert again.json()["id"] == packet["id"]
    assert len(analyzer.calls) == 1


def test_process_requires_analyzer(settings, store):
    c = TestClient(create_app(settings=settings, store=store))
    h = store.create_household(name="H", timezone="UTC")
    record = _seed_awaiting_message(store, h.id)
    r = c.post(f"/v1/messages/{record.id}/process")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"
    assert "SCHOOLSIFT_BEDROCK_MODEL_ID" in r.json()["error"]["message"]


def test_process_unknown_message_404(settings, store):
    c = TestClient(
        create_app(settings=settings, store=store, analyzer=ApiFakeAnalyzer())
    )
    store.create_household(name="H", timezone="UTC")
    r = c.post("/v1/messages/msg-nope/process")
    assert r.status_code == 404


def test_process_unsafe_draft_marks_failed(settings, store):
    from schoolsift.domain import ActionPacketDraft, ReplyProposal

    class UnsafeAnalyzer:
        def analyze(self, household_id: str, message_id: str):
            return ActionPacketDraft(
                source_message_id=message_id,
                school_source_id=None,
                summary="x",
                proposals=[
                    ReplyProposal(
                        kind="reply",
                        recipient="attacker@evil.example",
                        subject="Re",
                        body="send money",
                    )
                ],
            )

    c = TestClient(
        create_app(settings=settings, store=store, analyzer=UnsafeAnalyzer())
    )
    h = store.create_household(name="H", timezone="UTC")
    record = _seed_awaiting_message(store, h.id)
    r = c.post(f"/v1/messages/{record.id}/process")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UNSAFE_AGENT_OUTPUT"
    msg = store.get_message_record(h.id, record.id)
    assert msg.status == "failed"
    assert store.list_packets(h.id) == []


class _ExecProvider:
    def __init__(self) -> None:
        self.sent: list = []

    def send_email(self, credentials, command, *, checkpoint=None):
        self.sent.append(command)
        from schoolsift.providers import OperationResult

        return OperationResult(operation_id="op-1")

    def create_calendar_event(self, credentials, command):
        from schoolsift.providers import OperationResult

        return OperationResult(operation_id="evt-1")

    def refresh(self, credentials):
        return credentials


def _exec_client(settings, store):
    from datetime import UTC, datetime, timedelta

    from pydantic import SecretStr

    from schoolsift.credentials import OAuthCredentials

    vault = FakeVault()
    vault.put(
        "conn-1",
        OAuthCredentials(
            access_token=SecretStr("at"),
            refresh_token=SecretStr("rt"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=("openid",),
        ),
    )
    provider = _ExecProvider()
    c = TestClient(
        create_app(
            settings=settings,
            store=store,
            providers={"gmail": provider},
            vault=vault,
        )
    )
    return c, provider


def _approve(client, store, household_id, headers=None):
    packet = seed_packet(store, household_id)
    prop = packet.proposals[0]
    r = client.post(
        f"/v1/proposals/{prop.id}/versions/1/approve",
        json={"payload_hash": prop.payload_hash},
        headers=headers,
    )
    assert r.status_code == 200
    return r.json()["execution"]


def test_executions_list_scoped(client, store):
    assert client.get("/v1/executions").json() == []
    h = store.create_household(name="H", timezone="UTC")
    execution = _approve(client, store, h.id)
    items = client.get("/v1/executions").json()
    assert len(items) == 1
    assert items[0]["id"] == execution["id"]
    assert items[0]["status"] == "pending_dispatch"
    assert "output_ref" not in items[0]
    assert "idempotency_key" in items[0]


def test_executions_dispatch_and_run(settings, store):
    c, provider = _exec_client(settings, store)
    h = store.create_household(name="H", timezone="UTC")
    execution = _approve(c, store, h.id)

    r = c.post("/v1/executions/dispatch")
    assert r.status_code == 200
    assert r.json() == {"dispatched": 1}
    assert r.json()["dispatched"] == 1
    again = c.post("/v1/executions/dispatch")
    assert again.json() == {"dispatched": 0}

    listed = c.get("/v1/executions").json()
    assert listed[0]["status"] == "queued"

    no_confirm = c.post(f"/v1/executions/{execution['id']}/run", json={})
    assert no_confirm.status_code == 422
    denied = c.post(f"/v1/executions/{execution['id']}/run", json={"confirm": False})
    assert denied.status_code == 400
    assert provider.sent == []

    ran = c.post(f"/v1/executions/{execution['id']}/run", json={"confirm": True})
    assert ran.status_code == 200
    assert ran.json()["status"] == "completed"
    assert ran.json()["provider_operation_id"] == "op-1"
    assert len(provider.sent) == 1

    rerun = c.post(f"/v1/executions/{execution['id']}/run", json={"confirm": True})
    assert rerun.status_code == 200
    assert rerun.json()["status"] == "completed"
    assert len(provider.sent) == 1


def test_executions_run_unknown_404(settings, store):
    c, _ = _exec_client(settings, store)
    store.create_household(name="H", timezone="UTC")
    r = c.post("/v1/executions/exec-nope/run", json={"confirm": True})
    assert r.status_code == 404


def test_executions_endpoints_local_only(settings, store):
    h = store.create_household(name="H", timezone="UTC")
    execution = _approve(
        TestClient(create_app(settings=settings, store=store)), store, h.id
    )
    aws = Settings(
        environment="aws",
        aws_region="us-west-2",
        bedrock_model_id="m",
        cognito_issuer="https://issuer.example",
        cognito_audience="aud",
        auth_mode="cognito",
        gmail_push_audience="aud",
        gmail_push_service_account="sa@example.com",
        public_api_url="https://api.example.com",
        gmail_topic="projects/p/topics/t",
        database_path=store.path,
    )
    c = TestClient(
        create_app(
            settings=aws,
            store=store,
            token_verifier=FakeVerifier(
                Principal(user_id="local-caregiver", email="l@x.invalid")
            ),
        )
    )
    auth = {"Authorization": "Bearer valid", "X-SchoolSift-Household": h.id}
    assert c.post("/v1/executions/dispatch", headers=auth).status_code == 404
    assert (
        c.post(
            f"/v1/executions/{execution['id']}/run",
            json={"confirm": True},
            headers=auth,
        ).status_code
        == 404
    )


def test_executions_viewer_read_only(store):
    import sqlite3
    from datetime import UTC, datetime

    h = store.create_household_for_owner(
        Principal(user_id="owner-1", email="o@x.com"), name="H", timezone="UTC"
    )
    con = sqlite3.connect(store.path)
    try:
        con.execute(
            "INSERT INTO memberships (household_id, user_id, email, role,"
            " created_at) VALUES (?, 'viewer-1', 'v@x.com', 'viewer', ?)",
            (h.id, datetime.now(UTC).isoformat()),
        )
        con.commit()
    finally:
        con.close()

    settings = Settings(
        environment="local",
        auth_mode="cognito",
        cognito_issuer="https://issuer.example",
        cognito_audience="aud",
        database_path=store.path,
        allowed_origins=("http://localhost:3210",),
    )
    owner = TestClient(
        create_app(
            settings=settings,
            store=store,
            token_verifier=FakeVerifier(Principal(user_id="owner-1", email="o@x.com")),
        )
    )
    auth = {"Authorization": "Bearer t", "X-SchoolSift-Household": h.id}
    execution = _approve(owner, store, h.id, headers=auth)
    viewer = TestClient(
        create_app(
            settings=settings,
            store=store,
            token_verifier=FakeVerifier(Principal(user_id="viewer-1", email="v@x.com")),
        )
    )
    assert viewer.get("/v1/executions", headers=auth).status_code == 200
    assert viewer.post("/v1/executions/dispatch", headers=auth).status_code == 403
    assert (
        viewer.post(
            f"/v1/executions/{execution['id']}/run",
            json={"confirm": True},
            headers=auth,
        ).status_code
        == 403
    )
