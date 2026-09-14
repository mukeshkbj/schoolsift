from __future__ import annotations

import base64
import email
import email.header
import email.utils
import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage, Message
from typing import Literal, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, ValidationError

from .credentials import OAuthCredentials
from .documents import html_to_text
from .errors import (
    DeliveryUncertainError,
    FullResyncRequiredError,
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
)
from .models import (
    AttachmentContent,
    FetchedMessage,
    MessageHeader,
    MessagePage,
    NotificationRegistration,
    WebhookSubscription,
)

Provider = Literal["gmail", "outlook"]


class OAuthStart(BaseModel):
    authorization_url: AnyHttpUrl


class SendEmailCommand(BaseModel):
    idempotency_key: str = Field(max_length=500)
    recipient: str = Field(max_length=500)
    subject: str = Field(max_length=1_000)
    body: str = Field(max_length=100_000)
    attachment_name: str | None = Field(default=None, max_length=500)
    attachment_mime: str | None = Field(default=None, max_length=200)
    attachment_bytes: bytes | None = Field(default=None, exclude=True)
    draft_id: str | None = Field(default=None, exclude=True)


class CreateEventCommand(BaseModel):
    idempotency_key: str = Field(max_length=500)
    title: str = Field(max_length=1_000)
    starts_at: datetime
    ends_at: datetime


class OperationResult(BaseModel):
    operation_id: str | None = None


class ProviderIdentity(BaseModel):
    provider_subject: str
    email: str


class OAuthProvider(Protocol):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthCredentials: ...

    def identity(self, credentials: OAuthCredentials) -> ProviderIdentity: ...


class MailProvider(OAuthProvider, Protocol):
    def refresh(self, credentials: OAuthCredentials) -> OAuthCredentials: ...

    def list_message_headers(
        self, credentials: OAuthCredentials, *, cursor: str | None
    ) -> MessagePage: ...

    def fetch_message(
        self, credentials: OAuthCredentials, provider_message_id: str
    ) -> FetchedMessage: ...

    def create_notification_registration(
        self,
        credentials: OAuthCredentials,
        *,
        notification_url: str,
        lifecycle_url: str,
        gmail_topic: str | None,
    ) -> NotificationRegistration: ...

    def renew_notification_registration(
        self,
        credentials: OAuthCredentials,
        existing: WebhookSubscription,
        *,
        notification_url: str,
        lifecycle_url: str,
        gmail_topic: str | None,
    ) -> NotificationRegistration: ...

    def send_email(
        self,
        credentials: OAuthCredentials,
        command: SendEmailCommand,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ) -> OperationResult: ...

    def create_calendar_event(
        self, credentials: OAuthCredentials, command: CreateEventCommand
    ) -> OperationResult: ...


class _TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    scope: str = ""


class _GoogleUserInfo(BaseModel):
    sub: str
    email: str


class _MicrosoftProfile(BaseModel):
    id: str
    mail: str | None = None
    userPrincipalName: str | None = None


class _GmailRef(BaseModel):
    id: str
    threadId: str = ""


class _GmailListPage(BaseModel):
    messages: list[_GmailRef] = []
    nextPageToken: str | None = None


class _GmailHeader(BaseModel):
    name: str
    value: str


class _GmailMetaPayload(BaseModel):
    headers: list[_GmailHeader] = []


class _GmailMeta(BaseModel):
    id: str
    threadId: str = ""
    internalDate: str | None = None
    payload: _GmailMetaPayload = _GmailMetaPayload()


class _GmailRaw(BaseModel):
    raw: str


class _GmailHistoryMessage(BaseModel):
    message: _GmailRef


class _GmailHistoryRecord(BaseModel):
    messagesAdded: list[_GmailHistoryMessage] = []


class _GmailHistoryPage(BaseModel):
    history: list[_GmailHistoryRecord] = []
    nextPageToken: str | None = None
    historyId: str | None = None


class _GmailWatchResponse(BaseModel):
    historyId: str
    expiration: str


class _GmailSent(BaseModel):
    id: str
    threadId: str = ""


class _GraphDraft(BaseModel):
    id: str


class _GraphCreatedEvent(BaseModel):
    id: str


class _GraphSentItem(BaseModel):
    id: str


class _GraphSentItems(BaseModel):
    value: list[_GraphSentItem] = []


class _GraphAddress(BaseModel):
    emailAddress: _GraphEmailAddress


class _GraphEmailAddress(BaseModel):
    name: str = ""
    address: str = ""


class _GraphHead(BaseModel):
    id: str
    conversationId: str = ""
    sender: _GraphAddress | None = None
    replyTo: list[_GraphAddress] = []
    subject: str = ""
    receivedDateTime: datetime


class _GraphDeltaItem(BaseModel):
    id: str
    removed: dict[str, str] | None = Field(default=None, alias="@removed")
    conversationId: str = ""
    sender: _GraphAddress | None = None
    replyTo: list[_GraphAddress] = []
    subject: str = ""
    receivedDateTime: datetime | None = None


class _GraphPage(BaseModel):
    value: list[_GraphDeltaItem] = []
    next_link: str | None = Field(default=None, alias="@odata.nextLink")
    delta_link: str | None = Field(default=None, alias="@odata.deltaLink")


class _GraphSubscription(BaseModel):
    id: str
    expirationDateTime: datetime


class _GraphBody(BaseModel):
    contentType: str = ""
    content: str = ""


class _GraphMessage(_GraphDeltaItem):
    receivedDateTime: datetime
    body: _GraphBody = _GraphBody()


class _GraphAttachment(BaseModel):
    odata_type: str = Field(alias="@odata.type")
    id: str = ""
    name: str = ""
    contentType: str = ""
    contentBytes: str | None = None


class _GraphAttachments(BaseModel):
    value: list[_GraphAttachment] = []
    next_link: str | None = Field(default=None, alias="@odata.nextLink")


def _validate_json(model: type[BaseModel], body: bytes, what: str) -> BaseModel:
    try:
        return model.model_validate_json(body)
    except ValidationError as e:
        raise ProviderError(f"Provider returned an unexpected {what} shape.") from e


def _credentials(body: bytes) -> OAuthCredentials:
    parsed = _validate_json(_TokenResponse, body, "token")
    assert isinstance(parsed, _TokenResponse)
    expires = (
        datetime.now(UTC) + timedelta(seconds=parsed.expires_in)
        if parsed.expires_in
        else None
    )
    refresh = SecretStr(parsed.refresh_token) if parsed.refresh_token else None
    scopes = tuple(s for s in parsed.scope.split(" ") if s)
    access = SecretStr(parsed.access_token)
    return OAuthCredentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        scopes=scopes,
    )


def _post(
    http: httpx.Client,
    url: str,
    data: dict[str, str],
    *,
    auth_on_client_error: bool = False,
) -> httpx.Response:
    try:
        res = http.post(url, data=data)
        res.raise_for_status()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403) or (auth_on_client_error and code < 500):
            raise ProviderAuthError(
                "Provider authorization failed; reconnect the account."
            ) from e
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    except httpx.HTTPError as e:
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    return res


def _get(
    http: httpx.Client,
    url: str,
    token: SecretStr,
    params: Mapping[str, str | list[str]] | None = None,
    *,
    resync_on: tuple[int, ...] = (),
) -> httpx.Response:
    try:
        res = http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        )
        res.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in resync_on:
            raise FullResyncRequiredError(
                "The stored sync checkpoint expired; a full resync is required."
            ) from e
        if e.response.status_code in (401, 403):
            raise ProviderAuthError(
                "Provider authorization failed; reconnect the account."
            ) from e
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    except httpx.HTTPError as e:
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    return res


def _json_request(
    http: httpx.Client,
    method: str,
    url: str,
    token: SecretStr,
    payload: Mapping[str, object],
    *,
    not_found: bool = False,
    conflict_ok: bool = False,
    uncertain_op: str | None = None,
) -> httpx.Response:
    try:
        res = http.request(
            method,
            url,
            json=dict(payload),
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        )
        res.raise_for_status()
    except httpx.TransportError as e:
        if uncertain_op is not None and not isinstance(e, httpx.ConnectError):
            raise DeliveryUncertainError(
                "The provider may have completed this action;"
                " it will not be retried automatically.",
                operation_id=uncertain_op,
            ) from e
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if not_found and code == 404:
            raise ProviderNotFoundError(
                "The provider no longer recognizes this resource."
            ) from e
        if conflict_ok and code == 409:
            return e.response
        if code in (401, 403):
            raise ProviderAuthError(
                "Provider authorization failed; reconnect the account."
            ) from e
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    except httpx.HTTPError as e:
        raise ProviderError(f"Provider request failed: {type(e).__name__}") from e
    return res


def _refresh(
    http: httpx.Client,
    url: str,
    client_id: str,
    client_secret: str,
    credentials: OAuthCredentials,
    extra: dict[str, str] | None = None,
) -> OAuthCredentials:
    stored = credentials.refresh_token
    if stored is None:
        raise ProviderAuthError(
            "No refresh credential is stored; reconnect the account."
        )
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": stored.get_secret_value(),
        "grant_type": "refresh_token",
        **(extra or {}),
    }
    fresh = _credentials(_post(http, url, data, auth_on_client_error=True).content)
    if fresh.refresh_token is None:
        fresh.refresh_token = stored
    return fresh


def _decode_header(value: str) -> str:
    """Decode RFC 2047 encoded words (=?charset?Q?...?=) into readable text."""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return value


def _header_map(meta: _GmailMeta) -> dict[str, str]:
    return {h.name.lower(): h.value for h in meta.payload.headers}


def _received_at(internal_ms: str | None, headers: dict[str, str]) -> datetime:
    if internal_ms:
        return datetime.fromtimestamp(int(internal_ms) / 1000, UTC)
    raw = headers.get("date")
    if raw:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    raise ProviderError("Provider message had no usable date.")


def _plain_text(msg: Message) -> str:
    fallback = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if ctype == "text/plain":
            return text
        if ctype == "text/html" and not fallback:
            fallback = html_to_text(text)
    return fallback


def _attachments(provider_message_id: str, msg: Message) -> list[AttachmentContent]:
    found: list[AttachmentContent] = []
    for i, part in enumerate(msg.walk()):
        name = part.get_filename()
        if not name:
            continue
        content = part.get_payload(decode=True)
        if not isinstance(content, bytes):
            continue
        found.append(
            AttachmentContent(
                provider_attachment_id=f"{provider_message_id}:{i}",
                name=name,
                mime=part.get_content_type(),
                content=content,
            )
        )
    return found


_SAFE_SUB_ID = re.compile(r"^[\w-]{1,200}$")


def _graph_next_link(url: str) -> str:
    if not url.startswith("https://graph.microsoft.com/v1.0/"):
        raise ProviderError("Provider returned an untrusted pagination link.")
    return url


class GmailOAuthProvider:
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    CODE_EXCHANGE_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    HISTORY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
    WATCH_URL = "https://gmail.googleapis.com/gmail/v1/users/me/watch"
    CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

    def __init__(self, http: httpx.Client, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GMAIL_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthCredentials:
        res = _post(
            self._http,
            self.CODE_EXCHANGE_URL,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        return _credentials(res.content)

    def identity(self, credentials: OAuthCredentials) -> ProviderIdentity:
        res = _get(self._http, self.USERINFO_URL, credentials.access_token)
        info = _validate_json(_GoogleUserInfo, res.content, "profile")
        assert isinstance(info, _GoogleUserInfo)
        return ProviderIdentity(provider_subject=info.sub, email=info.email)

    def refresh(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return _refresh(
            self._http,
            self.CODE_EXCHANGE_URL,
            self._client_id,
            self._client_secret,
            credentials,
        )

    def _header(self, credentials: OAuthCredentials, ref: _GmailRef) -> MessageHeader:
        res = _get(
            self._http,
            f"{self.MESSAGES_URL}/{ref.id}",
            credentials.access_token,
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Reply-To", "Subject", "Date"],
            },
        )
        meta = _validate_json(_GmailMeta, res.content, "message")
        assert isinstance(meta, _GmailMeta)
        headers = _header_map(meta)
        name, addr = email.utils.parseaddr(headers.get("from", ""))
        if not addr:
            raise ProviderError("Provider message had no sender.")
        _, reply_addr = email.utils.parseaddr(headers.get("reply-to", ""))
        return MessageHeader(
            provider_message_id=meta.id,
            thread_id=meta.threadId or ref.threadId,
            sender_name=_decode_header(name) or addr,
            sender_email=addr.lower(),
            reply_to=(reply_addr or addr).lower(),
            subject=_decode_header(headers.get("subject", "")),
            received_at=_received_at(meta.internalDate, headers),
        )

    def list_message_headers(
        self, credentials: OAuthCredentials, *, cursor: str | None
    ) -> MessagePage:
        if cursor is None or cursor.startswith("page:"):
            return self._initial_page(credentials, cursor)
        if cursor.startswith("history:") or cursor.startswith("page-history:"):
            return self._history_page(credentials, cursor)
        raise ProviderError("Unrecognized Gmail sync cursor.")

    def _initial_page(
        self, credentials: OAuthCredentials, cursor: str | None
    ) -> MessagePage:
        params = {"q": "newer_than:30d", "maxResults": "100"}
        if cursor is not None:
            continuation = cursor.removeprefix("page:")
            if not continuation:
                raise ProviderError("Unrecognized Gmail sync cursor.")
            params["pageToken"] = continuation
        res = _get(self._http, self.MESSAGES_URL, credentials.access_token, params)
        page = _validate_json(_GmailListPage, res.content, "message list")
        assert isinstance(page, _GmailListPage)
        return MessagePage(
            messages=[self._header(credentials, ref) for ref in page.messages],
            next_cursor=(
                f"page:{page.nextPageToken}" if page.nextPageToken is not None else None
            ),
            has_more=page.nextPageToken is not None,
        )

    def _history_page(self, credentials: OAuthCredentials, cursor: str) -> MessagePage:
        params: dict[str, str | list[str]] = {
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
            "maxResults": "100",
        }
        if cursor.startswith("history:"):
            start = cursor.removeprefix("history:")
            if not start.isdigit():
                raise ProviderError("Unrecognized Gmail sync cursor.")
            params["startHistoryId"] = start
        else:
            rest = cursor.removeprefix("page-history:")
            start, sep, token = rest.partition(":")
            if not sep or not start.isdigit() or not token:
                raise ProviderError("Unrecognized Gmail sync cursor.")
            params["startHistoryId"] = start
            params["pageToken"] = token
        res = _get(
            self._http,
            self.HISTORY_URL,
            credentials.access_token,
            params,
            resync_on=(404,),
        )
        page = _validate_json(_GmailHistoryPage, res.content, "history list")
        assert isinstance(page, _GmailHistoryPage)
        seen: set[str] = set()
        refs: list[_GmailRef] = []
        for record in page.history:
            for added in record.messagesAdded:
                if added.message.id not in seen:
                    seen.add(added.message.id)
                    refs.append(added.message)
        if page.nextPageToken is not None:
            next_cursor = (
                f"page-history:{params['startHistoryId']}:{page.nextPageToken}"
            )
            has_more = True
        elif page.historyId is not None:
            next_cursor = f"history:{page.historyId}"
            has_more = False
        else:
            next_cursor = None
            has_more = False
        return MessagePage(
            messages=[self._header(credentials, ref) for ref in refs],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _watch(
        self, credentials: OAuthCredentials, gmail_topic: str | None
    ) -> NotificationRegistration:
        if gmail_topic is None:
            raise ProviderError("Gmail notifications are not configured.")
        res = _json_request(
            self._http,
            "POST",
            self.WATCH_URL,
            credentials.access_token,
            {
                "topicName": gmail_topic,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "include",
            },
        )
        parsed = _validate_json(_GmailWatchResponse, res.content, "watch")
        assert isinstance(parsed, _GmailWatchResponse)
        try:
            expires_ms = int(parsed.expiration)
        except ValueError as e:
            raise ProviderError("Provider returned an unexpected watch shape.") from e
        return NotificationRegistration(
            provider_subscription_id="",
            cursor=f"history:{parsed.historyId}",
            expires_at=datetime.fromtimestamp(expires_ms / 1000, UTC),
        )

    def create_notification_registration(
        self,
        credentials: OAuthCredentials,
        *,
        notification_url: str,
        lifecycle_url: str,
        gmail_topic: str | None,
    ) -> NotificationRegistration:
        return self._watch(credentials, gmail_topic)

    def renew_notification_registration(
        self,
        credentials: OAuthCredentials,
        existing: WebhookSubscription,
        *,
        notification_url: str,
        lifecycle_url: str,
        gmail_topic: str | None,
    ) -> NotificationRegistration:
        return self._watch(credentials, gmail_topic)

    def fetch_message(
        self, credentials: OAuthCredentials, provider_message_id: str
    ) -> FetchedMessage:
        res = _get(
            self._http,
            f"{self.MESSAGES_URL}/{provider_message_id}",
            credentials.access_token,
            params={"format": "raw"},
        )
        raw = _validate_json(_GmailRaw, res.content, "message")
        assert isinstance(raw, _GmailRaw)
        try:
            content = base64.urlsafe_b64decode(raw.raw + "==")
        except Exception as e:
            raise ProviderError("Provider returned undecodable content.") from e
        msg = email.message_from_bytes(content)
        meta = _header_map(
            _GmailMeta(
                id=provider_message_id,
                threadId=msg.get("Thread-Id", ""),
                payload=_GmailMetaPayload(
                    headers=[
                        _GmailHeader(name=k, value=v)
                        for k, v in msg.items()
                        if k.lower()
                        in ("from", "reply-to", "subject", "date", "thread-id")
                    ]
                ),
            )
        )
        name, addr = email.utils.parseaddr(meta.get("from", ""))
        _, reply_addr = email.utils.parseaddr(meta.get("reply-to", ""))
        return FetchedMessage(
            provider_message_id=provider_message_id,
            thread_id=meta.get("thread-id", ""),
            sender_name=_decode_header(name) or addr,
            sender_email=addr.lower(),
            reply_to=(reply_addr or addr).lower(),
            subject=_decode_header(meta.get("subject", "")),
            received_at=_received_at(None, meta),
            body_text=_plain_text(msg),
            attachments=_attachments(provider_message_id, msg),
        )

    def send_email(
        self,
        credentials: OAuthCredentials,
        command: SendEmailCommand,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ) -> OperationResult:
        msg = EmailMessage()
        msg["To"] = command.recipient
        msg["Subject"] = command.subject
        digest = hashlib.sha256(command.idempotency_key.encode()).hexdigest()
        msg["Message-ID"] = f"<{digest}@schoolsift.invalid>"
        msg.set_content(command.body)
        if command.attachment_bytes is not None:
            maintype, _, subtype = (
                command.attachment_mime or "application/octet-stream"
            ).partition("/")
            msg.add_attachment(
                command.attachment_bytes,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=command.attachment_name or "attachment",
            )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        res = _json_request(
            self._http,
            "POST",
            f"{self.MESSAGES_URL}/send",
            credentials.access_token,
            {"raw": raw},
            uncertain_op=digest,
        )
        sent = _validate_json(_GmailSent, res.content, "sent message")
        assert isinstance(sent, _GmailSent)
        if not sent.id:
            raise ProviderError("Provider returned an unexpected send shape.")
        return OperationResult(operation_id=sent.id)

    def create_calendar_event(
        self, credentials: OAuthCredentials, command: CreateEventCommand
    ) -> OperationResult:
        event_id = (
            base64.b32hexencode(
                hashlib.sha256(command.idempotency_key.encode()).digest()
            )
            .decode()
            .lower()[:26]
        )
        body = {
            "id": event_id,
            "summary": command.title,
            "start": {
                "dateTime": command.starts_at.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": command.ends_at.isoformat(),
                "timeZone": "UTC",
            },
        }
        res = _json_request(
            self._http,
            "POST",
            f"{self.CALENDAR_URL}?sendUpdates=none",
            credentials.access_token,
            body,
            conflict_ok=True,
            uncertain_op=event_id,
        )
        if res.status_code == 409:
            existing = _get(
                self._http,
                f"{self.CALENDAR_URL}/{event_id}",
                credentials.access_token,
                {"sendUpdates": "none"},
            )
            found = _validate_json(_GraphCreatedEvent, existing.content, "event")
            assert isinstance(found, _GraphCreatedEvent)
            if not found.id:
                raise ProviderError("Provider returned an unexpected event shape.")
        return OperationResult(operation_id=event_id)


class OutlookOAuthProvider:
    AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    CODE_EXCHANGE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    PROFILE_URL = "https://graph.microsoft.com/v1.0/me"
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, http: httpx.Client, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(OUTLOOK_SCOPES),
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthCredentials:
        res = _post(
            self._http,
            self.CODE_EXCHANGE_URL,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": " ".join(OUTLOOK_SCOPES),
            },
        )
        return _credentials(res.content)

    def identity(self, credentials: OAuthCredentials) -> ProviderIdentity:
        res = _get(self._http, self.PROFILE_URL, credentials.access_token)
        profile = _validate_json(_MicrosoftProfile, res.content, "profile")
        assert isinstance(profile, _MicrosoftProfile)
        email = profile.mail or profile.userPrincipalName
        if email is None:
            raise ProviderError("Provider profile did not include an email.")
        return ProviderIdentity(provider_subject=profile.id, email=email)

    def refresh(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return _refresh(
            self._http,
            self.CODE_EXCHANGE_URL,
            self._client_id,
            self._client_secret,
            credentials,
            {"scope": " ".join(OUTLOOK_SCOPES)},
        )

    def list_message_headers(
        self, credentials: OAuthCredentials, *, cursor: str | None
    ) -> MessagePage:
        if cursor is not None:
            url = _graph_next_link(cursor)
            res = self._graph_get(credentials, url, None, resync_on=(410,))
        else:
            res = self._graph_get(
                credentials,
                f"{self.GRAPH}/me/mailFolders/inbox/messages/delta",
                {
                    "$top": "100",
                    "$select": (
                        "id,conversationId,sender,replyTo,subject,receivedDateTime"
                    ),
                },
                resync_on=(410,),
            )
        page = _validate_json(_GraphPage, res.content, "message list")
        assert isinstance(page, _GraphPage)
        if page.next_link is not None:
            next_cursor: str | None = _graph_next_link(page.next_link)
            has_more = True
        elif page.delta_link is not None:
            next_cursor = _graph_next_link(page.delta_link)
            has_more = False
        else:
            next_cursor = None
            has_more = False
        return MessagePage(
            messages=[self._to_header(m) for m in page.value if m.removed is None],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _graph_get(
        self,
        credentials: OAuthCredentials,
        url: str,
        params: Mapping[str, str | list[str]] | None,
        *,
        resync_on: tuple[int, ...] = (),
    ) -> httpx.Response:
        return _get(
            self._http,
            url,
            credentials.access_token,
            params,
            resync_on=resync_on,
        )

    def _to_header(self, m: _GraphDeltaItem) -> MessageHeader:
        if m.receivedDateTime is None:
            raise ProviderError("Provider message had no usable date.")
        if m.sender is None or not m.sender.emailAddress.address:
            raise ProviderError("Provider message had no sender.")
        addr = m.sender.emailAddress.address
        reply = (
            m.replyTo[0].emailAddress.address
            if m.replyTo and m.replyTo[0].emailAddress.address
            else addr
        )
        return MessageHeader(
            provider_message_id=m.id,
            thread_id=m.conversationId,
            sender_name=m.sender.emailAddress.name or addr,
            sender_email=addr.lower(),
            reply_to=reply.lower(),
            subject=m.subject,
            received_at=m.receivedDateTime,
        )

    def fetch_message(
        self, credentials: OAuthCredentials, provider_message_id: str
    ) -> FetchedMessage:
        res = self._graph_get(
            credentials,
            f"{self.GRAPH}/me/messages/{provider_message_id}",
            {
                "$select": (
                    "id,conversationId,sender,replyTo,subject,receivedDateTime,body"
                )
            },
        )
        msg = _validate_json(_GraphMessage, res.content, "message")
        assert isinstance(msg, _GraphMessage)
        head = self._to_header(msg)
        body = (
            html_to_text(msg.body.content)
            if msg.body.contentType.lower() == "html"
            else msg.body.content
        )
        res = self._graph_get(
            credentials,
            f"{self.GRAPH}/me/messages/{provider_message_id}/attachments",
            {"$top": "50"},
        )
        page = _validate_json(_GraphAttachments, res.content, "attachment list")
        assert isinstance(page, _GraphAttachments)
        attachments: list[AttachmentContent] = []
        for att in page.value:
            if (
                att.odata_type == "#microsoft.graph.fileAttachment"
                and att.contentBytes is not None
            ):
                try:
                    content = base64.b64decode(att.contentBytes)
                except Exception as e:
                    raise ProviderError(
                        "Provider returned undecodable attachment content."
                    ) from e
                attachments.append(
                    AttachmentContent(
                        provider_attachment_id=att.id,
                        name=att.name,
                        mime=att.contentType or "application/octet-stream",
                        content=content,
                    )
                )
            else:
                raise ProviderError(
                    f"Attachment '{att.name}' has a type SchoolSift cannot"
                    " read; review it in your inbox."
                )
        return FetchedMessage(
            **head.model_dump(), body_text=body, attachments=attachments
        )

    def _subscription_urls(
        self, notification_url: str, lifecycle_url: str
    ) -> tuple[str, str]:
        for url in (notification_url, lifecycle_url):
            if not url.startswith("https://"):
                raise ProviderError("Notification endpoints must be HTTPS.")
        return notification_url, lifecycle_url

    def create_notification_registration(
        self,
        credentials: OAuthCredentials,
        *,
        notification_url: str,
        lifecycle_url: str,
        gmail_topic: str | None,
    ) -> NotificationRegistration:
        notify, lifecycle = self._subscription_urls(notification_url, lifecycle_url)
        client_state = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(days=2, hours=23)
        res = _json_request(
            self._http,
            "POST",
            f"{self.GRAPH}/subscriptions",
            credentials.access_token,
            {
                "changeType": "created",
                "resource": "/me/mailFolders('inbox')/messages",
                "notificationUrl": notify,
                "lifecycleNotificationUrl": lifecycle,
                "expirationDateTime": expires.isoformat().replace("+00:00", "Z"),
                "clientState": client_state,
            },
        )
        parsed = _validate_json(_GraphSubscription, res.content, "subscription")
        assert isinstance(parsed, _GraphSubscription)
        return NotificationRegistration(
            provider_subscription_id=parsed.id,
            client_state_hash=hashlib.sha256(client_state.encode()).hexdigest(),
            expires_at=parsed.expirationDateTime,
        )

    def renew_notification_registration(
        self,
        credentials: OAuthCredentials,
        existing: WebhookSubscription,
        *,
        notification_url: str,
        lifecycle_url: str,
        gmail_topic: str | None,
    ) -> NotificationRegistration:
        if not _SAFE_SUB_ID.fullmatch(existing.provider_subscription_id):
            raise ProviderError("Unrecognized provider subscription id.")
        expires = datetime.now(UTC) + timedelta(days=2, hours=23)
        res = _json_request(
            self._http,
            "PATCH",
            f"{self.GRAPH}/subscriptions/{existing.provider_subscription_id}",
            credentials.access_token,
            {"expirationDateTime": expires.isoformat().replace("+00:00", "Z")},
            not_found=True,
        )
        parsed = _validate_json(_GraphSubscription, res.content, "subscription")
        assert isinstance(parsed, _GraphSubscription)
        return NotificationRegistration(
            provider_subscription_id=parsed.id,
            client_state_hash=existing.client_state_hash,
            expires_at=parsed.expirationDateTime,
        )

    def _sent_items_confirm(
        self, credentials: OAuthCredentials, internet_id: str
    ) -> bool:
        try:
            res = self._graph_get(
                credentials,
                f"{self.GRAPH}/me/mailFolders/sentitems/messages",
                {
                    "$filter": f"internetMessageId eq '{internet_id}'",
                    "$top": "1",
                    "$select": "id,internetMessageId",
                },
            )
            page = _validate_json(_GraphSentItems, res.content, "sent items")
            assert isinstance(page, _GraphSentItems)
        except Exception:
            return False
        return len(page.value) > 0

    def send_email(
        self,
        credentials: OAuthCredentials,
        command: SendEmailCommand,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ) -> OperationResult:
        digest = hashlib.sha256(command.idempotency_key.encode()).hexdigest()
        internet_id = f"<{digest}@schoolsift.invalid>"
        draft_id = command.draft_id
        if draft_id is None:
            payload: dict[str, object] = {
                "subject": command.subject,
                "body": {"contentType": "Text", "content": command.body},
                "toRecipients": [{"emailAddress": {"address": command.recipient}}],
                "internetMessageId": internet_id,
            }
            if command.attachment_bytes is not None:
                payload["attachments"] = [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": command.attachment_name or "attachment",
                        "contentType": (
                            command.attachment_mime or "application/octet-stream"
                        ),
                        "contentBytes": base64.b64encode(
                            command.attachment_bytes
                        ).decode(),
                    }
                ]
            res = _json_request(
                self._http,
                "POST",
                f"{self.GRAPH}/me/messages",
                credentials.access_token,
                payload,
            )
            draft = _validate_json(_GraphDraft, res.content, "draft")
            assert isinstance(draft, _GraphDraft)
            if not _SAFE_SUB_ID.fullmatch(draft.id):
                raise ProviderError("Provider returned an unexpected draft shape.")
            draft_id = draft.id
            if checkpoint is not None:
                checkpoint(draft_id)
        elif not _SAFE_SUB_ID.fullmatch(draft_id):
            raise ProviderError("Unrecognized provider draft id.")
        _json_request(
            self._http,
            "POST",
            f"{self.GRAPH}/me/messages/{draft_id}/send",
            credentials.access_token,
            {},
            uncertain_op=draft_id,
        )
        if self._sent_items_confirm(credentials, internet_id):
            return OperationResult(operation_id=draft_id)
        raise DeliveryUncertainError(
            "The send was accepted but delivery could not be confirmed.",
            operation_id=draft_id,
        )

    def create_calendar_event(
        self, credentials: OAuthCredentials, command: CreateEventCommand
    ) -> OperationResult:
        res = _json_request(
            self._http,
            "POST",
            f"{self.GRAPH}/me/events",
            credentials.access_token,
            {
                "subject": command.title,
                "start": {
                    "dateTime": command.starts_at.isoformat(),
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": command.ends_at.isoformat(),
                    "timeZone": "UTC",
                },
                "transactionId": command.idempotency_key,
            },
            uncertain_op=command.idempotency_key,
        )
        created = _validate_json(_GraphCreatedEvent, res.content, "event")
        assert isinstance(created, _GraphCreatedEvent)
        if not created.id:
            raise ProviderError("Provider returned an unexpected event shape.")
        return OperationResult(operation_id=created.id)


GMAIL_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)

OUTLOOK_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "Mail.Read",
    "Mail.Send",
    "Calendars.ReadWrite",
)
