from __future__ import annotations

import contextlib
import json
from datetime import datetime
from typing import Protocol

import keyring
from pydantic import BaseModel, SecretStr


class OAuthCredentials(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_at: datetime | None
    scopes: tuple[str, ...]


class CredentialVault(Protocol):
    def put(self, connection_id: str, credentials: OAuthCredentials) -> None: ...

    def get(self, connection_id: str) -> OAuthCredentials: ...

    def delete(self, connection_id: str) -> None: ...


class KeyringCredentialVault:
    SERVICE = "schoolsift"

    @staticmethod
    def _key(connection_id: str) -> str:
        return f"connection:{connection_id}"

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        payload = {
            "access_token": credentials.access_token.get_secret_value(),
            "refresh_token": (
                credentials.refresh_token.get_secret_value()
                if credentials.refresh_token
                else None
            ),
            "expires_at": (
                credentials.expires_at.isoformat() if credentials.expires_at else None
            ),
            "scopes": list(credentials.scopes),
        }
        keyring.set_password(
            self.SERVICE, self._key(connection_id), json.dumps(payload)
        )

    def get(self, connection_id: str) -> OAuthCredentials:
        raw = keyring.get_password(self.SERVICE, self._key(connection_id))
        if raw is None:
            raise KeyError(f"No credentials for {connection_id}")
        return OAuthCredentials.model_validate(json.loads(raw))

    def delete(self, connection_id: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self.SERVICE, self._key(connection_id))
