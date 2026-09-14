from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from schoolsift.errors import AuthenticationError
from schoolsift.identity import CognitoIdTokenVerifier, Principal

ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/pool"
CLIENT_ID = "client-id"


def _keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem, pub


class StaticKeyProvider:
    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, token: str):
        return type("K", (), {"key": self._key})()


def _sign(private_pem, **overrides) -> str:
    claims = {
        "sub": "user-1",
        "email": "Parent@Example.com",
        "email_verified": True,
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256")


@pytest.fixture()
def keys():
    return _keypair()


@pytest.fixture()
def verifier(keys):
    _, pub = keys
    return CognitoIdTokenVerifier(
        issuer=ISSUER,
        app_client_id=CLIENT_ID,
        key_provider=StaticKeyProvider(pub),
    )


def test_valid_token_returns_normalized_principal(verifier, keys):
    private, _ = keys
    principal = verifier.verify(_sign(private))
    assert principal == Principal(user_id="user-1", email="parent@example.com")


def test_bad_signature_rejected(keys):
    private, _ = keys
    _, other_pub = _keypair()
    v = CognitoIdTokenVerifier(
        issuer=ISSUER,
        app_client_id=CLIENT_ID,
        key_provider=StaticKeyProvider(other_pub),
    )
    with pytest.raises(AuthenticationError):
        v.verify(_sign(private))


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example"},
        {"aud": "other-client"},
        {"exp": int(time.time()) - 10},
        {"token_use": "access"},
        {"email_verified": False},
        {"sub": ""},
        "no-email",
    ],
)
def test_claim_failures_rejected(verifier, keys, overrides):
    private, _ = keys
    if overrides == "no-email":
        token = jwt.encode(
            {
                "sub": "user-1",
                "email_verified": True,
                "token_use": "id",
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "exp": int(time.time()) + 300,
            },
            private,
            algorithm="RS256",
        )
    else:
        token = _sign(private, **overrides)
    with pytest.raises(AuthenticationError):
        verifier.verify(token)


def test_malformed_token_rejected(verifier):
    with pytest.raises(AuthenticationError):
        verifier.verify("not-a-jwt")


def test_error_does_not_leak_claims(verifier, keys):
    private, _ = keys
    token = _sign(private, iss="https://evil.example")
    with pytest.raises(AuthenticationError) as exc:
        verifier.verify(token)
    assert "evil.example" not in str(exc.value)
    assert "user-1" not in str(exc.value)
