from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from schoolsift.credentials import (
    KeyringCredentialVault,
    OAuthCredentials,
)


class InMemoryVault:
    def __init__(self) -> None:
        self.store: dict[str, OAuthCredentials] = {}

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        self.store[connection_id] = credentials

    def get(self, connection_id: str) -> OAuthCredentials:
        return self.store[connection_id]

    def delete(self, connection_id: str) -> None:
        self.store.pop(connection_id, None)


def creds() -> OAuthCredentials:
    return OAuthCredentials(
        access_token=SecretStr("at"),
        refresh_token=SecretStr("rt"),
        expires_at=datetime(2026, 9, 20, tzinfo=UTC),
        scopes=("openid", "email"),
    )


def test_inmemory_vault_roundtrip():
    v = InMemoryVault()
    v.put("c1", creds())
    assert v.get("c1").access_token.get_secret_value() == "at"
    v.delete("c1")
    with pytest.raises(KeyError):
        v.get("c1")


def test_keyring_vault_roundtrip_via_stubbed_backend(monkeypatch):
    backing: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "keyring.set_password",
        lambda svc, user, pw: backing.__setitem__((svc, user), pw),
    )
    monkeypatch.setattr(
        "keyring.get_password", lambda svc, user: backing.get((svc, user))
    )
    monkeypatch.setattr(
        "keyring.delete_password",
        lambda svc, user: backing.pop((svc, user), None),
    )
    vault = KeyringCredentialVault()
    vault.put("conn-9", creds())
    raw = backing[("schoolsift", "connection:conn-9")]
    assert "at" in raw and "rt" in raw
    got = vault.get("conn-9")
    assert got.access_token.get_secret_value() == "at"
    assert got.refresh_token is not None
    assert got.scopes == ("openid", "email")
    vault.delete("conn-9")
    with pytest.raises(KeyError):
        vault.get("conn-9")
