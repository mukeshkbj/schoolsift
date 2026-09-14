from __future__ import annotations

import httpx
import pytest

from schoolsift.errors import ProviderError
from schoolsift.providers import GmailOAuthProvider, OutlookOAuthProvider


def gmail_client(handler) -> GmailOAuthProvider:
    return GmailOAuthProvider(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        client_id="gid",
        client_secret="gsecret",  # noqa: S106
    )


def outlook_client(handler) -> OutlookOAuthProvider:
    return OutlookOAuthProvider(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        client_id="oid",
        client_secret="osecret",  # noqa: S106
    )


def test_gmail_authorization_url():
    p = gmail_client(lambda r: httpx.Response(200, json={}))
    url = p.authorization_url(state="st", redirect_uri="http://a/cb")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=gid" in url
    assert "redirect_uri=http%3A%2F%2Fa%2Fcb" in url
    assert "state=st" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    for scope in (
        "openid",
        "email",
        "profile",
        "gmail.readonly",
        "gmail.send",
        "calendar.events",
    ):
        assert scope in url


def test_outlook_authorization_url():
    p = outlook_client(lambda r: httpx.Response(200, json={}))
    url = p.authorization_url(state="st", redirect_uri="http://a/cb")
    assert url.startswith(
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
    )
    assert "client_id=oid" in url
    assert "state=st" in url
    for scope in (
        "openid",
        "profile",
        "email",
        "offline_access",
        "Mail.Read",
        "Mail.Send",
        "Calendars.ReadWrite",
    ):
        assert scope in url


def test_gmail_exchange_and_identity():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 3600,
                    "scope": "openid email",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(200, json={"sub": "sub-1", "email": "me@x.com"})

    p = gmail_client(handler)
    creds = p.exchange_code(code="code", redirect_uri="http://a/cb")
    assert creds.access_token.get_secret_value() == "at"
    assert creds.refresh_token is not None
    assert creds.refresh_token.get_secret_value() == "rt"
    assert creds.expires_at is not None
    assert "openid" in creds.scopes
    token_req = calls[0]
    assert b"grant_type=authorization_code" in token_req.content
    assert b"code=code" in token_req.content
    assert b"client_secret=gsecret" in token_req.content

    ident = p.identity(creds)
    assert ident.provider_subject == "sub-1"
    assert ident.email == "me@x.com"
    assert calls[1].headers["authorization"] == "Bearer at"


def test_outlook_exchange_and_identity():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "login.microsoftonline.com" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "access_token": "oat",
                    "expires_in": 3600,
                    "scope": "Mail.Read",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(
            200, json={"id": "ms-1", "mail": None, "userPrincipalName": "me@y.com"}
        )

    p = outlook_client(handler)
    creds = p.exchange_code(code="code", redirect_uri="http://a/cb")
    assert creds.access_token.get_secret_value() == "oat"
    ident = p.identity(creds)
    assert ident.provider_subject == "ms-1"
    assert ident.email == "me@y.com"


def test_provider_error_sanitized():
    marker_body = "deadbeef-secret-token-body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": marker_body})

    p = gmail_client(handler)
    with pytest.raises(ProviderError) as ei:
        p.exchange_code(code="c", redirect_uri="http://a/cb")
    assert marker_body not in str(ei.value.message)
    assert ei.value.code == "PROVIDER_ERROR"


from datetime import UTC, datetime, timedelta  # noqa: E402
from email.message import EmailMessage  # noqa: E402

from pydantic import SecretStr  # noqa: E402

from schoolsift.credentials import OAuthCredentials  # noqa: E402
from schoolsift.errors import ProviderAuthError  # noqa: E402


def creds(*, expires_in: int = 3600) -> OAuthCredentials:
    return OAuthCredentials(
        access_token=SecretStr("at"),
        refresh_token=SecretStr("rt"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=("openid",),
    )


def gmail_raw_message() -> str:
    import base64

    msg = EmailMessage()
    msg["From"] = "School Office <office@school.org>"
    msg["Reply-To"] = "office@school.org"
    msg["Subject"] = "Lunch menu"
    msg["Date"] = "Fri, 12 Sep 2026 12:00:00 +0000"
    msg.set_content("Plain body here.")
    msg.add_attachment(
        b"PDFBYTES", maintype="application", subtype="pdf", filename="menu.pdf"
    )
    return base64.urlsafe_b64encode(msg.as_bytes()).rstrip(b"=").decode()


def test_gmail_list_headers_paginates_and_reads_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "format=metadata" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            return httpx.Response(
                200,
                json={
                    "id": mid,
                    "threadId": f"t-{mid}",
                    "internalDate": "1789214400000",
                    "payload": {
                        "headers": [
                            {
                                "name": "From",
                                "value": "Office <office@school.org>",
                            },
                            {"name": "Subject", "value": "Hi"},
                        ]
                    },
                },
            )
        if "pageToken=next-1" in url:
            return httpx.Response(
                200, json={"messages": [{"id": "m2", "threadId": "t-m2"}]}
            )
        return httpx.Response(
            200,
            json={
                "messages": [{"id": "m1", "threadId": "t-m1"}],
                "nextPageToken": "next-1",
            },
        )

    p = gmail_client(handler)
    page1 = p.list_message_headers(creds(), cursor=None)
    assert page1.next_cursor == "page:next-1"
    assert page1.has_more is True
    assert page1.messages[0].provider_message_id == "m1"
    assert page1.messages[0].sender_email == "office@school.org"
    assert page1.messages[0].received_at.year == 2026
    page2 = p.list_message_headers(creds(), cursor="page:next-1")
    assert page2.next_cursor is None
    assert page2.has_more is False
    assert page2.messages[0].provider_message_id == "m2"


def test_gmail_fetch_decodes_raw_multipart():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"raw": gmail_raw_message()})

    p = gmail_client(handler)
    msg = p.fetch_message(creds(), "m1")
    assert msg.sender_email == "office@school.org"
    assert msg.subject == "Lunch menu"
    assert "Plain body here." in msg.body_text
    assert len(msg.attachments) == 1
    assert msg.attachments[0].name == "menu.pdf"
    assert msg.attachments[0].content == b"PDFBYTES"


def test_gmail_fetch_malformed_raw():
    p = gmail_client(lambda r: httpx.Response(200, json={"raw": "!!!"}))
    with pytest.raises(ProviderError):
        p.fetch_message(creds(), "m1")


def test_refresh_preserves_old_refresh_token_when_omitted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=refresh_token" in request.content
        assert b"refresh_token=rt" in request.content
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})

    p = gmail_client(handler)
    fresh = p.refresh(creds())
    assert fresh.access_token.get_secret_value() == "at2"
    assert fresh.refresh_token is not None
    assert fresh.refresh_token.get_secret_value() == "rt"


def test_refresh_invalid_grant_is_auth_error():
    p = gmail_client(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(ProviderAuthError):
        p.refresh(creds())


def test_outlook_list_headers_and_fetch():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = str(request.url)
        if "/attachments" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "id": "att-1",
                            "name": "form.pdf",
                            "contentType": "application/pdf",
                            "contentBytes": "UERG",
                        }
                    ]
                },
            )
        if "/messages/m9" in url:
            return httpx.Response(
                200,
                json={
                    "id": "m9",
                    "conversationId": "conv-9",
                    "sender": {
                        "emailAddress": {
                            "name": "Office",
                            "address": "Office@School.org",
                        }
                    },
                    "replyTo": [],
                    "subject": "Form",
                    "receivedDateTime": "2026-09-12T12:00:00Z",
                    "body": {
                        "contentType": "html",
                        "content": "<p>Hi <b>there</b></p>",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "m9",
                        "conversationId": "conv-9",
                        "sender": {
                            "emailAddress": {
                                "name": "Office",
                                "address": "Office@School.org",
                            }
                        },
                        "replyTo": [],
                        "subject": "Form",
                        "receivedDateTime": "2026-09-12T12:00:00Z",
                    }
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=100",
            },
        )

    p = outlook_client(handler)
    page = p.list_message_headers(creds(), cursor=None)
    assert page.next_cursor == (
        "https://graph.microsoft.com/v1.0/me/messages?$skip=100"
    )
    assert page.has_more is True
    assert page.messages[0].sender_email == "office@school.org"
    msg = p.fetch_message(creds(), "m9")
    assert msg.body_text == "Hi there"
    assert msg.attachments[0].content == b"PDF"
    assert msg.attachments[0].provider_attachment_id == "att-1"


def test_outlook_rejects_hostile_next_link():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.nextLink": "https://evil.example/steal",
            },
        )

    p = outlook_client(handler)
    with pytest.raises(ProviderError):
        p.list_message_headers(creds(), cursor=None)
    with pytest.raises(ProviderError):
        p.list_message_headers(creds(), cursor="https://evil.example/steal")


def test_outlook_non_file_attachment_marks_unreadable():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/attachments" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.itemAttachment",
                            "id": "att-x",
                            "name": "nested",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "m9",
                "conversationId": "c",
                "sender": {"emailAddress": {"name": "O", "address": "o@s.org"}},
                "replyTo": [],
                "subject": "S",
                "receivedDateTime": "2026-09-12T12:00:00Z",
                "body": {"contentType": "text", "content": "b"},
            },
        )

    p = outlook_client(handler)
    with pytest.raises(ProviderError):
        p.fetch_message(creds(), "m9")


def test_outlook_malformed_list():
    p = outlook_client(lambda r: httpx.Response(200, json={"value": "nope"}))
    with pytest.raises(ProviderError):
        p.list_message_headers(creds(), cursor=None)


def test_list_401_is_auth_error():
    p = gmail_client(lambda r: httpx.Response(401, json={}))
    with pytest.raises(ProviderAuthError):
        p.list_message_headers(creds(), cursor=None)


def test_gmail_send_email_builds_mime_and_deterministic_id():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "gm-1", "threadId": "t1"})

    from schoolsift.providers import SendEmailCommand

    p = gmail_client(handler)
    command = SendEmailCommand(
        idempotency_key="idem-1",
        recipient="school@example.org",
        subject="Re: Trip",
        body="Yes.",
        attachment_name="form.pdf",
        attachment_mime="application/pdf",
        attachment_bytes=b"PDF",
    )
    result = p.send_email(creds(), command)
    assert result.operation_id == "gm-1"
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == "/gmail/v1/users/me/messages/send"
    import base64
    import email
    import json

    raw = json.loads(request.content)["raw"]
    mime = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=="))
    assert mime["To"] == "school@example.org"
    assert mime["Subject"] == "Re: Trip"
    assert mime["Message-ID"].startswith("<")
    assert "schoolsift.invalid" in mime["Message-ID"]
    parts = [part.get_content_type() for part in mime.walk()]
    assert "application/pdf" in parts

    # deterministic Message-ID across identical commands
    def handler2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "gm-2", "threadId": "t"})

    p2 = gmail_client(handler2)
    p2.send_email(creds(), command)


def test_gmail_send_email_message_id_deterministic():
    raws: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        raws.append(json.loads(request.content)["raw"])
        return httpx.Response(200, json={"id": "gm-1", "threadId": "t"})

    from schoolsift.providers import SendEmailCommand

    p = gmail_client(handler)
    command = SendEmailCommand(
        idempotency_key="same-key",
        recipient="a@b.org",
        subject="s",
        body="b",
    )
    p.send_email(creds(), command)
    p.send_email(creds(), command)
    import base64
    import email

    ids = [
        email.message_from_bytes(base64.urlsafe_b64decode(r + "=="))["Message-ID"]
        for r in raws
    ]
    assert ids[0] == ids[1]


def test_gmail_send_email_timeout_is_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    p = gmail_client(handler)
    with pytest.raises(DeliveryUncertainError):
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )


def test_gmail_send_email_bad_response():
    from schoolsift.providers import SendEmailCommand

    p = gmail_client(lambda r: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(ProviderError):
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )


def test_gmail_calendar_creates_deterministic_event():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "ignored"})

    from schoolsift.providers import CreateEventCommand

    p = gmail_client(handler)
    command = CreateEventCommand(
        idempotency_key="idem-cal",
        title="Trip",
        starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
        ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
    )
    result = p.create_calendar_event(creds(), command)
    assert result.operation_id is not None
    import re

    assert re.fullmatch(r"[0-9a-v]{5,1024}", result.operation_id)
    request = calls[0]
    assert request.url.path == "/calendar/v3/calendars/primary/events"
    assert request.url.params["sendUpdates"] == "none"
    import json

    body = json.loads(request.content)
    assert body["id"] == result.operation_id
    assert body["summary"] == "Trip"

    # same idempotency key -> same event id
    result2 = p.create_calendar_event(creds(), command)
    assert result2.operation_id == result.operation_id


def test_gmail_calendar_409_fetches_existing():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                409, json={"id": body["id"], "summary": body["summary"]}
            )
        return httpx.Response(200, json={"id": "exists"})

    from schoolsift.providers import CreateEventCommand

    p = gmail_client(handler)
    result = p.create_calendar_event(
        creds(),
        CreateEventCommand(
            idempotency_key="k",
            title="t",
            starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
            ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
        ),
    )
    assert calls == ["POST", "GET"]
    assert result.operation_id is not None


def test_gmail_calendar_timeout_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import CreateEventCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    p = gmail_client(handler)
    with pytest.raises(DeliveryUncertainError):
        p.create_calendar_event(
            creds(),
            CreateEventCommand(
                idempotency_key="k",
                title="t",
                starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
            ),
        )


def test_outlook_send_email_draft_send_confirm():
    calls: list[tuple[str, str]] = []
    bodies: list[dict] = []
    checkpoints: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append((request.method, request.url.path))
        if request.content:
            bodies.append(json.loads(request.content))
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(200, json={"id": "draft-1"})
        if request.url.path == "/v1.0/me/messages/draft-1/send":
            return httpx.Response(202)
        if request.url.path == "/v1.0/me/mailFolders/sentitems/messages":
            assert "internetMessageId" in str(request.url.params)
            return httpx.Response(200, json={"value": [{"id": "sent-1"}]})
        return httpx.Response(404, json={})

    from schoolsift.providers import SendEmailCommand

    p = outlook_client(handler)
    result = p.send_email(
        creds(),
        SendEmailCommand(
            idempotency_key="k",
            recipient="school@example.org",
            subject="s",
            body="b",
        ),
        checkpoint=checkpoints.append,
    )
    assert result.operation_id == "draft-1"
    assert checkpoints == ["draft-1"]
    assert calls == [
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft-1/send"),
        ("GET", "/v1.0/me/mailFolders/sentitems/messages"),
    ]
    draft = bodies[0]
    assert draft["internetMessageId"].startswith("<")
    assert "schoolsift.invalid" in draft["internetMessageId"]
    assert draft["toRecipients"][0]["emailAddress"]["address"] == ("school@example.org")


def test_outlook_send_email_unconfirmed_is_uncertain():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(200, json={"id": "draft-1"})
        if request.url.path == "/v1.0/me/messages/draft-1/send":
            return httpx.Response(202)
        if request.url.path == "/v1.0/me/mailFolders/sentitems/messages":
            return httpx.Response(200, json={"value": []})
        return httpx.Response(404, json={})

    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    p = outlook_client(handler)
    with pytest.raises(DeliveryUncertainError) as e:
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )
    assert e.value.operation_id == "draft-1"


def test_outlook_send_email_resumes_draft():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1.0/me/messages/draft-9/send":
            return httpx.Response(202)
        if request.url.path == "/v1.0/me/mailFolders/sentitems/messages":
            return httpx.Response(200, json={"value": [{"id": "s1"}]})
        return httpx.Response(404, json={})

    from schoolsift.providers import SendEmailCommand

    p = outlook_client(handler)
    result = p.send_email(
        creds(),
        SendEmailCommand(
            idempotency_key="k",
            recipient="a@b.org",
            subject="s",
            body="b",
            draft_id="draft-9",
        ),
    )
    assert result.operation_id == "draft-9"
    assert not any(c == "POST /v1.0/me/messages" for c in calls)


def test_outlook_send_email_bad_draft_id_rejected():
    from schoolsift.providers import SendEmailCommand

    p = outlook_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k",
                recipient="a@b.org",
                subject="s",
                body="b",
                draft_id="../evil",
            ),
        )


def test_outlook_send_email_timeout_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(200, json={"id": "draft-1"})
        raise httpx.ReadTimeout("slow", request=request)

    p = outlook_client(handler)
    with pytest.raises(DeliveryUncertainError) as e:
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )
    assert e.value.operation_id == "draft-1"


def test_outlook_calendar_transaction_id():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "evt-1"})

    from schoolsift.providers import CreateEventCommand

    p = outlook_client(handler)
    result = p.create_calendar_event(
        creds(),
        CreateEventCommand(
            idempotency_key="idem-x",
            title="Trip",
            starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
            ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
        ),
    )
    assert result.operation_id == "evt-1"
    assert calls[0].url.path == "/v1.0/me/events"
    import json

    assert json.loads(calls[0].content)["transactionId"] == "idem-x"


def test_outlook_calendar_timeout_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import CreateEventCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    p = outlook_client(handler)
    with pytest.raises(DeliveryUncertainError):
        p.create_calendar_event(
            creds(),
            CreateEventCommand(
                idempotency_key="k",
                title="t",
                starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
            ),
        )


def test_gmail_send_email_transport_error_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("dropped", request=request)

    p = gmail_client(handler)
    with pytest.raises(DeliveryUncertainError) as e:
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )
    assert e.value.operation_id


def test_gmail_send_email_protocol_error_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("mid-body", request=request)

    p = gmail_client(handler)
    with pytest.raises(DeliveryUncertainError):
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )


def test_gmail_send_email_connect_error_retryable():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    p = gmail_client(handler)
    with pytest.raises(ProviderError) as e:
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )
    assert not isinstance(e.value, DeliveryUncertainError)


def test_outlook_send_email_transport_error_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(200, json={"id": "draft-1"})
        raise httpx.RemoteProtocolError("mid-body", request=request)

    p = outlook_client(handler)
    with pytest.raises(DeliveryUncertainError) as e:
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )
    assert e.value.operation_id == "draft-1"


def test_outlook_send_email_connect_error_retryable():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import SendEmailCommand

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(200, json={"id": "draft-1"})
        raise httpx.ConnectError("refused", request=request)

    p = outlook_client(handler)
    with pytest.raises(ProviderError) as e:
        p.send_email(
            creds(),
            SendEmailCommand(
                idempotency_key="k", recipient="a@b.org", subject="s", body="b"
            ),
        )
    assert not isinstance(e.value, DeliveryUncertainError)


def test_gmail_calendar_transport_error_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import CreateEventCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteError("dropped", request=request)

    p = gmail_client(handler)
    with pytest.raises(DeliveryUncertainError) as e:
        p.create_calendar_event(
            creds(),
            CreateEventCommand(
                idempotency_key="k",
                title="t",
                starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
            ),
        )
    assert e.value.operation_id


def test_gmail_calendar_connect_error_retryable():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import CreateEventCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    p = gmail_client(handler)
    with pytest.raises(ProviderError) as e:
        p.create_calendar_event(
            creds(),
            CreateEventCommand(
                idempotency_key="k",
                title="t",
                starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
            ),
        )
    assert not isinstance(e.value, DeliveryUncertainError)


def test_outlook_calendar_transport_error_uncertain():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import CreateEventCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.CloseError("dropped", request=request)

    p = outlook_client(handler)
    with pytest.raises(DeliveryUncertainError) as e:
        p.create_calendar_event(
            creds(),
            CreateEventCommand(
                idempotency_key="k",
                title="t",
                starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
            ),
        )
    assert e.value.operation_id == "k"


def test_outlook_calendar_connect_error_retryable():
    from schoolsift.errors import DeliveryUncertainError
    from schoolsift.providers import CreateEventCommand

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    p = outlook_client(handler)
    with pytest.raises(ProviderError) as e:
        p.create_calendar_event(
            creds(),
            CreateEventCommand(
                idempotency_key="k",
                title="t",
                starts_at=datetime(2026, 9, 18, 9, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 15, tzinfo=UTC),
            ),
        )
    assert not isinstance(e.value, DeliveryUncertainError)
