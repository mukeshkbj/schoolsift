from __future__ import annotations

from typing import Any, Protocol

import jwt
from pydantic import BaseModel, ConfigDict, Field

from .errors import AuthenticationError


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)


LOCAL_PRINCIPAL = Principal(user_id="local-caregiver", email="local@schoolsift.invalid")


def normalize_email(email: str) -> str:
    return email.strip().lower()


class TokenVerifier(Protocol):
    def verify(self, bearer_token: str) -> Principal: ...


class SigningKeyProvider(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class CognitoIdTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        app_client_id: str,
        key_provider: SigningKeyProvider,
    ) -> None:
        self._issuer = issuer
        self._app_client_id = app_client_id
        self._key_provider = key_provider

    def verify(self, bearer_token: str) -> Principal:
        try:
            signing_key = self._key_provider.get_signing_key_from_jwt(bearer_token)
            claims: dict[str, Any] = jwt.decode(
                bearer_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._app_client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except Exception as e:
            raise AuthenticationError(
                "The sign-in credential could not be verified."
            ) from e
        if claims.get("token_use") != "id":
            raise AuthenticationError("The sign-in credential could not be verified.")
        subject = claims.get("sub")
        email = claims.get("email")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(email, str)
            or claims.get("email_verified") is not True
        ):
            raise AuthenticationError("The sign-in credential could not be verified.")
        return Principal(user_id=subject, email=normalize_email(email))
