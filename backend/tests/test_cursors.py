from __future__ import annotations

import httpx
import pytest
from test_providers import creds, gmail_client, outlook_client

from schoolsift.errors import FullResyncRequiredError, ProviderError


def _gmail_meta(mid: str) -> dict:
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "internalDate": "1789214400000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Office <office@school.org>"},
                {"name": "Subject", "value": "Hi"},
            ]
        },
    }


def test_gmail_history_cursor_calls_history_list():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/history" in url:
            assert "startHistoryId=555" in url
            assert "historyTypes=messageAdded" in url
            return httpx.Response(
                200,
                json={
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "m1", "threadId": "t1"}},
                                {"message": {"id": "m1", "threadId": "t1"}},
                                {"message": {"id": "m2", "threadId": "t2"}},
                            ]
                        }
                    ],
                    "historyId": "600",
                },
            )
        return httpx.Response(
            200, json=_gmail_meta(url.rsplit("/", 1)[-1].split("?")[0])
        )

    p = gmail_client(handler)
    page = p.list_message_headers(creds(), cursor="history:555")
    assert page.has_more is False
    assert page.next_cursor == "history:600"
    assert [m.provider_message_id for m in page.messages] == ["m1", "m2"]


def test_gmail_history_pagination_cursor():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "history": [],
                    "nextPageToken": "hp-1",
                    "historyId": "700",
                },
            )
        return httpx.Response(200, json=_gmail_meta("m"))

    p = gmail_client(handler)
    page = p.list_message_headers(creds(), cursor="history:555")
    assert page.has_more is True
    assert page.next_cursor == "page-history:555:hp-1"

    def handler2(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url:
            assert "startHistoryId=555" in url
            assert "pageToken=hp-1" in url
            return httpx.Response(200, json={"history": [], "historyId": "701"})
        return httpx.Response(200, json=_gmail_meta("m"))

    p2 = gmail_client(handler2)
    page2 = p2.list_message_headers(creds(), cursor="page-history:555:hp-1")
    assert page2.has_more is False
    assert page2.next_cursor == "history:701"


def test_gmail_history_404_requires_full_resync():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 404}})

    p = gmail_client(handler)
    with pytest.raises(FullResyncRequiredError):
        p.list_message_headers(creds(), cursor="history:999999")


@pytest.mark.parametrize(
    "cursor",
    [
        "history:abc",
        "page-history:555",
        "page-history:x:y",
        "page-history:555:",
        "page:",
        "https://evil.example/steal",
        "weird:thing",
    ],
)
def test_gmail_rejects_malformed_or_foreign_cursors(cursor):
    p = gmail_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        p.list_message_headers(creds(), cursor=cursor)


def test_outlook_delta_initial_and_delta_link():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "m1",
                        "conversationId": "c1",
                        "sender": {
                            "emailAddress": {
                                "name": "Office",
                                "address": "office@school.org",
                            }
                        },
                        "subject": "Hi",
                        "receivedDateTime": "2026-09-12T12:00:00Z",
                    },
                    {
                        "id": "m-gone",
                        "@removed": {"reason": "deleted"},
                    },
                ],
                "@odata.deltaLink": (
                    "https://graph.microsoft.com/v1.0/me/mailFolders"
                    "/inbox/messages/delta?$deltatoken=abc"
                ),
            },
        )

    p = outlook_client(handler)
    page = p.list_message_headers(creds(), cursor=None)
    assert "/delta" in calls[0]
    assert page.has_more is False
    assert page.next_cursor == (
        "https://graph.microsoft.com/v1.0/me/mailFolders"
        "/inbox/messages/delta?$deltatoken=abc"
    )
    assert [m.provider_message_id for m in page.messages] == ["m1"]


def test_outlook_delta_next_link_and_410():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$deltatoken=dead" in url:
            return httpx.Response(410, json={"error": {"code": "resyncRequired"}})
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/mailFolders"
                    "/inbox/messages/delta?$skiptoken=s1"
                ),
            },
        )

    p = outlook_client(handler)
    page = p.list_message_headers(creds(), cursor=None)
    assert page.has_more is True
    with pytest.raises(FullResyncRequiredError):
        p.list_message_headers(
            creds(),
            cursor=(
                "https://graph.microsoft.com/v1.0/me/mailFolders"
                "/inbox/messages/delta?$deltatoken=dead"
            ),
        )


def test_outlook_rejects_gmail_cursor():
    p = outlook_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        p.list_message_headers(creds(), cursor="history:555")
