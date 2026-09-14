from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from schoolsift.api import create_app
from schoolsift.config import Settings
from schoolsift.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
)
from schoolsift.identity import LOCAL_PRINCIPAL, Principal
from schoolsift.sqlite_store import MIGRATIONS, SQLiteStore

OWNER = Principal(user_id="owner-1", email="owner@example.com")
GUEST = Principal(user_id="guest-1", email="guest@example.com")
FUTURE = datetime.now(UTC) + timedelta(days=7)
PAST = datetime.now(UTC) - timedelta(days=1)


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "m.db")
    s.initialize()
    return s


def _household(store: SQLiteStore, principal: Principal = OWNER):
    return store.create_household_for_owner(principal, name="H", timezone="UTC")


def test_create_household_adds_owner_membership(store):
    h = _household(store)
    members = store.list_memberships(h.id)
    assert len(members) == 1
    assert members[0].user_id == OWNER.user_id
    assert members[0].role == "owner"
    assert members[0].email == "owner@example.com"


def test_migration_v8_backfills_owner_for_existing_household(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    try:
        for migrate in MIGRATIONS[:7]:
            migrate(con)
        con.execute(
            "INSERT INTO schema_version (version) VALUES (7)"
            if _has_schema_version(con)
            else "CREATE TABLE schema_version (version INTEGER NOT NULL)"
        )
        con.execute("DELETE FROM schema_version")
        con.execute("INSERT INTO schema_version (version) VALUES (7)")
        now = datetime.now(UTC).isoformat()
        con.execute(
            "INSERT INTO households (id, name, timezone, created_at)"
            " VALUES ('hh-old', 'Old', 'UTC', ?)",
            (now,),
        )
        con.commit()
    finally:
        con.close()
    s = SQLiteStore(db)
    s.initialize()
    members = s.list_memberships("hh-old")
    assert len(members) == 1
    assert members[0].role == "owner"
    assert members[0].user_id == "local-caregiver"


def _has_schema_version(con: sqlite3.Connection) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        is not None
    )


def test_invitation_round_trip(store):
    h = _household(store)
    invitation, invite_code = store.create_invitation(
        h.id, email="Guest@Example.com", role="editor", expires_at=FUTURE
    )
    assert invitation.email == "guest@example.com"
    assert invitation.status == "pending"
    listed = store.list_invitations(h.id)
    assert listed[0].id == invitation.id
    membership = store.accept_invitation(GUEST, invite_code)
    assert membership.role == "editor"
    assert membership.household_id == h.id
    assert store.get_membership(h.id, GUEST.user_id) is not None


def test_invitation_token_not_stored_plaintext(store, tmp_path):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="g@example.com", role="viewer", expires_at=FUTURE
    )
    con = sqlite3.connect(tmp_path / "m.db")
    try:
        hashes = [r[0] for r in con.execute("SELECT token_hash FROM invitations")]
    finally:
        con.close()
    assert invite_code not in hashes


def test_duplicate_pending_invitation_conflicts(store):
    h = _household(store)
    store.create_invitation(
        h.id, email="g@example.com", role="viewer", expires_at=FUTURE
    )
    with pytest.raises(ConflictError):
        store.create_invitation(
            h.id, email="G@example.com", role="editor", expires_at=FUTURE
        )


def test_invite_existing_member_conflicts(store):
    h = _household(store)
    with pytest.raises(ConflictError):
        store.create_invitation(
            h.id, email="owner@example.com", role="viewer", expires_at=FUTURE
        )


def test_accept_replay_fails(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="editor", expires_at=FUTURE
    )
    store.accept_invitation(GUEST, invite_code)
    with pytest.raises(ConflictError):
        store.accept_invitation(GUEST, invite_code)


def test_accept_expired_fails(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="editor", expires_at=PAST
    )
    with pytest.raises(ConflictError):
        store.accept_invitation(GUEST, invite_code)


def test_accept_wrong_email_fails(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="someone@example.com", role="editor", expires_at=FUTURE
    )
    with pytest.raises(ConflictError):
        store.accept_invitation(GUEST, invite_code)


def test_accept_unknown_token_fails(store):
    _household(store)
    with pytest.raises(ConflictError):
        store.accept_invitation(GUEST, "bogus-token")


def test_concurrent_accept_single_winner(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="editor", expires_at=FUTURE
    )
    results: list[str] = []

    def accept():
        try:
            store.accept_invitation(GUEST, invite_code)
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=accept) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1


def test_revoke_invitation(store):
    h = _household(store)
    invitation, invite_code = store.create_invitation(
        h.id, email="g@example.com", role="viewer", expires_at=FUTURE
    )
    revoked = store.revoke_invitation(h.id, invitation.id)
    assert revoked.status == "revoked"
    with pytest.raises(ConflictError):
        store.accept_invitation(GUEST, invite_code)


def test_revoke_cross_household_fails(store):
    h1 = _household(store)
    h2 = store.create_household_for_owner(
        Principal(user_id="o2", email="o2@example.com"),
        name="H2",
        timezone="UTC",
    )
    invitation, _ = store.create_invitation(
        h1.id, email="g@example.com", role="viewer", expires_at=FUTURE
    )
    with pytest.raises(NotFoundError):
        store.revoke_invitation(h2.id, invitation.id)


def test_cannot_demote_last_owner(store):
    h = _household(store)
    with pytest.raises(ConflictError):
        store.change_member_role(h.id, OWNER.user_id, "editor")


def test_cannot_remove_last_owner(store):
    h = _household(store)
    with pytest.raises(ConflictError):
        store.remove_member(h.id, OWNER.user_id)


def test_promote_then_demote_allowed(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="editor", expires_at=FUTURE
    )
    store.accept_invitation(GUEST, invite_code)
    store.change_member_role(h.id, GUEST.user_id, "owner")
    updated = store.change_member_role(h.id, OWNER.user_id, "editor")
    assert updated.role == "editor"


def test_remove_member(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="viewer", expires_at=FUTURE
    )
    store.accept_invitation(GUEST, invite_code)
    store.remove_member(h.id, GUEST.user_id)
    assert store.get_membership(h.id, GUEST.user_id) is None


def test_remove_member_cross_household_fails(store):
    h1 = _household(store)
    h2 = store.create_household_for_owner(
        Principal(user_id="o2", email="o2@example.com"),
        name="H2",
        timezone="UTC",
    )
    with pytest.raises(NotFoundError):
        store.remove_member(h1.id, "o2")
    assert store.get_membership(h2.id, "o2") is not None


# ---- API authorization ----


class FakeVerifier:
    def __init__(self, principal: Principal):
        self.principal = principal

    def verify(self, bearer_token: str) -> Principal:
        if not bearer_token.startswith("v"):
            raise AuthenticationError("nope")
        return self.principal


def _cognito_client(store: SQLiteStore, principal: Principal) -> TestClient:
    settings = Settings(
        environment="local",
        auth_mode="cognito",
        cognito_issuer="https://issuer.example",
        cognito_audience="aud",
        database_path=store.path,
        allowed_origins=("http://localhost:3210",),
    )
    return TestClient(
        create_app(
            settings=settings,
            store=store,
            token_verifier=FakeVerifier(principal),
        )
    )


def _auth(bearer: str = "valid-tok") -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


def test_cognito_requires_bearer(store):
    c = _cognito_client(store, OWNER)
    assert c.get("/v1/bootstrap").status_code == 401
    assert c.get("/v1/bootstrap", headers=_auth("invalid")).status_code == 401


def test_cognito_viewer_is_read_only(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="viewer", expires_at=FUTURE
    )
    store.accept_invitation(GUEST, invite_code)
    c = _cognito_client(store, GUEST)
    hdrs = {**_auth(), "X-SchoolSift-Household": h.id}
    assert c.get("/v1/messages", headers=hdrs).status_code == 200
    r = c.post(
        "/v1/children",
        json={"name": "K", "school": "S", "grade": "1"},
        headers=hdrs,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_cognito_editor_cannot_invite(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="editor", expires_at=FUTURE
    )
    store.accept_invitation(GUEST, invite_code)
    c = _cognito_client(store, GUEST)
    hdrs = {**_auth(), "X-SchoolSift-Household": h.id}
    r = c.post(
        "/v1/invitations",
        json={"email": "x@example.com", "role": "viewer"},
        headers=hdrs,
    )
    assert r.status_code == 403
    assert c.get("/v1/invitations", headers=hdrs).status_code == 403


def test_cognito_non_member_forbidden(store):
    h = _household(store)
    c = _cognito_client(store, GUEST)
    r = c.get(
        "/v1/messages",
        headers={**_auth(), "X-SchoolSift-Household": h.id},
    )
    assert r.status_code == 403


def test_invite_flow_via_api(store):
    _household(store)
    owner_client = _cognito_client(store, OWNER)
    boot = owner_client.get("/v1/bootstrap", headers=_auth()).json()
    hid = boot["active_household_id"]
    hdrs = {**_auth(), "X-SchoolSift-Household": hid}
    r = owner_client.post(
        "/v1/invitations",
        json={"email": "Guest@Example.com", "role": "editor"},
        headers=hdrs,
    )
    assert r.status_code == 200
    invite_code = r.json()["token"]
    assert r.json()["invitation"]["email"] == "guest@example.com"
    listed = owner_client.get("/v1/invitations", headers=hdrs).json()
    assert all("token" not in i for i in listed)
    guest_client = _cognito_client(store, GUEST)
    r2 = guest_client.post(
        "/v1/invitations/accept", json={"token": invite_code}, headers=_auth()
    )
    assert r2.status_code == 200
    assert r2.json()["membership"]["household_id"] == hid


def test_invite_owner_role_rejected(store):
    _household(store)
    c = _cognito_client(store, OWNER)
    r = c.post(
        "/v1/invitations",
        json={"email": "x@example.com", "role": "owner"},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_owner_only_member_management(store):
    h = _household(store)
    _, invite_code = store.create_invitation(
        h.id, email="guest@example.com", role="editor", expires_at=FUTURE
    )
    store.accept_invitation(GUEST, invite_code)
    guest = _cognito_client(store, GUEST)
    hdrs = {**_auth(), "X-SchoolSift-Household": h.id}
    r = guest.delete(f"/v1/members/{OWNER.user_id}", headers=hdrs)
    assert r.status_code == 403
    owner = _cognito_client(store, OWNER)
    ohdrs = {**_auth(), "X-SchoolSift-Household": h.id}
    r2 = owner.delete(f"/v1/members/{GUEST.user_id}", headers=ohdrs)
    assert r2.status_code == 204
    r3 = owner.delete(f"/v1/members/{OWNER.user_id}", headers=ohdrs)
    assert r3.status_code == 409


def test_multi_membership_requires_header(store):
    _household(store)
    store.create_household_for_owner(OWNER, name="H2", timezone="UTC")
    c = _cognito_client(store, OWNER)
    boot = c.get("/v1/bootstrap", headers=_auth()).json()
    assert boot["household"] is None
    assert len(boot["memberships"]) == 2
    r = c.get("/v1/messages", headers=_auth())
    assert r.status_code == 409


def test_local_mode_unchanged(store):
    settings = Settings(
        database_path=store.path,
        allowed_origins=("http://localhost:3210",),
    )
    c = TestClient(create_app(settings=settings, store=store))
    r = c.post("/v1/household", json={"name": "H", "timezone": "UTC"})
    assert r.status_code == 200
    boot = c.get("/v1/bootstrap").json()
    assert boot["household"]["name"] == "H"
    assert boot["memberships"][0]["user_id"] == LOCAL_PRINCIPAL.user_id
