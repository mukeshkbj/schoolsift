from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from schoolsift.api import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def process_field_trip(client: TestClient) -> dict:
    resp = client.post("/v1/demo/process", json={"message_ids": ["msg-field-trip"]})
    assert resp.status_code == 200, resp.text
    return resp.json()["packets"][0]


def head(packet: dict, kind: str) -> dict:
    heads: dict[str, dict] = {}
    for v in packet["proposals"]:
        heads[v["id"]] = v
    return next(v for v in heads.values() if v["payload"]["kind"] == kind)


class TestBasics:
    def test_health(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_demo_state_is_labeled(self, client: TestClient):
        resp = client.get("/v1/demo")
        assert resp.status_code == 200
        body = resp.json()
        assert body["label"] == "Demo data"
        assert len(body["messages"]) == 6
        assert body["packets"] == []
        assert body["outcomes"] == []

    def test_process_all(self, client: TestClient):
        resp = client.post("/v1/demo/process", json={"message_ids": None})
        assert resp.status_code == 200
        assert len(resp.json()["packets"]) == 6

    def test_process_unknown_message(self, client: TestClient):
        resp = client.post("/v1/demo/process", json={"message_ids": ["msg-nope"]})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_reset(self, client: TestClient):
        client.post("/v1/demo/process", json={"message_ids": None})
        resp = client.post("/v1/demo/reset")
        assert resp.status_code == 200
        assert resp.json()["packets"] == []


class TestVersioning:
    def test_edit_creates_new_version(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "reply")
        new_payload = {**proposal["payload"], "body": "Maya will attend. — Alex"}
        resp = client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions",
            json={"expected_version": proposal["version"], "payload": new_payload},
        )
        assert resp.status_code == 200, resp.text
        v2 = resp.json()
        assert v2["version"] == 2
        assert v2["status"] == "proposed"
        assert v2["payload"]["body"] == "Maya will attend. — Alex"

        state = client.get("/v1/demo").json()
        packet = next(p for p in state["packets"] if p["id"] == packet["id"])
        versions = [v for v in packet["proposals"] if v["id"] == proposal["id"]]
        assert {v["version"]: v["status"] for v in versions} == {
            1: "superseded",
            2: "proposed",
        }

    def test_edit_stale_version_conflicts(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "reply")
        resp = client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions",
            json={"expected_version": 99, "payload": proposal["payload"]},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "VERSION_CONFLICT"

    def test_approve_writes_demo_outcome(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "reply")
        resp = client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions/{proposal['version']}/approve",
            json={"payload_hash": proposal["payload_hash"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"]["status"] == "completed"
        outcome = body["outcome"]
        assert outcome["label"] == "Demo data"
        assert outcome["kind"] == "demo_reply"

        state = client.get("/v1/demo").json()
        assert len(state["outcomes"]) == 1

    def test_approve_stale_hash_conflicts(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "reply")
        resp = client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions/{proposal['version']}/approve",
            json={"payload_hash": "0" * 64},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "HASH_MISMATCH"

    def test_approve_superseded_version_conflicts(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "reply")
        client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions",
            json={"expected_version": 1, "payload": proposal["payload"]},
        )
        resp = client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions/1/approve",
            json={"payload_hash": proposal["payload_hash"]},
        )
        assert resp.status_code == 409

    def test_first_decision_wins(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "reply")
        url = f"/v1/demo/proposals/{proposal['id']}/versions/1"
        assert (
            client.post(
                f"{url}/approve", json={"payload_hash": proposal["payload_hash"]}
            ).status_code
            == 200
        )
        resp = client.post(
            f"{url}/reject", json={"payload_hash": proposal["payload_hash"]}
        )
        assert resp.status_code == 409

    def test_escalation_cannot_be_approved(self, client: TestClient):
        resp = client.post(
            "/v1/demo/process", json={"message_ids": ["msg-date-conflict"]}
        )
        packet = resp.json()["packets"][0]
        esc = head(packet, "escalation")
        resp = client.post(
            f"/v1/demo/proposals/{esc['id']}/versions/{esc['version']}/approve",
            json={"payload_hash": esc["payload_hash"]},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ESCALATION_NOT_APPROVABLE"

    def test_reject(self, client: TestClient):
        packet = process_field_trip(client)
        proposal = head(packet, "calendar")
        resp = client.post(
            f"/v1/demo/proposals/{proposal['id']}/versions/{proposal['version']}/reject",
            json={"payload_hash": proposal["payload_hash"]},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_unknown_proposal_404(self, client: TestClient):
        resp = client.post(
            "/v1/demo/proposals/nope/versions/1/approve",
            json={"payload_hash": "0" * 64},
        )
        assert resp.status_code == 404

    def test_validation_error_is_typed(self, client: TestClient):
        resp = client.post("/v1/demo/process", json={"message_ids": "oops"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


class TestCors:
    def test_allowed_origin(self, client: TestClient):
        resp = client.options(
            "/v1/demo",
            headers={
                "Origin": "http://localhost:3210",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers["access-control-allow-origin"] == "http://localhost:3210"

    def test_disallowed_origin(self, client: TestClient):
        resp = client.options(
            "/v1/demo",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert (
            "access-control-allow-origin" not in {k.lower() for k in resp.headers}
            or resp.headers.get("access-control-allow-origin") != "https://evil.example"
        )
