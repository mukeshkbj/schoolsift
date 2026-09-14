from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Protocol

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from .credentials import OAuthCredentials
from .errors import ContentError, QueueError, VaultError
from .models import ExecutionCommand
from .notifications import IngestionEvent

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


class _S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...
    def get_object(self, **kwargs: Any) -> Any: ...
    def delete_object(self, **kwargs: Any) -> Any: ...


class _SecretsManagerClient(Protocol):
    def create_secret(self, **kwargs: Any) -> Any: ...
    def put_secret_value(self, **kwargs: Any) -> Any: ...
    def get_secret_value(self, **kwargs: Any) -> Any: ...


class _SQSClient(Protocol):
    def send_message(self, **kwargs: Any) -> Any: ...


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _safe_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ContentError("Invalid content identifier.")
    return value


def _valid_prefix(prefix: str) -> bool:
    segments = prefix.split("/")
    return all(_SAFE_ID.fullmatch(segment) for segment in segments)


class S3ContentStore:
    def __init__(
        self,
        *,
        bucket: str,
        kms_key_arn: str,
        prefix: str = "raw",
        client: _S3Client,
    ) -> None:
        if not _valid_prefix(prefix):
            raise ContentError("Invalid content prefix.")
        self._bucket = bucket
        self._kms_key_arn = kms_key_arn
        self._prefix = prefix
        self._client = client

    def _key(self, household_id: str, ref: str) -> str:
        return f"{self._prefix}/{_safe_id(household_id)}/{_safe_id(ref)}"

    def put(self, household_id: str, content: bytes) -> str:
        ref = secrets.token_hex(24)
        key = self._key(household_id, ref)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self._kms_key_arn,
                ContentType="application/octet-stream",
                Metadata={
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "household": household_id,
                },
            )
        except ClientError as e:
            raise ContentError("Could not store content.") from e
        return ref

    def get(self, household_id: str, ref: str) -> bytes:
        key = self._key(household_id, ref)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _error_code(e) in ("NoSuchKey", "NoSuchBucket", "404"):
                raise KeyError(ref) from e
            raise ContentError("Could not read content.") from e
        body: bytes = response["Body"].read()
        stored = str(response.get("Metadata", {}).get("sha256", ""))
        if not hmac.compare_digest(stored, hashlib.sha256(body).hexdigest()):
            raise ContentError("Content integrity check failed.")
        return body

    def delete(self, household_id: str, ref: str) -> None:
        key = self._key(household_id, ref)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            raise ContentError("Could not delete content.") from e


def _credentials_json(credentials: OAuthCredentials) -> str:
    return json.dumps(
        {
            "access_token": credentials.access_token.get_secret_value(),
            "refresh_token": (
                credentials.refresh_token.get_secret_value()
                if credentials.refresh_token is not None
                else None
            ),
            "expires_at": (
                credentials.expires_at.isoformat()
                if credentials.expires_at is not None
                else None
            ),
            "scopes": list(credentials.scopes),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


class SecretsManagerCredentialVault:
    def __init__(
        self, secret_prefix: str, *, kms_key_arn: str, client: _SecretsManagerClient
    ) -> None:
        if not _valid_prefix(secret_prefix):
            raise VaultError("Invalid secret prefix.")
        self._prefix = secret_prefix
        self._kms_key_arn = kms_key_arn
        self._client = client

    def _name(self, connection_id: str) -> str:
        if not _SAFE_ID.fullmatch(connection_id):
            raise VaultError("Invalid connection identifier.")
        return f"{self._prefix}/connections/{connection_id}"

    def put(self, connection_id: str, credentials: OAuthCredentials) -> None:
        name = self._name(connection_id)
        payload = _credentials_json(credentials)
        try:
            self._client.create_secret(
                Name=name,
                SecretString=payload,
                KmsKeyId=self._kms_key_arn,
                Tags=[{"Key": "application", "Value": "schoolsift"}],
            )
        except ClientError as e:
            if _error_code(e) == "ResourceExistsException":
                try:
                    self._client.put_secret_value(SecretId=name, SecretString=payload)
                except ClientError as inner:
                    raise VaultError("Could not store credentials.") from inner
            else:
                raise VaultError("Could not store credentials.") from e

    def get(self, connection_id: str) -> OAuthCredentials:
        name = self._name(connection_id)
        try:
            response = self._client.get_secret_value(SecretId=name)
        except ClientError as e:
            raise VaultError("Credentials are unavailable.") from e
        secret = response.get("SecretString")
        if not isinstance(secret, str):
            raise VaultError("Credentials are unavailable.")
        try:
            parsed = json.loads(secret)
        except json.JSONDecodeError as e:
            raise VaultError("Credentials are unavailable.") from e
        if not isinstance(parsed, dict) or parsed.get("revoked") is True:
            raise VaultError("Credentials are unavailable.")
        try:
            return OAuthCredentials.model_validate(parsed)
        except Exception as e:
            raise VaultError("Credentials are unavailable.") from e

    def delete(self, connection_id: str) -> None:
        name = self._name(connection_id)
        try:
            self._client.put_secret_value(
                SecretId=name, SecretString='{"revoked":true}'
            )
        except ClientError as e:
            raise VaultError("Could not revoke credentials.") from e


class SQSIngestionQueue:
    def __init__(self, *, queue_url: str, client: _SQSClient) -> None:
        if not queue_url.endswith(".fifo"):
            raise QueueError("Ingestion queue must be a FIFO queue.")
        self._queue_url = queue_url
        self._client = client

    def enqueue(self, event: IngestionEvent, *, dedupe_key: str) -> bool:
        try:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=event.model_dump_json(),
                MessageGroupId=event.household_id,
                MessageDeduplicationId=hashlib.sha256(dedupe_key.encode()).hexdigest(),
            )
        except ClientError as e:
            raise QueueError("Could not queue the ingestion event.") from e
        return True


class SQSExecutionQueue:
    def __init__(self, *, queue_url: str, client: _SQSClient) -> None:
        if not queue_url.endswith(".fifo"):
            raise QueueError("Execution queue must be a FIFO queue.")
        self._queue_url = queue_url
        self._client = client

    def enqueue(self, command: ExecutionCommand, *, dedupe_key: str) -> bool:
        try:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=command.model_dump_json(),
                MessageGroupId=command.household_id,
                MessageDeduplicationId=hashlib.sha256(dedupe_key.encode()).hexdigest(),
            )
        except ClientError as e:
            raise QueueError("Could not queue the execution.") from e
        return True
