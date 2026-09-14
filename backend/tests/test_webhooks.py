from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from schoolsift.api import create_app
from schoolsift.config import Settings
from schoolsift.sqlite_store import SQLiteStore

AUDIENCE = "schoolsift-push"
SERVICE_ACCOUNT = "push@system.gserviceaccount.com"


class FakeVerifier:
    def __init__(self, claims=None, fail: Exception | None = None) -> None:
        self.claims = (
            claims
            if claims is not None
            else {
                "email": SERVICE_ACCOUNT,
                "email_verified": True,
            }
        )
        self.fail = fail
        self.seen: list[tuple[str, str]] = []

    def verify(self, bearer_token: str, *, audience: str):
        self.seen.append((bearer_token, audience))
        if self.fail:
            raise self.fail
        if audience != AUDIENCE:
            raise ValueError("audience mismatch")
        return self.claims


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "wh.db",
        gmail_push_audience=AUDIENCE,
        gmail_push_service_account=SERVICE_ACCOUNT,
    )


@pytest.fixture()
def store(settings) -> SQLiteStore:
    s = SQLiteStore(settings.database_path)
    s.initialize()
    return s


def _client(settings, store, verifier=None):
    return TestClient(
        create_app(
            settings=settings,
            store=store,
            google_push_verifier=verifier if verifier is not None else FakeVerifier(),
        )
    )


def _gmail_body(email="me@example.com", history_id=12345, message_id="mid-1"):
    data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    ).decode()
    return {
        "message": {
            "messageId": message_id,
            "data": data,
            "publishTime": "2026-01-01T00:00:00Z",
        },
        "subscription": "projects/p/subscriptions/s",
    }


def _events(store) -> list[tuple]:
    con = sqlite3.connect(store.path)
    try:
        return con.execute(
            "SELECT dedupe_key, payload, status FROM ingestion_events"
        ).fetchall()
    finally:
        con.close()


def _connected_gmail(store):
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="Me@Example.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    return h, conn


class TestGmailWebhook:
    def test_unconfigured_returns_503(self, tmp_path):
        s = SQLiteStore(tmp_path / "x.db")
        s.initialize()
        bare = Settings(database_path=tmp_path / "x.db")
        c = TestClient(
            create_app(settings=bare, store=s, google_push_verifier=FakeVerifier())
        )
        r = c.post("/v1/webhooks/gmail", json=_gmail_body())
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "WEBHOOK_NOT_CONFIGURED"

    def test_missing_and_malformed_auth_401(self, settings, store):
        c = _client(settings, store)
        assert c.post("/v1/webhooks/gmail", json=_gmail_body()).status_code == 401
        assert (
            c.post(
                "/v1/webhooks/gmail",
                json=_gmail_body(),
                headers={"Authorization": "Basic abc"},
            ).status_code
            == 401
        )
        assert (
            c.post(
                "/v1/webhooks/gmail",
                json=_gmail_body(),
                headers={"Authorization": "Bearer"},
            ).status_code
            == 401
        )

    def test_verifier_failure_401(self, settings, store):
        c = _client(settings, store, verifier=FakeVerifier(fail=ValueError("bad")))
        r = c.post(
            "/v1/webhooks/gmail",
            json=_gmail_body(),
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 401
        assert "tok" not in r.text

    def test_unverified_or_wrong_account_401(self, settings, store):
        c = _client(
            settings,
            store,
            verifier=FakeVerifier(
                claims={"email": SERVICE_ACCOUNT, "email_verified": False}
            ),
        )
        r = c.post(
            "/v1/webhooks/gmail",
            json=_gmail_body(),
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 401

        c = _client(
            settings,
            store,
            verifier=FakeVerifier(
                claims={"email": "other@gserviceaccount.com", "email_verified": True}
            ),
        )
        r = c.post(
            "/v1/webhooks/gmail",
            json=_gmail_body(),
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 401

    def test_valid_event_enqueues(self, settings, store):
        h, conn = _connected_gmail(store)
        c = _client(settings, store)
        r = c.post(
            "/v1/webhooks/gmail",
            json=_gmail_body(),
            headers={"Authorization": "Bearer real-token"},
        )
        assert r.status_code == 204
        rows = _events(store)
        assert len(rows) == 1
        assert rows[0][0] == "gmail:mid-1"
        payload = json.loads(rows[0][1])
        assert payload["kind"] == "mail_changed"
        assert payload["household_id"] == h.id
        assert payload["connection_id"] == conn.id
        assert payload["provider_cursor"] == "history:12345"
        assert "real-token" not in rows[0][1]

    def test_duplicate_message_id_enqueues_once(self, settings, store):
        _connected_gmail(store)
        c = _client(settings, store)
        for _ in range(2):
            r = c.post(
                "/v1/webhooks/gmail",
                json=_gmail_body(),
                headers={"Authorization": "Bearer tok"},
            )
            assert r.status_code == 204
        assert len(_events(store)) == 1

    def test_unknown_or_disconnected_account_204_no_enqueue(self, settings, store):
        h, conn = _connected_gmail(store)
        c = _client(settings, store)
        r = c.post(
            "/v1/webhooks/gmail",
            json=_gmail_body(email="nobody@else.com"),
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 204
        assert _events(store) == []

        store.set_connection_status(h.id, conn.id, "disconnected")
        r = c.post(
            "/v1/webhooks/gmail",
            json=_gmail_body(),
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 204
        assert _events(store) == []

    def test_malformed_bodies_422(self, settings, store):
        _connected_gmail(store)
        c = _client(settings, store)
        headers = {"Authorization": "Bearer tok"}
        assert (
            c.post(
                "/v1/webhooks/gmail",
                content=b"not json",
                headers=headers,
            ).status_code
            == 422
        )
        bad_b64 = {
            "message": {"messageId": "m", "data": "!!!", "publishTime": "t"},
            "subscription": "s",
        }
        assert (
            c.post("/v1/webhooks/gmail", json=bad_b64, headers=headers).status_code
            == 422
        )
        bad_payload = {
            "message": {
                "messageId": "m",
                "data": base64.urlsafe_b64encode(b'{"x": 1}').decode(),
                "publishTime": "t",
            },
            "subscription": "s",
        }
        assert (
            c.post("/v1/webhooks/gmail", json=bad_payload, headers=headers).status_code
            == 422
        )
        big = json.dumps(_gmail_body()).encode() + b" " * (64 * 1024)
        assert (
            c.post("/v1/webhooks/gmail", content=big, headers=headers).status_code
            == 422
        )


def _outlook_setup(store):
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="outlook", provider_subject="s", email="a@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    state = "secret-client-state"
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="outlook",
        provider_subscription_id="sub-123",
        client_state_hash=hashlib.sha256(state.encode()).hexdigest(),
    )
    return h, conn, state


def _note(
    sub_id="sub-123",
    state="secret-client-state",
    change="created",
    resource="me/messages/abc",
    rid="abc",
    lifecycle=None,
):
    note = {
        "subscriptionId": sub_id,
        "clientState": state,
        "changeType": change,
        "resource": resource,
        "resourceData": {"id": rid},
    }
    if lifecycle:
        note["lifecycleEvent"] = lifecycle
    return note


class TestOutlookWebhook:
    def test_validation_handshake(self, settings, store):
        c = _client(settings, store)
        r = c.post("/v1/webhooks/outlook?validationToken=abc%3D123%20tok")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "abc=123 tok"
        assert _events(store) == []

    def test_validation_token_bounds(self, settings, store):
        c = _client(settings, store)
        assert c.post("/v1/webhooks/outlook?validationToken=").status_code == 422
        too_long = "x" * 513
        assert (
            c.post(f"/v1/webhooks/outlook?validationToken={too_long}").status_code
            == 422
        )

    def test_malformed_body_422(self, settings, store):
        c = _client(settings, store)
        assert c.post("/v1/webhooks/outlook", content=b"not json").status_code == 422
        many = {"value": [_note() for _ in range(101)]}
        assert c.post("/v1/webhooks/outlook", json=many).status_code == 422

    def test_unknown_or_bad_state_ignored(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        r = c.post("/v1/webhooks/outlook", json={"value": [_note(sub_id="nope")]})
        assert r.status_code == 202
        r = c.post("/v1/webhooks/outlook", json={"value": [_note(state="wrong")]})
        assert r.status_code == 202
        assert _events(store) == []

    def test_created_maps_to_mail_changed(self, settings, store):
        h, conn, _ = _outlook_setup(store)
        c = _client(settings, store)
        r = c.post("/v1/webhooks/outlook", json={"value": [_note()]})
        assert r.status_code == 202
        rows = _events(store)
        assert len(rows) == 1
        payload = json.loads(rows[0][1])
        assert payload["kind"] == "mail_changed"
        assert payload["provider"] == "outlook"
        assert payload["household_id"] == h.id
        assert payload["connection_id"] == conn.id
        assert payload["provider_cursor"] == "abc"
        assert "secret-client-state" not in rows[0][1]
        assert "secret-client-state" not in r.text

    def test_lifecycle_events_map(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        notes = [
            _note(rid="m1", lifecycle="missed"),
            _note(rid="r1", lifecycle="reauthorizationRequired"),
            _note(rid="s1", lifecycle="subscriptionRemoved"),
        ]
        r = c.post("/v1/webhooks/outlook", json={"value": notes})
        assert r.status_code == 202
        kinds = sorted(json.loads(row[1])["kind"] for row in _events(store))
        assert kinds == [
            "missed",
            "reauthorization_required",
            "subscription_removed",
        ]

    def test_unknown_change_type_ignored(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        r = c.post("/v1/webhooks/outlook", json={"value": [_note(change="deleted")]})
        assert r.status_code == 202
        assert _events(store) == []

    def test_duplicate_notification_idempotent(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        for _ in range(2):
            r = c.post("/v1/webhooks/outlook", json={"value": [_note()]})
            assert r.status_code == 202
        assert len(_events(store)) == 1

    def test_mixed_batch_only_valid_enqueued(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        notes = [
            _note(rid="good-1"),
            _note(sub_id="nope", rid="bad-1"),
            _note(state="wrong", rid="bad-2"),
            _note(rid="good-2", lifecycle="missed"),
        ]
        r = c.post("/v1/webhooks/outlook", json={"value": notes})
        assert r.status_code == 202
        rows = _events(store)
        assert len(rows) == 2
        cursors = sorted(json.loads(row[1])["provider_cursor"] for row in rows)
        assert cursors == ["good-1", "good-2"]

    def test_inactive_subscription_ignored(self, settings, store):
        h = _outlook_setup(store)[0]
        sub = store.get_webhook_subscription("outlook", "sub-123")
        store.set_webhook_subscription_status(h.id, sub.id, "expired")
        c = _client(settings, store)
        r = c.post("/v1/webhooks/outlook", json={"value": [_note()]})
        assert r.status_code == 202
        assert _events(store) == []


def test_oidc_verifier_uses_issuer_checked_verify(monkeypatch):
    import google.oauth2.id_token as idt

    from schoolsift.webhooks import GoogleOIDCPushVerifier

    used: dict[str, object] = {}

    def fake_oauth2(token, request, *, audience=None):
        used["oauth2"] = (token, audience)
        return {"email": "svc@x.iam.gserviceaccount.com"}

    def legacy(*args, **kwargs):
        raise AssertionError("verify_token must not be used")

    monkeypatch.setattr(idt, "verify_oauth2_token", fake_oauth2)
    monkeypatch.setattr(idt, "verify_token", legacy)
    claims = GoogleOIDCPushVerifier().verify("tok-1", audience="aud")
    assert used["oauth2"] == ("tok-1", "aud")
    assert claims["email"] == "svc@x.iam.gserviceaccount.com"


class TestStrictSchemas:
    def test_gmail_envelope_rejects_extra_fields(self, settings, store):
        _connected_gmail(store)
        c = _client(settings, store)
        body = _gmail_body()
        body["unexpected"] = "x"
        r = c.post(
            "/v1/webhooks/gmail",
            json=body,
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 422
        assert _events(store) == []

    def test_gmail_payload_rejects_extra_fields(self, settings, store):
        _connected_gmail(store)
        c = _client(settings, store)
        data = base64.b64encode(
            json.dumps(
                {"emailAddress": "me@example.com", "historyId": 1, "evil": True}
            ).encode()
        ).decode()
        body = {
            "message": {"messageId": "m", "data": data, "publishTime": "t"},
            "subscription": "s",
        }
        r = c.post(
            "/v1/webhooks/gmail",
            json=body,
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 422

    def test_graph_notification_rejects_extra_fields(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        note = _note()
        note["bogus"] = "field"
        r = c.post("/v1/webhooks/outlook", json={"value": [note]})
        assert r.status_code == 422
        assert _events(store) == []

    def test_graph_batch_rejects_extra_fields(self, settings, store):
        _outlook_setup(store)
        c = _client(settings, store)
        r = c.post(
            "/v1/webhooks/outlook",
            json={"value": [], "extra": "nope"},
        )
        assert r.status_code == 422
