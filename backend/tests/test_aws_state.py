import base64
import hashlib
import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from schoolsift.aws_state import (
    ClaimAction,
    DeleteAction,
    DynamoStateRepository,
    PutAction,
    StateItem,
    claim_key,
)
from schoolsift.errors import BadRequestError, ConflictError, PersistenceError

SIGNING_KEY = b"k" * 32


def _client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "raw aws"}}, op)


def _attr_s(value: str) -> dict[str, Any]:
    return {"S": value}


def _item(
    household: str = "hh-1",
    etype: str = "message",
    eid: str = "m-1",
    version: int = 1,
) -> StateItem:
    return StateItem(
        household_id=household,
        entity_type=etype,  # type: ignore[arg-type]
        entity_id=eid,
        version=version,
        data={"field": "private-value"},
        expires_at=None,
    )


def _stored(**over: Any) -> dict[str, Any]:
    base = {
        "PK": _attr_s("HOUSEHOLD#hh-1"),
        "SK": _attr_s("MESSAGE#m-1"),
        "version": {"N": "1"},
        "data": {"M": {"field": _attr_s("private-value")}},
    }
    base.update(over)
    return base


class FakeDynamo:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.transact_calls: list[dict[str, Any]] = []
        self.get_response: dict[str, Any] = {}
        self.query_response: dict[str, Any] = {"Items": []}
        self.error: ClientError | None = None

    def get_item(self, **kw: Any) -> dict[str, Any]:
        self.get_calls.append(kw)
        if self.error:
            raise self.error
        return self.get_response

    def put_item(self, **kw: Any) -> dict[str, Any]:
        self.put_calls.append(kw)
        if self.error:
            raise self.error
        return {}

    def query(self, **kw: Any) -> dict[str, Any]:
        self.query_calls.append(kw)
        if self.error:
            raise self.error
        return self.query_response

    def transact_write_items(self, **kw: Any) -> dict[str, Any]:
        self.transact_calls.append(kw)
        if self.error:
            raise self.error
        return {}


def _repo(fake: FakeDynamo) -> DynamoStateRepository:
    return DynamoStateRepository(
        table_name="state-table",
        client=fake,
        cursor_signing_key=SIGNING_KEY,
    )


def test_signing_key_required():
    with pytest.raises(ValueError, match="32 bytes"):
        DynamoStateRepository(
            table_name="t", client=FakeDynamo(), cursor_signing_key=b"short"
        )


class TestGet:
    def test_exact_key(self):
        fake = FakeDynamo()
        fake.get_response = {"Item": _stored()}
        repo = _repo(fake)
        item = repo.get("hh-1", "message", "m-1")
        assert fake.get_calls[0]["TableName"] == "state-table"
        assert fake.get_calls[0]["Key"] == {
            "PK": {"S": "HOUSEHOLD#hh-1"},
            "SK": {"S": "MESSAGE#m-1"},
        }
        assert item is not None
        assert item.household_id == "hh-1"
        assert item.entity_type == "message"
        assert item.entity_id == "m-1"
        assert item.version == 1
        assert item.data == {"field": "private-value"}

    def test_missing_returns_none(self):
        fake = FakeDynamo()
        fake.get_response = {}
        assert _repo(fake).get("hh-1", "message", "m-9") is None

    def test_error_sanitized(self):
        fake = FakeDynamo()
        fake.error = _client_error("AccessDeniedException")
        with pytest.raises(PersistenceError) as exc:
            _repo(fake).get("hh-1", "message", "m-1")
        assert "state-table" not in str(exc.value)
        assert "raw aws" not in str(exc.value)


class TestPut:
    def test_create_condition(self):
        fake = FakeDynamo()
        _repo(fake).put(_item(), expected_version=None)
        call = fake.put_calls[0]
        assert call["Item"]["PK"] == {"S": "HOUSEHOLD#hh-1"}
        assert call["Item"]["SK"] == {"S": "MESSAGE#m-1"}
        assert call["Item"]["version"] == {"N": "1"}
        assert call["ConditionExpression"] == "attribute_not_exists(#pk)"

    def test_expected_version_condition(self):
        fake = FakeDynamo()
        _repo(fake).put(_item(version=2), expected_version=1)
        call = fake.put_calls[0]
        assert call["ConditionExpression"] == "#v = :v"
        assert call["ExpressionAttributeValues"] == {":v": {"N": "1"}}

    def test_conditional_failure_maps_conflict(self):
        fake = FakeDynamo()
        fake.error = _client_error("ConditionalCheckFailedException")
        with pytest.raises(ConflictError):
            _repo(fake).put(_item(), expected_version=1)

    def test_other_error_sanitized(self):
        fake = FakeDynamo()
        fake.error = _client_error("ProvisionedThroughputExceededException")
        with pytest.raises(PersistenceError) as exc:
            _repo(fake).put(_item())
        assert "private-value" not in str(exc.value)


class TestQuery:
    def test_exact_request_and_page(self):
        fake = FakeDynamo()
        fake.query_response = {
            "Items": [_stored()],
            "LastEvaluatedKey": {
                "PK": {"S": "HOUSEHOLD#hh-1"},
                "SK": {"S": "MESSAGE#m-1"},
            },
        }
        repo = _repo(fake)
        page = repo.query("hh-1", "message", limit=10)
        call = fake.query_calls[0]
        assert call["ExpressionAttributeValues"] == {
            ":pk": {"S": "HOUSEHOLD#hh-1"},
            ":sk": {"S": "MESSAGE#"},
        }
        assert call["Limit"] == 10
        assert len(page.items) == 1
        assert page.cursor is not None

        fake.query_response = {"Items": []}
        page2 = repo.query("hh-1", "message", limit=10, cursor=page.cursor)
        assert fake.query_calls[1]["ExclusiveStartKey"] == {
            "PK": {"S": "HOUSEHOLD#hh-1"},
            "SK": {"S": "MESSAGE#m-1"},
        }
        assert page2.cursor is None

    def _valid_cursor(self, repo: DynamoStateRepository) -> str:
        return repo._encode_cursor(
            {
                "PK": {"S": "HOUSEHOLD#hh-1"},
                "SK": {"S": "MESSAGE#m-1"},
            },
            "hh-1",
            "message",
        )

    def test_one_bit_tamper_rejected(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        cursor = self._valid_cursor(repo)
        raw = bytearray(
            base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
        )
        raw[0] ^= 1
        tampered = base64.urlsafe_b64encode(bytes(raw)).decode()
        with pytest.raises(BadRequestError):
            repo.query("hh-1", "message", cursor=tampered)
        assert fake.query_calls == []

    def test_forged_cursor_rejected(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        forged_payload = json.dumps(
            {
                "hh": "hh-1",
                "et": "message",
                "key": {
                    "PK": {"S": "HOUSEHOLD#hh-1"},
                    "SK": {"S": "MESSAGE#forged"},
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        forged = base64.urlsafe_b64encode(forged_payload + b"x" * 32).decode()
        with pytest.raises(BadRequestError):
            repo.query("hh-1", "message", cursor=forged)
        assert fake.query_calls == []

    def test_cursor_wrong_tenant_rejected(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        cursor = self._valid_cursor(repo)
        with pytest.raises(BadRequestError):
            repo.query("hh-2", "message", cursor=cursor)
        assert fake.query_calls == []

    def test_cursor_wrong_type_rejected(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        cursor = self._valid_cursor(repo)
        with pytest.raises(BadRequestError):
            repo.query("hh-1", "packet", cursor=cursor)
        assert fake.query_calls == []

    def test_malformed_cursor_rejected(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        with pytest.raises(BadRequestError):
            repo.query("hh-1", "message", cursor="!!!not-base64!!!")
        with pytest.raises(BadRequestError):
            repo.query(
                "hh-1", "message", cursor=base64.urlsafe_b64encode(b"{}").decode()
            )
        assert fake.query_calls == []

    def test_cursor_error_does_not_leak_key(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        with pytest.raises(BadRequestError) as exc:
            repo.query("hh-1", "message", cursor="a" * 64)
        assert SIGNING_KEY.decode() not in str(exc.value)


class TestTransact:
    def test_put_and_delete_actions(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        put = PutAction(item=_item(version=2), expected_version=1)
        delete = DeleteAction(
            household_id="hh-1",
            entity_type="message",
            entity_id="m-old",
            expected_version=3,
        )
        repo.transact([put, delete])
        items = fake.transact_calls[0]["TransactItems"]
        assert items[0]["Put"]["ConditionExpression"] == "#v = :v"
        assert items[0]["Put"]["ExpressionAttributeValues"] == {":v": {"N": "1"}}
        assert items[1]["Delete"]["Key"]["SK"] == {"S": "MESSAGE#m-old"}
        assert items[1]["Delete"]["ExpressionAttributeValues"] == {":v": {"N": "3"}}

    def test_transaction_canceled_maps_conflict(self):
        fake = FakeDynamo()
        fake.error = _client_error("TransactionCanceledException")
        with pytest.raises(ConflictError):
            _repo(fake).transact([PutAction(item=_item())])

    def test_other_error_sanitized(self):
        fake = FakeDynamo()
        fake.error = _client_error("InternalServerError")
        with pytest.raises(PersistenceError) as exc:
            _repo(fake).transact([PutAction(item=_item())])
        assert "private-value" not in str(exc.value)


class TestClaims:
    def test_provider_claim_exact_request(self):
        fake = FakeDynamo()
        repo = _repo(fake)
        claim = repo.provider_claim(
            household_id="hh-1",
            connection_id="conn-1",
            provider="gmail",
            provider_subject="sub-9",
        )
        assert isinstance(claim, ClaimAction)
        put = PutAction(
            item=_item(etype="connection", eid="conn-1"),
            gsi1pk="CONNECTION#conn-1",
            gsi1sk="HOUSEHOLD#hh-1",
        )
        repo.transact([claim, put])
        items = fake.transact_calls[0]["TransactItems"]
        claim_item = items[0]["Put"]["Item"]
        expected_key = claim_key("provider-subject:gmail", "sub-9")
        assert claim_item["PK"] == {"S": f"CLAIM#{expected_key}"}
        assert claim_item["SK"] == {"S": "CLAIM"}
        assert items[0]["Put"]["ConditionExpression"] == ("attribute_not_exists(#pk)")
        data = claim_item["data"]["M"]
        assert data["household_id"] == {"S": "hh-1"}
        assert data["entity_id"] == {"S": "conn-1"}
        serialized = json.dumps(items, default=str)
        assert "sub-9" not in serialized
        assert items[1]["Put"]["Item"]["GSI1PK"] == {"S": "CONNECTION#conn-1"}

    def test_same_claim_same_key_across_households(self):
        repo = _repo(FakeDynamo())
        a = repo.provider_claim(
            household_id="hh-1",
            connection_id="conn-1",
            provider="gmail",
            provider_subject="sub-9",
        )
        b = repo.provider_claim(
            household_id="hh-2",
            connection_id="conn-2",
            provider="gmail",
            provider_subject="sub-9",
        )
        assert a.claim_key == b.claim_key

    def test_different_subject_different_key(self):
        repo = _repo(FakeDynamo())
        a = repo.provider_claim(
            household_id="hh-1",
            connection_id="conn-1",
            provider="gmail",
            provider_subject="sub-9",
        )
        b = repo.provider_claim(
            household_id="hh-1",
            connection_id="conn-1",
            provider="gmail",
            provider_subject="sub-10",
        )
        c = repo.provider_claim(
            household_id="hh-1",
            connection_id="conn-1",
            provider="outlook",
            provider_subject="sub-9",
        )
        assert a.claim_key != b.claim_key
        assert a.claim_key != c.claim_key

    def test_claim_collision_maps_conflict(self):
        fake = FakeDynamo()
        fake.error = _client_error("TransactionCanceledException")
        repo = _repo(fake)
        claim = repo.provider_claim(
            household_id="hh-2",
            connection_id="conn-2",
            provider="gmail",
            provider_subject="sub-9",
        )
        with pytest.raises(ConflictError):
            repo.transact([claim])

    def test_claim_key_is_sha256_of_namespace_value(self):
        expected = hashlib.sha256(b"ns:value").hexdigest()
        assert claim_key("ns", "value") == expected


def test_state_item_bounds():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StateItem(
            household_id="hh",
            entity_type="widget",  # type: ignore[arg-type]
            entity_id="e",
            version=1,
            data={},
            expires_at=None,
        )
    with pytest.raises(ValidationError):
        StateItem(
            household_id="hh",
            entity_type="message",
            entity_id="e",
            version=0,
            data={},
            expires_at=None,
        )
