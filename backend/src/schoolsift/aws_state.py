from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Sequence
from typing import Any, Literal, Protocol, cast

from boto3.dynamodb.types import (  # type: ignore[import-untyped]
    TypeDeserializer,
    TypeSerializer,
)
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from .errors import BadRequestError, ConflictError, PersistenceError

EntityType = Literal[
    "household",
    "membership",
    "child",
    "connection",
    "source",
    "message",
    "document",
    "packet",
    "subscription",
    "audit",
]


class StateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_id: str = Field(min_length=1, max_length=200)
    entity_type: EntityType
    entity_id: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=1)
    data: dict[str, object] = Field(default_factory=dict)
    expires_at: int | None = None


class PutAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: StateItem
    expected_version: int | None = Field(default=None, ge=1)
    gsi1pk: str | None = None
    gsi1sk: str | None = None


class DeleteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_id: str = Field(min_length=1, max_length=200)
    entity_type: EntityType
    entity_id: str = Field(min_length=1, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class ClaimAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_id: str = Field(min_length=1, max_length=200)
    entity_type: EntityType
    entity_id: str = Field(min_length=1, max_length=500)
    namespace: str = Field(min_length=1, max_length=200)
    claim_key: str


TransactAction = PutAction | DeleteAction | ClaimAction


class StatePage(BaseModel):
    items: list[StateItem]
    cursor: str | None = None


class _DynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> Any: ...
    def put_item(self, **kwargs: Any) -> Any: ...
    def query(self, **kwargs: Any) -> Any: ...
    def transact_write_items(self, **kwargs: Any) -> Any: ...


_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _to_attr(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], _serializer.serialize(value))


def _from_item(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {k: _deserializer.deserialize(v) for k, v in item.items()}
    return result


def _pk(household_id: str) -> str:
    return f"HOUSEHOLD#{household_id}"


def _sk(entity_type: str, entity_id: str) -> str:
    return f"{entity_type.upper()}#{entity_id}"


def _is_conditional(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in (
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    )


def _item_attributes(item: StateItem) -> dict[str, Any]:
    attrs: dict[str, object] = {
        "PK": _pk(item.household_id),
        "SK": _sk(item.entity_type, item.entity_id),
        "version": item.version,
        "data": item.data,
    }
    if item.expires_at is not None:
        attrs["expires_at"] = item.expires_at
    return attrs


def _condition(expected_version: int | None) -> dict[str, Any]:
    if expected_version is None:
        return {
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": "PK"},
        }
    return {
        "ConditionExpression": "#v = :v",
        "ExpressionAttributeNames": {"#v": "version"},
        "ExpressionAttributeValues": {":v": {"N": str(expected_version)}},
    }


def claim_key(namespace: str, normalized_value: str) -> str:
    return hashlib.sha256(f"{namespace}:{normalized_value}".encode()).hexdigest()


class DynamoStateRepository:
    def __init__(
        self,
        *,
        table_name: str,
        client: _DynamoClient,
        cursor_signing_key: bytes,
    ) -> None:
        if len(cursor_signing_key) < 32:
            raise ValueError("cursor_signing_key must be at least 32 bytes")
        self._table = table_name
        self._client = client
        self._cursor_key = cursor_signing_key

    def _encode_cursor(
        self, key: dict[str, Any], household_id: str, entity_type: str
    ) -> str:
        payload = json.dumps(
            {"hh": household_id, "et": entity_type, "key": key},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode()

    def _decode_cursor(
        self, household_id: str, entity_type: str, cursor: str
    ) -> dict[str, Any]:
        try:
            raw = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
        except ValueError as e:
            raise BadRequestError("Invalid page cursor.") from e
        if len(raw) <= 32:
            raise BadRequestError("Invalid page cursor.")
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise BadRequestError("Invalid page cursor.")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as e:
            raise BadRequestError("Invalid page cursor.") from e
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"hh", "et", "key"}
            or decoded["hh"] != household_id
            or decoded["et"] != entity_type
        ):
            raise BadRequestError("Invalid page cursor.")
        key = decoded["key"]
        if not isinstance(key, dict) or set(key) != {"PK", "SK"}:
            raise BadRequestError("Invalid page cursor.")
        pk, sk = key["PK"], key["SK"]
        if not (
            isinstance(pk, dict)
            and set(pk) == {"S"}
            and isinstance(sk, dict)
            and set(sk) == {"S"}
        ):
            raise BadRequestError("Invalid page cursor.")
        if pk["S"] != _pk(household_id) or not str(sk["S"]).startswith(
            f"{entity_type.upper()}#"
        ):
            raise BadRequestError("Invalid page cursor.")
        return key

    def _to_item(self, raw: dict[str, Any]) -> StateItem:
        plain = _from_item(raw)
        sk = str(plain["SK"])
        entity_type, _, entity_id = sk.partition("#")
        data = plain.get("data")
        return StateItem(
            household_id=str(plain["PK"]).removeprefix("HOUSEHOLD#"),
            entity_type=entity_type.lower(),  # type: ignore[arg-type]
            entity_id=entity_id,
            version=int(plain["version"]),
            data=data if isinstance(data, dict) else {},
            expires_at=(int(plain["expires_at"]) if plain.get("expires_at") else None),
        )

    def get(
        self, household_id: str, entity_type: EntityType, entity_id: str
    ) -> StateItem | None:
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={
                    "PK": {"S": _pk(household_id)},
                    "SK": {"S": _sk(entity_type, entity_id)},
                },
                ConsistentRead=True,
            )
        except ClientError as e:
            raise PersistenceError("State read failed.") from e
        item = response.get("Item")
        if not item:
            return None
        return self._to_item(item)

    def put(self, item: StateItem, expected_version: int | None = None) -> None:
        attributes = {
            key: _to_attr(value) for key, value in _item_attributes(item).items()
        }
        request: dict[str, Any] = {
            "TableName": self._table,
            "Item": attributes,
            **_condition(expected_version),
        }
        try:
            self._client.put_item(**request)
        except ClientError as e:
            if _is_conditional(e):
                raise ConflictError("State write conflict.") from e
            raise PersistenceError("State write failed.") from e

    def query(
        self,
        household_id: str,
        entity_type: EntityType,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> StatePage:
        request: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :sk)",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
            "ExpressionAttributeValues": {
                ":pk": {"S": _pk(household_id)},
                ":sk": {"S": f"{entity_type.upper()}#"},
            },
            "Limit": limit,
            "ConsistentRead": True,
        }
        if cursor is not None:
            request["ExclusiveStartKey"] = self._decode_cursor(
                household_id, entity_type, cursor
            )
        try:
            response = self._client.query(**request)
        except ClientError as e:
            raise PersistenceError("State query failed.") from e
        items = [self._to_item(i) for i in response.get("Items", [])]
        last = response.get("LastEvaluatedKey")
        return StatePage(
            items=items,
            cursor=(
                self._encode_cursor(last, household_id, entity_type) if last else None
            ),
        )

    def transact(self, actions: Sequence[TransactAction]) -> None:
        transact_items: list[dict[str, Any]] = []
        for action in actions:
            if isinstance(action, PutAction):
                attributes = {
                    key: _to_attr(value)
                    for key, value in _item_attributes(action.item).items()
                }
                if action.gsi1pk is not None:
                    attributes["GSI1PK"] = {"S": action.gsi1pk}
                    attributes["GSI1SK"] = {"S": action.gsi1sk or ""}
                put: dict[str, Any] = {
                    "TableName": self._table,
                    "Item": attributes,
                    **_condition(action.expected_version),
                }
                transact_items.append({"Put": put})
            elif isinstance(action, ClaimAction):
                claim_put: dict[str, Any] = {
                    "TableName": self._table,
                    "Item": {
                        "PK": {"S": f"CLAIM#{action.claim_key}"},
                        "SK": {"S": "CLAIM"},
                        "data": {
                            "M": {
                                "namespace": {"S": action.namespace},
                                "household_id": {"S": action.household_id},
                                "entity_type": {"S": action.entity_type},
                                "entity_id": {"S": action.entity_id},
                            }
                        },
                    },
                    **_condition(None),
                }
                transact_items.append({"Put": claim_put})
            else:
                delete: dict[str, Any] = {
                    "TableName": self._table,
                    "Key": {
                        "PK": {"S": _pk(action.household_id)},
                        "SK": {"S": _sk(action.entity_type, action.entity_id)},
                    },
                }
                if action.expected_version is not None:
                    delete["ConditionExpression"] = "#v = :v"
                    delete["ExpressionAttributeNames"] = {"#v": "version"}
                    delete["ExpressionAttributeValues"] = {
                        ":v": {"N": str(action.expected_version)}
                    }
                transact_items.append({"Delete": delete})
        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except ClientError as e:
            if _is_conditional(e):
                raise ConflictError("State transaction conflict.") from e
            raise PersistenceError("State transaction failed.") from e

    def identity_claim(
        self,
        *,
        household_id: str,
        entity_type: EntityType,
        entity_id: str,
        namespace: str,
        value: str,
    ) -> ClaimAction:
        return ClaimAction(
            household_id=household_id,
            entity_type=entity_type,
            entity_id=entity_id,
            namespace=namespace,
            claim_key=claim_key(namespace, value.strip()),
        )

    def provider_claim(
        self,
        *,
        household_id: str,
        connection_id: str,
        provider: str,
        provider_subject: str,
    ) -> ClaimAction:
        return self.identity_claim(
            household_id=household_id,
            entity_type="connection",
            entity_id=connection_id,
            namespace=f"provider-subject:{provider}",
            value=provider_subject,
        )
