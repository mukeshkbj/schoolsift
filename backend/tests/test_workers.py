from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from schoolsift.config import Settings
from schoolsift.credentials import OAuthCredentials
from schoolsift.models import RenewalResult
from schoolsift.notifications import IngestionEvent
from schoolsift.sqlite_store import SQLiteStore
from schoolsift.workers import create_ingestion_handler, create_renewal_handler


def _event(
    event_id: str = "evt-1",
    *,
    kind: str = "reauthorization_required",
    household_id: str = "hh",
    connection_id: str = "conn",
) -> str:
    return IngestionEvent(
        id=event_id,
        provider="gmail",
        household_id=household_id,
        connection_id=connection_id,
        kind=kind,
        provider_cursor=None,
        received_at=datetime.now(UTC),
    ).model_dump_json()


def _record(message_id: str, body: str, group: str | None = None) -> dict:
    rec = {"messageId": message_id, "body": body}
    if group is not None:
        rec["attributes"] = {"MessageGroupId": group}
    return rec


class _NoopStore:
    pass


def _handler(**overrides):
    deps = {
        "store": _NoopStore(),
        "vault": object(),
        "content": object(),
        "providers": {},
    }
    deps.update(overrides)
    return create_ingestion_handler(**deps)


def test_malformed_body_is_partial_failure(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "schoolsift.workers.process_ingestion_event",
        lambda event, **kw: calls.append(event.id),
    )
    handler = _handler()
    result = handler(
        {
            "Records": [
                _record("m1", "not json"),
                _record("m2", _event("evt-2")),
            ]
        },
        None,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert calls == ["evt-2"]


def test_fifo_group_fails_after_first_failure(monkeypatch):
    calls: list[str] = []

    def process(event, **kw):
        calls.append(event.id)
        if event.id == "evt-bad":
            raise RuntimeError("boom")

    monkeypatch.setattr("schoolsift.workers.process_ingestion_event", process)
    handler = _handler()
    result = handler(
        {
            "Records": [
                _record("a1", _event("evt-bad"), group="g1"),
                _record("a2", _event("evt-later"), group="g1"),
                _record("b1", _event("evt-other"), group="g2"),
                _record("b2", _event("evt-other-2"), group="g2"),
            ]
        },
        None,
    )
    failures = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert failures == {"a1", "a2"}
    assert calls == ["evt-bad", "evt-other", "evt-other-2"]


def test_independent_records_without_group_continue(monkeypatch):
    calls: list[str] = []

    def process(event, **kw):
        calls.append(event.id)
        if event.id == "evt-bad":
            raise RuntimeError("boom")

    monkeypatch.setattr("schoolsift.workers.process_ingestion_event", process)
    handler = _handler()
    result = handler(
        {
            "Records": [
                _record("m1", _event("evt-bad")),
                _record("m2", _event("evt-ok")),
            ]
        },
        None,
    )
    assert result["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert calls == ["evt-bad", "evt-ok"]


def test_records_beyond_ten_are_unprocessed(monkeypatch):
    monkeypatch.setattr(
        "schoolsift.workers.process_ingestion_event",
        lambda event, **kw: None,
    )
    handler = _handler()
    records = [_record(f"m{i}", _event(f"evt-{i}")) for i in range(12)]
    result = handler({"Records": records}, None)
    failures = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert failures == {"m10", "m11"}


def test_non_sqs_event_raises():
    handler = _handler()
    with pytest.raises(ValueError, match="Records"):
        handler({"not": "sqs"}, None)
    with pytest.raises(ValueError, match="Records"):
        handler("not-a-dict", None)


@pytest.mark.parametrize(
    "record",
    [
        "not-a-dict",
        {"body": "{}"},
        {"messageId": 42, "body": "{}"},
        {"messageId": "", "body": "{}"},
        {"messageId": "m1", "body": "{}", "attributes": "x"},
        {
            "messageId": "m1",
            "body": "{}",
            "attributes": {"MessageGroupId": 7},
        },
    ],
)
def test_structurally_invalid_records_raise(record):
    handler = _handler()
    with pytest.raises(ValueError):
        handler({"Records": [record]}, None)


def test_invalid_record_beyond_ten_raises(monkeypatch):
    monkeypatch.setattr(
        "schoolsift.workers.process_ingestion_event",
        lambda event, **kw: None,
    )
    handler = _handler()
    records = [_record(f"m{i}", _event(f"evt-{i}")) for i in range(10)]
    records.append({"body": "{}"})
    with pytest.raises(ValueError):
        handler({"Records": records}, None)


def test_non_string_body_is_partial_failure(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "schoolsift.workers.process_ingestion_event",
        lambda event, **kw: calls.append(event.id),
    )
    handler = _handler()
    result = handler(
        {
            "Records": [
                {"messageId": "m1", "body": {"not": "a string"}},
                _record("m2", _event("evt-ok")),
            ]
        },
        None,
    )
    assert result["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert calls == ["evt-ok"]


def _sub_env(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    conn = store.upsert_connection(
        h.id, provider="gmail", provider_subject="s", email="me@x.com"
    )
    store.set_connection_status(h.id, conn.id, "connected")
    store.upsert_webhook_subscription(
        h.id,
        conn.id,
        provider="gmail",
        provider_subscription_id="gmail:x",
        client_state_hash="",
        cursor="history:5",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return store, h, conn


def test_renewal_handler_returns_counts(tmp_path, monkeypatch):
    store, _h, _conn = _sub_env(tmp_path)
    monkeypatch.setattr(
        "schoolsift.workers.renew_due_subscriptions",
        lambda now, **kw: RenewalResult(checked=3, renewed=2, failed=1),
    )
    handler = create_renewal_handler(
        store=store,
        vault=object(),
        providers={},
        settings=Settings(),
    )
    out = handler({"anything": "ignored", "secret": "x"}, None)
    assert out == {"checked": 3, "renewed": 2, "failed": 1}


def test_renewal_handler_invokes_service(tmp_path):
    store, _h, _conn = _sub_env(tmp_path)
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return __import__("httpx").Response(
            200, json={"historyId": "9", "expiration": "1893456000000"}
        )

    import httpx

    from schoolsift.providers import GmailOAuthProvider

    provider = GmailOAuthProvider(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        client_id="gid",
        client_secret="gs",  # noqa: S106
    )

    class _Vault:
        def get(self, cid):
            return OAuthCredentials(
                access_token=SecretStr("t"),
                refresh_token=SecretStr("r"),
                expires_at=None,
                scopes=(),
            )

        def put(self, cid, c):
            pass

        def delete(self, cid):
            pass

    handler_fn = create_renewal_handler(
        store=store,
        vault=_Vault(),
        providers={"gmail": provider},
        settings=Settings(
            public_api_url="https://api.example",
            gmail_topic="projects/p/topics/t",
        ),
    )
    out = handler_fn({}, None)
    assert out == {"checked": 1, "renewed": 1, "failed": 0}
    assert calls and calls[0].endswith("/watch")
    assert json.dumps(out) not in calls[0]


def _exec_cmd(execution_id: str = "exec-1", household_id: str = "hh") -> str:
    from schoolsift.models import ExecutionCommand

    return ExecutionCommand(
        execution_id=execution_id, household_id=household_id
    ).model_dump_json()


def _exec_handler(**overrides):
    from schoolsift.workers import create_execution_handler

    deps = {
        "store": _NoopStore(),
        "vault": object(),
        "content": object(),
        "providers": {},
    }
    deps.update(overrides)
    return create_execution_handler(**deps)


def test_execution_handler_malformed_body_partial_failure(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "schoolsift.workers.execute_command",
        lambda cmd, **kw: calls.append(cmd.execution_id),
    )
    handler = _exec_handler()
    result = handler(
        {
            "Records": [
                _record("m1", "not json"),
                _record("m2", _exec_cmd("exec-2")),
            ]
        },
        None,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert calls == ["exec-2"]


def test_execution_handler_fifo_group_stops_after_failure(monkeypatch):
    calls: list[str] = []

    def run(cmd, **kw):
        calls.append(cmd.execution_id)
        if cmd.execution_id == "exec-bad":
            raise RuntimeError("boom")

    monkeypatch.setattr("schoolsift.workers.execute_command", run)
    handler = _exec_handler()
    result = handler(
        {
            "Records": [
                _record("a1", _exec_cmd("exec-bad"), group="g1"),
                _record("a2", _exec_cmd("exec-later"), group="g1"),
                _record("b1", _exec_cmd("exec-other"), group="g2"),
            ]
        },
        None,
    )
    failures = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert failures == {"a1", "a2"}
    assert calls == ["exec-bad", "exec-other"]


def test_execution_handler_overflow_records_failed(monkeypatch):
    monkeypatch.setattr("schoolsift.workers.execute_command", lambda *a, **kw: None)
    handler = _exec_handler()
    records = [_record(f"m{i}", _exec_cmd(f"e{i}")) for i in range(12)]
    result = handler({"Records": records}, None)
    failures = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert failures == {"m10", "m11"}


def test_execution_handler_malformed_envelope_raises():
    handler = _exec_handler()
    with pytest.raises(ValueError):
        handler({"not": "sqs"}, None)
    with pytest.raises(ValueError):
        handler({"Records": [{"body": _exec_cmd()}]}, None)


def test_execution_handler_structural_record_raises(monkeypatch):
    monkeypatch.setattr("schoolsift.workers.execute_command", lambda *a, **kw: None)
    handler = _exec_handler()
    with pytest.raises(ValueError):
        handler({"Records": ["not-a-dict"]}, None)


def _execution_record(status: str, *, attempts: int = 1):
    from schoolsift.models import ExecutionRecord

    return ExecutionRecord(
        id="exec-1",
        household_id="hh",
        proposal_id="prop-1",
        proposal_version=1,
        connection_id="conn-1",
        idempotency_key="idem",
        status=status,
        attempts=attempts,
        provider_operation_id=None,
        safe_error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_execution_handler_queued_result_redelivered(tmp_path):
    from cryptography.fernet import Fernet
    from test_execution import (
        FakeProvider,
        InMemoryVault,
        approve,
        creds,
        make_packet,
        seed_message,
    )

    from schoolsift.content import LocalEncryptedContentStore
    from schoolsift.errors import ProviderError

    store = SQLiteStore(tmp_path / "db.sqlite")
    store.initialize()
    h = store.create_household(name="H", timezone="UTC")
    seed_message(store, h.id)
    vault = InMemoryVault()
    vault.put("conn-1", creds())
    content = LocalEncryptedContentStore(
        tmp_path / "content", key=Fernet.generate_key()
    )
    provider = FakeProvider()
    provider.send_error = ProviderError("transient")

    packet = make_packet()
    store.save_packet(h.id, packet)
    _, execution = approve(store, h, packet)

    handler = _exec_handler(
        store=store,
        vault=vault,
        content=content,
        providers={"gmail": provider},
    )
    result = handler(
        {"Records": [_record("m1", _exec_cmd(execution.id, h.id), group="hh")]},
        None,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    rec = store.get_execution(h.id, execution.id)
    assert rec is not None
    assert rec.status == "queued"
    assert rec.attempts == 1


def test_execution_handler_terminal_results_acked(monkeypatch):
    calls: list[str] = []

    def run(cmd, **kw):
        calls.append(cmd.execution_id)
        status = {
            "exec-uncertain": "delivery_uncertain",
            "exec-failed": "failed",
            "exec-done": "completed",
        }[cmd.execution_id]
        return _execution_record(status)

    monkeypatch.setattr("schoolsift.workers.execute_command", run)
    handler = _exec_handler()
    result = handler(
        {
            "Records": [
                _record("m1", _exec_cmd("exec-uncertain")),
                _record("m2", _exec_cmd("exec-failed")),
                _record("m3", _exec_cmd("exec-done")),
            ]
        },
        None,
    )
    assert result == {"batchItemFailures": []}
    assert calls == ["exec-uncertain", "exec-failed", "exec-done"]


def test_execution_handler_queued_result_propagates_group(monkeypatch):
    calls: list[str] = []

    def run(cmd, **kw):
        calls.append(cmd.execution_id)
        status = "queued" if cmd.execution_id == "exec-retry" else "completed"
        return _execution_record(status)

    monkeypatch.setattr("schoolsift.workers.execute_command", run)
    handler = _exec_handler()
    result = handler(
        {
            "Records": [
                _record("a1", _exec_cmd("exec-retry"), group="g1"),
                _record("a2", _exec_cmd("exec-later"), group="g1"),
                _record("b1", _exec_cmd("exec-other"), group="g2"),
            ]
        },
        None,
    )
    failures = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert failures == {"a1", "a2"}
    assert calls == ["exec-retry", "exec-other"]
