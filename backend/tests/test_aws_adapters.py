import hashlib
import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from schoolsift.aws_adapters import (
    S3ContentStore,
    SecretsManagerCredentialVault,
    SQSIngestionQueue,
)
from schoolsift.credentials import OAuthCredentials
from schoolsift.errors import ContentError, QueueError, VaultError
from schoolsift.notifications import IngestionEvent


def _client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "raw aws"}}, op)


class FakeS3:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.error: ClientError | None = None

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self.put_calls.append(kw)
        if self.error:
            raise self.error
        self.objects[kw["Key"]] = (kw["Body"], kw["Metadata"])
        return {}

    def get_object(self, **kw: Any) -> dict[str, Any]:
        self.get_calls.append(kw)
        if self.error:
            raise self.error
        if kw["Key"] not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        body, meta = self.objects[kw["Key"]]
        return {"Body": io.BytesIO(body), "Metadata": meta}

    def delete_object(self, **kw: Any) -> dict[str, Any]:
        self.delete_calls.append(kw)
        if self.error:
            raise self.error
        self.objects.pop(kw["Key"], None)
        return {}


class FakeSecretsManager:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.secrets: dict[str, str] = {}
        self.create_error: ClientError | None = None
        self.put_error: ClientError | None = None
        self.get_error: ClientError | None = None

    def create_secret(self, **kw: Any) -> dict[str, Any]:
        self.create_calls.append(kw)
        if self.create_error:
            raise self.create_error
        if kw["Name"] in self.secrets:
            raise _client_error("ResourceExistsException", "CreateSecret")
        self.secrets[kw["Name"]] = kw["SecretString"]
        return {"ARN": "arn:test"}

    def put_secret_value(self, **kw: Any) -> dict[str, Any]:
        self.put_calls.append(kw)
        if self.put_error:
            raise self.put_error
        self.secrets[kw["SecretId"]] = kw["SecretString"]
        return {}

    def get_secret_value(self, **kw: Any) -> dict[str, Any]:
        self.get_calls.append(kw)
        if self.get_error:
            raise self.get_error
        if kw["SecretId"] not in self.secrets:
            raise _client_error("ResourceNotFoundException", "GetSecretValue")
        return {"SecretString": self.secrets[kw["SecretId"]]}


class FakeSQS:
    def __init__(self) -> None:
        self.send_calls: list[dict[str, Any]] = []
        self.error: ClientError | None = None

    def send_message(self, **kw: Any) -> dict[str, Any]:
        self.send_calls.append(kw)
        if self.error:
            raise self.error
        return {"MessageId": "m-1"}


def _credentials() -> OAuthCredentials:
    return OAuthCredentials(
        access_token=SecretStr("tok-a"),
        refresh_token=SecretStr("tok-r"),
        expires_at=datetime.now(UTC),
        scopes=["mail.read"],
    )


def _event() -> IngestionEvent:
    return IngestionEvent(
        id="evt-1",
        provider="gmail",
        household_id="hh-1",
        connection_id="conn-1",
        kind="mail_changed",
        provider_cursor="12345",
        received_at=datetime.now(UTC),
    )


class TestS3ContentStore:
    def _store(self, fake: FakeS3) -> S3ContentStore:
        return S3ContentStore(
            bucket="content-bucket",
            kms_key_arn="arn:aws:kms:us-east-1:1:key/k",
            prefix="raw",
            client=fake,
        )

    def test_put_exact_request(self):
        fake = FakeS3()
        store = self._store(fake)
        ref = store.put("hh-1", b"payload-bytes")
        assert fake.put_calls[0]["Bucket"] == "content-bucket"
        assert fake.put_calls[0]["Key"] == f"raw/hh-1/{ref}"
        assert fake.put_calls[0]["Body"] == b"payload-bytes"
        assert fake.put_calls[0]["ServerSideEncryption"] == "aws:kms"
        assert fake.put_calls[0]["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:1:key/k"
        assert fake.put_calls[0]["ContentType"] == "application/octet-stream"
        assert (
            fake.put_calls[0]["Metadata"]["sha256"]
            == hashlib.sha256(b"payload-bytes").hexdigest()
        )

    def test_get_roundtrip_and_delete(self):
        fake = FakeS3()
        store = self._store(fake)
        ref = store.put("hh-1", b"content-here")
        assert store.get("hh-1", ref) == b"content-here"
        store.delete("hh-1", ref)
        assert fake.delete_calls[0]["Key"] == f"raw/hh-1/{ref}"
        with pytest.raises(KeyError):
            store.get("hh-1", ref)

    def test_get_detects_corruption(self):
        fake = FakeS3()
        store = self._store(fake)
        ref = store.put("hh-1", b"original")
        key = f"raw/hh-1/{ref}"
        _, meta = fake.objects[key]
        fake.objects[key] = (b"tampered", meta)
        with pytest.raises(ContentError) as exc:
            store.get("hh-1", ref)
        assert "tampered" not in str(exc.value)

    def test_missing_object_maps_keyerror(self):
        fake = FakeS3()
        store = self._store(fake)
        with pytest.raises(KeyError):
            store.get("hh-1", "missingref")

    def test_other_client_error_sanitized(self):
        fake = FakeS3()
        fake.error = _client_error("AccessDenied", "GetObject")
        store = self._store(fake)
        with pytest.raises(ContentError) as exc:
            store.get("hh-1", "ref-1")
        msg = str(exc.value)
        assert "content-bucket" not in msg
        assert "raw/hh-1" not in msg
        assert "raw aws" not in msg

    @pytest.mark.parametrize("bad", ["../evil", "a/b", "", "a..b/c", "white space"])
    def test_unsafe_household_id_rejected(self, bad: str):
        fake = FakeS3()
        store = self._store(fake)
        with pytest.raises(ContentError):
            store.put(bad, b"x")
        with pytest.raises(ContentError):
            store.get(bad, "ref-1")
        with pytest.raises(ContentError):
            store.delete(bad, "ref-1")
        assert fake.put_calls == []
        assert fake.get_calls == []
        assert fake.delete_calls == []

    def test_unsafe_ref_rejected(self):
        fake = FakeS3()
        store = self._store(fake)
        with pytest.raises(ContentError):
            store.get("hh-1", "../escape")
        assert fake.get_calls == []


class TestSecretsManagerCredentialVault:
    def _vault(self, fake: FakeSecretsManager) -> SecretsManagerCredentialVault:
        return SecretsManagerCredentialVault(
            "schoolsift/prod",
            kms_key_arn="arn:aws:kms:us-east-1:1:key/k",
            client=fake,
        )

    def test_put_creates_with_kms_and_tags(self):
        fake = FakeSecretsManager()
        vault = self._vault(fake)
        vault.put("conn-1", _credentials())
        call = fake.create_calls[0]
        assert call["Name"] == "schoolsift/prod/connections/conn-1"
        assert call["KmsKeyId"] == "arn:aws:kms:us-east-1:1:key/k"
        tag_values = json.dumps(call["Tags"])
        assert "tok-" not in tag_values
        stored = json.loads(call["SecretString"])
        parsed = OAuthCredentials.model_validate(stored)
        assert parsed.access_token.get_secret_value() == "tok-a"
        assert parsed.scopes == ("mail.read",)

    def test_put_existing_secret_updates(self):
        fake = FakeSecretsManager()
        vault = self._vault(fake)
        vault.put("conn-1", _credentials())
        vault.put("conn-1", _credentials())
        assert len(fake.create_calls) == 2
        assert len(fake.put_calls) == 1
        assert fake.put_calls[0]["SecretId"] == ("schoolsift/prod/connections/conn-1")

    def test_get_roundtrip(self):
        fake = FakeSecretsManager()
        vault = self._vault(fake)
        creds = _credentials()
        vault.put("conn-1", creds)
        loaded = vault.get("conn-1")
        assert loaded.access_token.get_secret_value() == "tok-a"
        assert loaded.scopes == ("mail.read",)

    def test_get_missing_secret_raises(self):
        fake = FakeSecretsManager()
        vault = self._vault(fake)
        with pytest.raises(VaultError) as exc:
            vault.get("conn-9")
        assert "tok-" not in str(exc.value)

    def test_get_malformed_raises(self):
        fake = FakeSecretsManager()
        fake.secrets["schoolsift/prod/connections/conn-1"] = "not-json"
        vault = self._vault(fake)
        with pytest.raises(VaultError):
            vault.get("conn-1")

    def test_get_missing_secretstring_raises(self):
        class NoString(FakeSecretsManager):
            def get_secret_value(self, **kw: Any) -> dict[str, Any]:
                return {"SecretBinary": b"x"}

        vault = SecretsManagerCredentialVault(
            "schoolsift/prod",
            kms_key_arn="arn:k",
            client=NoString(),
        )
        with pytest.raises(VaultError):
            vault.get("conn-1")

    def test_revoked_secret_raises(self):
        fake = FakeSecretsManager()
        fake.secrets["schoolsift/prod/connections/conn-1"] = '{"revoked":true}'
        vault = self._vault(fake)
        with pytest.raises(VaultError):
            vault.get("conn-1")

    def test_delete_writes_revoked_marker(self):
        fake = FakeSecretsManager()
        vault = self._vault(fake)
        vault.put("conn-1", _credentials())
        vault.delete("conn-1")
        assert json.loads(fake.put_calls[-1]["SecretString"]) == {"revoked": True}
        with pytest.raises(VaultError):
            vault.get("conn-1")

    def test_create_failure_sanitized(self):
        fake = FakeSecretsManager()
        fake.create_error = _client_error("AccessDeniedException")
        vault = self._vault(fake)
        with pytest.raises(VaultError) as exc:
            vault.put("conn-1", _credentials())
        msg = str(exc.value)
        assert "tok-" not in msg
        assert "raw aws" not in msg

    def test_invalid_connection_id_rejected(self):
        fake = FakeSecretsManager()
        vault = self._vault(fake)
        with pytest.raises(VaultError):
            vault.put("../bad", _credentials())
        assert fake.create_calls == []


class TestSQSIngestionQueue:
    def test_requires_fifo_url(self):
        with pytest.raises(QueueError):
            SQSIngestionQueue(
                queue_url="https://sqs.us-east-1.amazonaws.com/1/standard",
                client=FakeSQS(),
            )

    def test_enqueue_exact_request(self):
        fake = FakeSQS()
        queue = SQSIngestionQueue(
            queue_url="https://sqs.us-east-1.amazonaws.com/1/q.fifo",
            client=fake,
        )
        event = _event()
        assert queue.enqueue(event, dedupe_key="gmail:m-1") is True
        call = fake.send_calls[0]
        assert call["QueueUrl"].endswith("q.fifo")
        assert call["MessageBody"] == event.model_dump_json()
        assert call["MessageGroupId"] == "hh-1"
        assert (
            call["MessageDeduplicationId"] == hashlib.sha256(b"gmail:m-1").hexdigest()
        )
        assert "gmail:m-1" not in call["MessageBody"]

    def test_error_sanitized(self):
        fake = FakeSQS()
        fake.error = _client_error("AccessDenied")
        queue = SQSIngestionQueue(
            queue_url="https://sqs.us-east-1.amazonaws.com/1/q.fifo",
            client=fake,
        )
        with pytest.raises(QueueError) as exc:
            queue.enqueue(_event(), dedupe_key="gmail:m-1")
        msg = str(exc.value)
        assert "gmail:m-1" not in msg
        assert "raw aws" not in msg


class TestPrefixValidation:
    @pytest.mark.parametrize(
        "bad", ["", "../x", "a//b", "a/./b", "a/../b", ".hidden", "a b/c"]
    )
    def test_s3_prefix_rejected(self, bad: str):
        with pytest.raises(ContentError):
            S3ContentStore(
                bucket="b",
                kms_key_arn="arn:k",
                prefix=bad,
                client=FakeS3(),
            )

    @pytest.mark.parametrize("good", ["raw", "a/b", "x-y_z/9"])
    def test_s3_prefix_accepted(self, good: str):
        S3ContentStore(bucket="b", kms_key_arn="arn:k", prefix=good, client=FakeS3())

    @pytest.mark.parametrize("bad", ["", "../x", "a//b", "a/../b", ".x", "seg ment"])
    def test_secret_prefix_rejected(self, bad: str):
        with pytest.raises(VaultError):
            SecretsManagerCredentialVault(
                bad, kms_key_arn="arn:k", client=FakeSecretsManager()
            )

    @pytest.mark.parametrize("good", ["schoolsift", "schoolsift/prod"])
    def test_secret_prefix_accepted(self, good: str):
        SecretsManagerCredentialVault(
            good, kms_key_arn="arn:k", client=FakeSecretsManager()
        )


class TestSQSExecutionQueue:
    def test_requires_fifo(self):
        from schoolsift.aws_adapters import SQSExecutionQueue
        from schoolsift.errors import QueueError

        with pytest.raises(QueueError):
            SQSExecutionQueue(
                queue_url="https://sqs.example/standard", client=FakeSQS()
            )

    def test_enqueue_sends_fifo_message(self):
        from schoolsift.aws_adapters import SQSExecutionQueue
        from schoolsift.models import ExecutionCommand

        client = FakeSQS()
        queue = SQSExecutionQueue(
            queue_url="https://sqs.example/exec.fifo", client=client
        )
        ok = queue.enqueue(
            ExecutionCommand(execution_id="e1", household_id="hh-1"),
            dedupe_key="idem-1",
        )
        assert ok is True
        call = client.send_calls[0]
        assert call["QueueUrl"] == "https://sqs.example/exec.fifo"
        assert call["MessageGroupId"] == "hh-1"
        assert call["MessageDeduplicationId"] == hashlib.sha256(b"idem-1").hexdigest()
        import json

        body = json.loads(call["MessageBody"])
        assert body == {"execution_id": "e1", "household_id": "hh-1"}

    def test_enqueue_failure_raises_queue_error(self):
        from schoolsift.aws_adapters import SQSExecutionQueue
        from schoolsift.errors import QueueError
        from schoolsift.models import ExecutionCommand

        client = FakeSQS()
        client.error = _client_error("ServiceUnavailable", "send")
        queue = SQSExecutionQueue(
            queue_url="https://sqs.example/exec.fifo", client=client
        )
        with pytest.raises(QueueError):
            queue.enqueue(
                ExecutionCommand(execution_id="e1", household_id="hh-1"),
                dedupe_key="k",
            )
