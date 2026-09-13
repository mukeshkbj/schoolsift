from __future__ import annotations

import hashlib
import json

import pytest

from schoolsift.domain import (
    CalendarProposal,
    EscalationProposal,
    ProposalVersion,
    ReplyProposal,
    approve_proposal,
    edit_proposal,
    payload_digest,
    reject_proposal,
)
from schoolsift.errors import ConflictError, NotApprovableError, NotFoundError


def reply(body: str = "Thank you, Maya will attend.") -> ReplyProposal:
    return ReplyProposal(
        kind="reply",
        recipient="office@maplegrove.example",
        subject="Re: Field trip",
        body=body,
    )


def v1(payload=None) -> list[ProposalVersion]:
    payload = payload or reply()
    return [
        ProposalVersion(
            id="prop-1",
            version=1,
            status="proposed",
            payload=payload,
            payload_hash=payload_digest(payload),
        )
    ]


class TestCanonicalHash:
    def test_digest_is_sha256_of_canonical_json(self):
        payload = reply()
        canonical = json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert payload_digest(payload) == hashlib.sha256(canonical).hexdigest()

    def test_digest_ignores_input_key_order(self):
        a = ReplyProposal.model_validate(
            {"kind": "reply", "recipient": "a@b.example", "subject": "s", "body": "x"}
        )
        b = ReplyProposal.model_validate(
            {"body": "x", "subject": "s", "recipient": "a@b.example", "kind": "reply"}
        )
        assert payload_digest(a) == payload_digest(b)

    def test_digest_changes_with_content(self):
        assert payload_digest(reply("a")) != payload_digest(reply("b"))

    def test_calendar_digest_covers_datetime(self):
        cal = CalendarProposal(
            kind="calendar",
            title="Trip",
            starts_at="2026-09-25T09:00:00Z",
            ends_at="2026-09-25T14:30:00Z",
        )
        assert len(payload_digest(cal)) == 64


class TestEdit:
    def test_edit_creates_new_version_and_supersedes(self):
        versions = v1()
        new = edit_proposal(
            versions,
            proposal_id="prop-1",
            expected_version=1,
            new_payload=reply("Edited body"),
        )
        assert len(new) == 2
        assert new[0].status == "superseded"
        assert new[1].version == 2
        assert new[1].status == "proposed"
        assert new[1].payload.body == "Edited body"
        assert new[1].payload_hash == payload_digest(new[1].payload)
        assert new[1].payload_hash != new[0].payload_hash

    def test_edit_with_stale_expected_version_conflicts(self):
        versions = edit_proposal(
            v1(), proposal_id="prop-1", expected_version=1, new_payload=reply("v2")
        )
        with pytest.raises(ConflictError):
            edit_proposal(
                versions,
                proposal_id="prop-1",
                expected_version=1,
                new_payload=reply("v3"),
            )

    def test_edit_of_decided_proposal_conflicts(self):
        versions = v1()
        versions = reject_proposal(
            versions, version=1, payload_hash=versions[0].payload_hash
        )
        with pytest.raises(ConflictError):
            edit_proposal(
                versions,
                proposal_id="prop-1",
                expected_version=1,
                new_payload=reply("x"),
            )


class TestApproveReject:
    def test_approve_marks_completed(self):
        versions = v1()
        out = approve_proposal(
            versions, version=1, payload_hash=versions[0].payload_hash
        )
        assert out[0].status == "completed"

    def test_approve_with_wrong_hash_conflicts(self):
        versions = v1()
        with pytest.raises(ConflictError):
            approve_proposal(versions, version=1, payload_hash="0" * 64)

    def test_approve_of_superseded_version_conflicts(self):
        versions = edit_proposal(
            v1(), proposal_id="prop-1", expected_version=1, new_payload=reply("v2")
        )
        with pytest.raises(ConflictError):
            approve_proposal(versions, version=1, payload_hash=versions[0].payload_hash)

    def test_approve_of_unknown_version_not_found(self):
        with pytest.raises(NotFoundError):
            approve_proposal(v1(), version=9, payload_hash="0" * 64)

    def test_first_decision_wins(self):
        versions = v1()
        versions = approve_proposal(
            versions, version=1, payload_hash=versions[0].payload_hash
        )
        assert versions[0].status == "completed"
        with pytest.raises(ConflictError):
            reject_proposal(versions, version=1, payload_hash=versions[0].payload_hash)
        with pytest.raises(ConflictError):
            approve_proposal(versions, version=1, payload_hash=versions[0].payload_hash)
        assert versions[0].status == "completed"

    def test_rejected_stays_rejected(self):
        versions = v1()
        versions = reject_proposal(
            versions, version=1, payload_hash=versions[0].payload_hash
        )
        assert versions[0].status == "rejected"
        with pytest.raises(ConflictError):
            reject_proposal(versions, version=1, payload_hash=versions[0].payload_hash)
        with pytest.raises(ConflictError):
            approve_proposal(versions, version=1, payload_hash=versions[0].payload_hash)

    def test_escalation_cannot_be_approved(self):
        payload = EscalationProposal(
            kind="escalation", reason="payment", detail="Fee requested"
        )
        versions = v1(payload)
        with pytest.raises(NotApprovableError):
            approve_proposal(versions, version=1, payload_hash=versions[0].payload_hash)

    def test_escalation_can_be_rejected(self):
        payload = EscalationProposal(
            kind="escalation", reason="uncertain", detail="check"
        )
        versions = v1(payload)
        out = reject_proposal(
            versions, version=1, payload_hash=versions[0].payload_hash
        )
        assert out[0].status == "rejected"
