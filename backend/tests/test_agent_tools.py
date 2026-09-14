from __future__ import annotations

from datetime import UTC, datetime

import pytest
from strands.tools.decorator import DecoratedFunctionTool  # noqa: F401

from schoolsift.agent import build_tools
from schoolsift.models import (
    CalendarRecord,
    DocumentContent,
    HouseholdContext,
    NormalizedMessage,
    RelatedAction,
)


class FakeSource:
    household_id = "hh-1"

    def get_household_context(self, household_id: str) -> HouseholdContext:
        if household_id != self.household_id:
            raise PermissionError(f"Unknown household: {household_id}")
        return HouseholdContext(
            household_id=household_id,
            children=["Kid"],
            connections=["me@example.com"],
        )

    def get_message(self, household_id: str, message_id: str) -> NormalizedMessage:
        self.get_household_context(household_id)
        if message_id != "msg-1":
            raise KeyError(message_id)
        return NormalizedMessage(
            message_id="msg-1",
            thread_id="thread-1",
            sender_email="o@example.org",
            reply_to="o@example.org",
            subject="Question",
            received_at=datetime(2026, 9, 10, tzinfo=UTC),
            source_confirmed=True,
            body="Body text.",
            attachment_ids=["doc-1"],
        )

    def get_document(self, household_id: str, document_id: str) -> DocumentContent:
        self.get_household_context(household_id)
        if document_id != "doc-1":
            raise KeyError(document_id)
        return DocumentContent(
            document_id="doc-1",
            name="form.pdf",
            mime="application/pdf",
            text="Form body only.",
            acroform_fields=["student_name"],
        )

    def find_related_actions(
        self, household_id: str, thread_key: str
    ) -> list[RelatedAction]:
        self.get_household_context(household_id)
        if thread_key == "thread-1":
            return [
                RelatedAction(packet_id="packet-1", summary="s", status="needs_review")
            ]
        return []

    def find_calendar_conflicts(
        self, household_id: str, start: datetime, end: datetime
    ) -> list[CalendarRecord]:
        self.get_household_context(household_id)
        return [
            CalendarRecord(
                id="evt-1",
                household_id=household_id,
                title="Dentist",
                starts_at=start,
                ends_at=end,
            )
        ]


@pytest.fixture()
def tools() -> dict:
    (
        get_household_context,
        get_message,
        get_document,
        find_related_actions,
        find_calendar_conflicts,
    ) = build_tools(FakeSource())
    return {
        "household": get_household_context,
        "message": get_message,
        "document": get_document,
        "related": find_related_actions,
        "conflicts": find_calendar_conflicts,
    }


def test_household_context_rejects_foreign_household(tools):
    with pytest.raises((ValueError, PermissionError)):
        tools["household"]("someone-elses-household")
    ctx = tools["household"]("hh-1")
    assert "Kid" in ctx.children


def test_related_actions_rejects_foreign_household(tools):
    with pytest.raises((ValueError, PermissionError)):
        tools["related"]("other-household", "thread-1")


def test_calendar_conflicts_rejects_foreign_household(tools):
    with pytest.raises((ValueError, PermissionError)):
        tools["conflicts"](
            "other-household",
            datetime(2026, 9, 25, 15, tzinfo=UTC),
            datetime(2026, 9, 25, 16, tzinfo=UTC),
        )


def test_related_actions_match_on_thread_key(tools):
    assert tools["related"]("hh-1", "thread-1")[0].packet_id == "packet-1"
    assert tools["related"]("hh-1", "msg-1") == []


def test_document_content_scoped_to_selected_attachment(tools):
    doc = tools["document"]("hh-1", "doc-1")
    assert doc.text == "Form body only."
    with pytest.raises(KeyError):
        tools["document"]("hh-1", "doc-missing")


def test_message_and_document_reject_foreign_household(tools):
    with pytest.raises((ValueError, PermissionError)):
        tools["message"]("other-household", "msg-1")
    with pytest.raises((ValueError, PermissionError)):
        tools["document"]("other-household", "doc-1")


def test_calendar_conflicts_overlap(tools):
    found = tools["conflicts"](
        "hh-1",
        datetime(2026, 9, 25, 15, tzinfo=UTC),
        datetime(2026, 9, 25, 16, tzinfo=UTC),
    )
    assert [c.id for c in found] == ["evt-1"]
