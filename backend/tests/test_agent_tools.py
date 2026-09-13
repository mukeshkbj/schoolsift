from pathlib import Path

import pytest
from strands.tools.decorator import DecoratedFunctionTool  # noqa: F401

from schoolsift.agent import build_tools
from schoolsift.demo_store import Attachment, DemoStore

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"


@pytest.fixture()
def store() -> DemoStore:
    return DemoStore(DEMO_DIR)


@pytest.fixture()
def tools(store: DemoStore) -> dict:
    (
        get_household_context,
        get_normalized_message,
        get_document_content,
        find_related_actions,
        find_calendar_conflicts,
    ) = build_tools(store)
    return {
        "household": get_household_context,
        "message": get_normalized_message,
        "document": get_document_content,
        "related": find_related_actions,
        "conflicts": find_calendar_conflicts,
    }


def test_household_context_rejects_foreign_household(tools, store):
    with pytest.raises((ValueError, PermissionError)):
        tools["household"]("someone-elses-household")
    ctx = tools["household"](store.context.household_id)
    assert "Maya" in ctx.children


def test_related_actions_rejects_foreign_household(tools, store):
    with pytest.raises((ValueError, PermissionError)):
        tools["related"]("other-household", "thread-field-trip")


def test_calendar_conflicts_rejects_foreign_household(tools, store):
    with pytest.raises((ValueError, PermissionError)):
        tools["conflicts"](
            "other-household",
            store.context.family_calendar[0].starts_at,
            store.context.family_calendar[0].ends_at,
        )


def test_related_actions_match_on_thread_key_not_message_id(tools, store):
    store.process(["msg-field-trip"])
    by_thread = tools["related"](store.context.household_id, "thread-field-trip")
    assert len(by_thread) == 1
    assert by_thread[0].packet_id == "packet-msg-field-trip"
    by_message_id = tools["related"](store.context.household_id, "msg-field-trip")
    assert by_message_id == []


def test_calendar_conflicts_overlap(tools, store):
    found = tools["conflicts"](
        store.context.household_id,
        store.context.family_calendar[0].starts_at,
        store.context.family_calendar[0].ends_at,
    )
    assert [c.event_id for c in found] == ["evt-dentist"]


def test_document_content_returns_only_that_attachment(tools, store):
    message = next(m for m in store.messages if m.id == "msg-early-dismissal")
    message.attachments.append(
        Attachment(
            id="att-extra-txt",
            name="materials-fee-letter.txt",
            mime="text/plain",
            path="materials-fee-letter.txt",
        )
    )
    doc = tools["document"]("att-extra-txt")
    assert "materials fee" in doc.text.lower()
    assert "Early dismissal schedule" not in doc.text

    docx = tools["document"]("att-dismissal-docx")
    assert "pickup 12:40" in docx.text
    assert "materials fee" not in docx.text.lower()

    with pytest.raises(KeyError):
        tools["document"]("att-missing")
