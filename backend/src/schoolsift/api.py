from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import AnyHttpUrl, BaseModel, Field, ValidationError

from .agent import MessageAnalyzer, StrandsMessageAnalyzer
from .agent_data import StoredAgentDataSource
from .config import Settings
from .content import ContentStore, LocalEncryptedContentStore
from .credentials import CredentialVault, KeyringCredentialVault
from .domain import ActionPacket, ProposalPayload, ProposalVersion
from .errors import (
    AgentNotConfiguredError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ProviderNotConfiguredError,
    SchoolSiftError,
    VaultError,
    WebhookAuthError,
    WebhookNotConfiguredError,
    WebhookValidationError,
)
from .execution import (
    ExecutionQueue,
    MemoryExecutionQueue,
    dispatch_pending_executions,
    execute_command,
)
from .identity import (
    LOCAL_PRINCIPAL,
    CognitoIdTokenVerifier,
    Principal,
    TokenVerifier,
)
from .models import (
    ApprovalResponse,
    Child,
    ConnectionView,
    DispatchResult,
    ExecutionCommand,
    ExecutionRecord,
    Household,
    Invitation,
    Membership,
    MessageRecord,
    Role,
    RunRequest,
    SchoolSource,
    SubscriptionPublic,
    SyncResult,
)
from .notifications import (
    IngestionEvent,
    IngestionEventKind,
    IngestionQueue,
    SQLiteIngestionQueue,
)
from .processing import process_message
from .providers import (
    GmailOAuthProvider,
    MailProvider,
    OAuthStart,
    OutlookOAuthProvider,
    Provider,
)
from .sqlite_store import SQLiteStore
from .store import SchoolSiftStore
from .subscription_service import ensure_subscription
from .sync import sync_connection
from .webhooks import (
    GmailPushPayload,
    GoogleOIDCPushVerifier,
    GooglePushVerifier,
    GraphNotification,
    GraphNotificationBatch,
    PubSubEnvelope,
)

ProviderRegistry = Mapping[Provider, MailProvider]

MAX_WEBHOOK_BODY = 64 * 1024
MAX_GRAPH_BODY = 128 * 1024
MAX_PUSH_PAYLOAD = 16 * 1024

_GRAPH_LIFECYCLE: dict[str, IngestionEventKind] = {
    "missed": "missed",
    "reauthorizationRequired": "reauthorization_required",
    "subscriptionRemoved": "subscription_removed",
}


def _graph_event_kind(note: GraphNotification) -> IngestionEventKind | None:
    if note.lifecycleEvent:
        return _GRAPH_LIFECYCLE.get(note.lifecycleEvent)
    if note.changeType in ("created", "updated"):
        return "mail_changed"
    return None


class Capabilities(BaseModel):
    gmail: bool
    outlook: bool
    agent: bool
    aws: bool


class BootstrapResponse(BaseModel):
    mode: Literal["local", "aws"]
    capabilities: Capabilities
    household: Household | None
    active_household_id: str | None
    memberships: list[Membership]
    members: list[Membership]
    invitations: list[Invitation]
    children: list[Child]
    connections: list[ConnectionView]
    sources: list[SchoolSource]
    messages: list[MessageRecord]
    packets: list[ActionPacket]
    executions: list[ExecutionRecord]


class HouseholdRequest(BaseModel):
    name: str
    timezone: str


class ChildRequest(BaseModel):
    name: str
    school: str
    grade: str


class EditRequest(BaseModel):
    expected_version: int
    payload: ProposalPayload


class DecisionRequest(BaseModel):
    payload_hash: str


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["editor", "viewer"]


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class MemberRoleRequest(BaseModel):
    role: Role


class InvitationCreateResponse(BaseModel):
    invitation: Invitation
    token: str


class AcceptResponse(BaseModel):
    membership: Membership


INVITATION_TTL_DAYS = 7


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _default_providers(settings: Settings) -> dict[Provider, MailProvider]:
    registry: dict[Provider, MailProvider] = {}
    if settings.gmail_client_id and settings.gmail_client_secret:
        registry["gmail"] = GmailOAuthProvider(
            httpx.Client(timeout=20),
            settings.gmail_client_id,
            settings.gmail_client_secret,
        )
    if settings.outlook_client_id and settings.outlook_client_secret:
        registry["outlook"] = OutlookOAuthProvider(
            httpx.Client(timeout=20),
            settings.outlook_client_id,
            settings.outlook_client_secret,
        )
    return registry


def _parse_provider(value: str) -> Provider:
    if value == "gmail":
        return "gmail"
    if value == "outlook":
        return "outlook"
    raise NotFoundError(f"Unknown provider: {value}")


def create_app(
    settings: Settings | None = None,
    store: SchoolSiftStore | None = None,
    providers: ProviderRegistry | None = None,
    vault: CredentialVault | None = None,
    content: ContentStore | None = None,
    analyzer: MessageAnalyzer | None = None,
    ingestion_queue: IngestionQueue | None = None,
    execution_queue: ExecutionQueue | None = None,
    google_push_verifier: GooglePushVerifier | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if store is None:
        store = SQLiteStore(settings.database_path)
    store.initialize()
    if providers is None:
        providers = _default_providers(settings)
    if vault is None:
        vault = KeyringCredentialVault()
    if content is None:
        content = LocalEncryptedContentStore(settings.content_directory)
    if analyzer is None and settings.aws_region and settings.bedrock_model_id:
        analyzer = StrandsMessageAnalyzer(
            StoredAgentDataSource(store, content),
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
        )
    queue = ingestion_queue or SQLiteIngestionQueue(store)
    exec_queue = execution_queue or MemoryExecutionQueue()
    if (
        google_push_verifier is None
        and settings.gmail_push_audience
        and settings.gmail_push_service_account
    ):
        google_push_verifier = GoogleOIDCPushVerifier()
    if settings.auth_mode == "cognito" and token_verifier is None:
        import jwt

        token_verifier = CognitoIdTokenVerifier(
            issuer=str(settings.cognito_issuer),
            app_client_id=str(settings.cognito_audience),
            key_provider=jwt.PyJWKClient(
                f"{settings.cognito_issuer}/.well-known/jwks.json"
            ),
        )
    app = FastAPI(title="SchoolSift API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "DELETE", "PATCH"],
        allow_headers=["*"],
    )

    @app.exception_handler(SchoolSiftError)
    async def domain_errors(_: Request, exc: SchoolSiftError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_errors(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(422, "VALIDATION_ERROR", str(exc.errors()[0].get("msg")))

    def _principal(request: Request) -> Principal:
        if settings.auth_mode == "local":
            return LOCAL_PRINCIPAL
        if token_verifier is None:
            raise AuthenticationError("Sign-in is not configured.")
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("Sign in to continue.")
        return token_verifier.verify(token.strip())

    def _membership_or_none(request: Request) -> Membership | None:
        principal = _principal(request)
        household_id = request.headers.get("x-schoolsift-household")
        if household_id:
            membership = store.get_membership(household_id, principal.user_id)
            if membership is None:
                raise ForbiddenError("You do not have access to that household.")
            return membership
        memberships = store.list_memberships_for_user(principal.user_id)
        if len(memberships) == 1:
            return memberships[0]
        if memberships:
            raise ConflictError("Select a household to continue.")
        return None

    def _membership(request: Request) -> Membership:
        membership = _membership_or_none(request)
        if membership is None:
            raise ConflictError("Set up a household first.")
        return membership

    def _require_edit(membership: Membership) -> None:
        if membership.role == "viewer":
            raise ForbiddenError("Viewers cannot change this household.")

    def _require_owner(membership: Membership) -> None:
        if membership.role != "owner":
            raise ForbiddenError("Only a household owner can do that.")

    def require_provider(provider: str) -> MailProvider:
        parsed = _parse_provider(provider)
        adapter = providers.get(parsed)
        if adapter is None:
            raise ProviderNotConfiguredError(
                f"{parsed} OAuth is not configured on this API."
            )
        return adapter

    def callback_uri(provider: str) -> str:
        base = str(settings.public_api_url).rstrip("/")
        return f"{base}/v1/connections/{provider}/callback"

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": settings.environment}

    @app.get("/v1/bootstrap")
    def bootstrap(request: Request) -> BootstrapResponse:
        principal = _principal(request)
        memberships = store.list_memberships_for_user(principal.user_id)
        requested = request.headers.get("x-schoolsift-household")
        active: Membership | None = None
        if requested:
            active = store.get_membership(requested, principal.user_id)
            if active is None:
                raise ForbiddenError("You do not have access to that household.")
        elif len(memberships) == 1:
            active = memberships[0]
        household = store.get_household_by_id(active.household_id) if active else None
        hid = active.household_id if active else None
        members: list[Membership] = []
        invitations: list[Invitation] = []
        if active is not None and active.role == "owner":
            members = store.list_memberships(active.household_id)
            invitations = store.list_invitations(active.household_id)
        return BootstrapResponse(
            mode=settings.environment,
            capabilities=Capabilities(
                gmail="gmail" in providers,
                outlook="outlook" in providers,
                agent=analyzer is not None,
                aws=settings.environment == "aws",
            ),
            household=household,
            active_household_id=hid,
            memberships=memberships,
            members=members,
            invitations=invitations,
            children=store.list_children(hid) if hid else [],
            connections=_connection_views(hid) if hid else [],
            sources=store.list_sources(hid) if hid else [],
            messages=store.list_messages(hid) if hid else [],
            packets=store.list_packets(hid) if hid else [],
            executions=store.list_executions(hid) if hid else [],
        )

    @app.post("/v1/household")
    def create_household(request: Request, req: HouseholdRequest) -> Household:
        principal = _principal(request)
        return store.create_household_for_owner(
            principal, name=req.name, timezone=req.timezone
        )

    @app.get("/v1/members")
    def members(request: Request) -> list[Membership]:
        membership = _membership_or_none(request)
        return store.list_memberships(membership.household_id) if membership else []

    @app.get("/v1/invitations")
    def invitations(request: Request) -> list[Invitation]:
        membership = _membership(request)
        _require_owner(membership)
        return store.list_invitations(membership.household_id)

    @app.post("/v1/invitations")
    def invite(request: Request, req: InviteRequest) -> InvitationCreateResponse:
        membership = _membership(request)
        _require_owner(membership)
        invitation, invite_code = store.create_invitation(
            membership.household_id,
            email=req.email,
            role=req.role,
            expires_at=datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS),
        )
        return InvitationCreateResponse(invitation=invitation, token=invite_code)

    @app.delete("/v1/invitations/{invitation_id}", status_code=204)
    def revoke_invitation(request: Request, invitation_id: str) -> Response:
        membership = _membership(request)
        _require_owner(membership)
        store.revoke_invitation(membership.household_id, invitation_id)
        return Response(status_code=204)

    @app.post("/v1/invitations/accept")
    def accept_invitation(request: Request, req: AcceptInviteRequest) -> AcceptResponse:
        principal = _principal(request)
        membership = store.accept_invitation(principal, req.token)
        return AcceptResponse(membership=membership)

    @app.patch("/v1/members/{user_id}")
    def change_member_role(
        request: Request, user_id: str, req: MemberRoleRequest
    ) -> Membership:
        membership = _membership(request)
        _require_owner(membership)
        return store.change_member_role(membership.household_id, user_id, req.role)

    @app.delete("/v1/members/{user_id}", status_code=204)
    def remove_member(request: Request, user_id: str) -> Response:
        membership = _membership(request)
        _require_owner(membership)
        store.remove_member(membership.household_id, user_id)
        return Response(status_code=204)

    @app.post("/v1/children")
    def add_child(request: Request, req: ChildRequest) -> Child:
        membership = _membership(request)
        _require_edit(membership)
        return store.add_child(
            membership.household_id,
            name=req.name,
            school=req.school,
            grade=req.grade,
        )

    def _connection_views(household_id: str | None) -> list[ConnectionView]:
        if household_id is None:
            return []
        views: list[ConnectionView] = []
        for conn in store.list_connections(household_id):
            sub = store.get_subscription_for_connection(household_id, conn.id)
            public = (
                SubscriptionPublic(
                    provider=sub.provider,
                    status=sub.status,
                    expires_at=sub.expires_at,
                )
                if sub is not None
                else None
            )
            views.append(ConnectionView(**conn.model_dump(), subscription=public))
        return views

    @app.get("/v1/connections")
    def connections(request: Request) -> list[ConnectionView]:
        membership = _membership_or_none(request)
        return _connection_views(membership.household_id if membership else None)

    @app.post("/v1/connections/{connection_id}/notifications")
    def enable_notifications(
        request: Request, connection_id: str
    ) -> SubscriptionPublic:
        membership = _membership(request)
        _require_edit(membership)
        subscription = ensure_subscription(
            membership.household_id,
            connection_id,
            store=store,
            vault=vault,
            providers=providers,
            settings=settings,
        )
        return SubscriptionPublic(
            provider=subscription.provider,
            status=subscription.status,
            expires_at=subscription.expires_at,
        )

    @app.post("/v1/connections/{provider}/authorize")
    def authorize(request: Request, provider: str) -> OAuthStart:
        membership = _membership(request)
        _require_edit(membership)
        parsed = _parse_provider(provider)
        adapter = require_provider(parsed)
        state = store.create_oauth_state(membership.household_id, parsed)
        url = adapter.authorization_url(state=state, redirect_uri=callback_uri(parsed))
        return OAuthStart(authorization_url=AnyHttpUrl(url))

    @app.get("/v1/connections/{provider}/callback")
    def callback(provider: str, code: str, state: str) -> RedirectResponse:
        parsed = _parse_provider(provider)
        adapter = require_provider(parsed)
        consumed = store.consume_oauth_state(state)
        if consumed.provider != parsed:
            raise ConflictError("OAuth state does not match this provider.")
        credentials = adapter.exchange_code(
            code=code, redirect_uri=callback_uri(parsed)
        )
        identity = adapter.identity(credentials)
        prior = next(
            (
                c
                for c in store.list_connections(consumed.household_id)
                if c.provider == parsed
                and c.provider_subject == identity.provider_subject
            ),
            None,
        )
        connection = store.upsert_connection(
            consumed.household_id,
            provider=parsed,
            provider_subject=identity.provider_subject,
            email=identity.email,
        )
        try:
            vault.put(connection.id, credentials)
        except Exception as e:
            if prior is not None:
                store.restore_connection(
                    consumed.household_id,
                    connection.id,
                    email=prior.email,
                    status=prior.status,
                )
            else:
                store.set_connection_status(
                    consumed.household_id, connection.id, "disconnected"
                )
            raise VaultError(
                "Could not store account credentials; nothing was connected."
            ) from e
        try:
            store.set_connection_status(
                consumed.household_id, connection.id, "connected"
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                vault.delete(connection.id)
            raise VaultError("Could not finalize the account connection.") from e
        target = str(settings.public_web_url).rstrip("/")
        return RedirectResponse(f"{target}/app?connected={parsed}", status_code=303)

    @app.delete("/v1/connections/{connection_id}", status_code=204)
    def disconnect(request: Request, connection_id: str) -> Response:
        membership = _membership(request)
        _require_edit(membership)
        store.disconnect_connection(membership.household_id, connection_id)
        vault.delete(connection_id)
        return Response(status_code=204)

    @app.post("/v1/connections/{connection_id}/sync")
    def sync(request: Request, connection_id: str) -> SyncResult:
        membership = _membership(request)
        _require_edit(membership)
        return sync_connection(
            membership.household_id,
            connection_id,
            store=store,
            vault=vault,
            content=content,
            providers=providers,
        )

    @app.get("/v1/sources")
    def sources(request: Request) -> list[SchoolSource]:
        membership = _membership_or_none(request)
        return store.list_sources(membership.household_id) if membership else []

    @app.post("/v1/sources/{source_id}/confirm")
    def confirm_source(request: Request, source_id: str) -> SchoolSource:
        membership = _membership(request)
        _require_edit(membership)
        return store.set_source_status(membership.household_id, source_id, "confirmed")

    @app.post("/v1/sources/{source_id}/reject")
    def reject_source(request: Request, source_id: str) -> SchoolSource:
        membership = _membership(request)
        _require_edit(membership)
        return store.set_source_status(membership.household_id, source_id, "rejected")

    @app.get("/v1/messages")
    def messages(request: Request) -> list[MessageRecord]:
        membership = _membership_or_none(request)
        return store.list_messages(membership.household_id) if membership else []

    @app.post("/v1/messages/{message_id}/process")
    def process(request: Request, message_id: str) -> ActionPacket:
        membership = _membership(request)
        _require_edit(membership)
        if analyzer is None:
            raise AgentNotConfiguredError(
                "Set SCHOOLSIFT_AWS_REGION and SCHOOLSIFT_BEDROCK_MODEL_ID"
                " on the API to enable processing."
            )
        return process_message(
            membership.household_id, message_id, store=store, analyzer=analyzer
        )

    @app.post("/v1/messages/{message_id}/retry")
    def retry_message(request: Request, message_id: str) -> MessageRecord:
        membership = _membership(request)
        _require_edit(membership)
        return store.reset_failed_message(membership.household_id, message_id)

    @app.get("/v1/action-packets")
    def action_packets(request: Request) -> list[ActionPacket]:
        membership = _membership_or_none(request)
        return store.list_packets(membership.household_id) if membership else []

    @app.get("/v1/action-packets/{packet_id}")
    def packet(request: Request, packet_id: str) -> ActionPacket:
        membership = _membership(request)
        return store.get_packet(membership.household_id, packet_id)

    @app.post("/v1/proposals/{proposal_id}/versions")
    def edit(request: Request, proposal_id: str, req: EditRequest) -> ProposalVersion:
        membership = _membership(request)
        _require_edit(membership)
        return store.edit_proposal(
            membership.household_id,
            proposal_id,
            expected_version=req.expected_version,
            payload=req.payload,
        )

    @app.post("/v1/proposals/{proposal_id}/versions/{version}/approve")
    def approve(
        request: Request, proposal_id: str, version: int, req: DecisionRequest
    ) -> ApprovalResponse:
        membership = _membership(request)
        _require_edit(membership)
        proposal, execution = store.approve_proposal(
            membership.household_id,
            proposal_id,
            version=version,
            payload_hash=req.payload_hash,
        )
        return ApprovalResponse(proposal=proposal, execution=execution)

    @app.post("/v1/proposals/{proposal_id}/versions/{version}/reject")
    def reject(
        request: Request, proposal_id: str, version: int, req: DecisionRequest
    ) -> ProposalVersion:
        membership = _membership(request)
        _require_edit(membership)
        return store.reject_proposal(
            membership.household_id,
            proposal_id,
            version=version,
            payload_hash=req.payload_hash,
        )

    @app.get("/v1/executions")
    def executions(request: Request) -> list[ExecutionRecord]:
        membership = _membership_or_none(request)
        return store.list_executions(membership.household_id) if membership else []

    @app.post("/v1/executions/dispatch")
    def dispatch(request: Request) -> DispatchResult:
        membership = _membership(request)
        _require_edit(membership)
        if settings.environment != "local":
            raise NotFoundError("Unknown route.")
        return DispatchResult(dispatched=dispatch_pending_executions(store, exec_queue))

    @app.post("/v1/executions/{execution_id}/run")
    def run_execution(
        request: Request, execution_id: str, req: RunRequest
    ) -> ExecutionRecord:
        membership = _membership(request)
        _require_edit(membership)
        if settings.environment != "local":
            raise NotFoundError("Unknown route.")
        if req.confirm is not True:
            raise BadRequestError(
                "Pass confirm=true to run this approved action against "
                "the connected provider account."
            )
        result = execute_command(
            ExecutionCommand(
                execution_id=execution_id,
                household_id=membership.household_id,
            ),
            store=store,
            vault=vault,
            content=content,
            providers=providers,
        )
        if result is not None:
            return result
        return store.get_execution(membership.household_id, execution_id)

    @app.post("/v1/webhooks/gmail")
    async def gmail_webhook(request: Request) -> Response:
        if (
            google_push_verifier is None
            or not settings.gmail_push_audience
            or not settings.gmail_push_service_account
        ):
            raise WebhookNotConfiguredError(
                "Gmail push notifications are not configured on this API."
            )
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise WebhookAuthError("A valid bearer credential is required.")
        try:
            claims = google_push_verifier.verify(
                token.strip(), audience=settings.gmail_push_audience
            )
        except Exception as e:
            raise WebhookAuthError("The push credential could not be verified.") from e
        account = str(claims.get("email") or "")
        if claims.get("email_verified") is not True or not hmac.compare_digest(
            account, settings.gmail_push_service_account
        ):
            raise WebhookAuthError("The push credential could not be verified.")
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BODY:
            raise WebhookValidationError("Webhook payload is too large.")
        try:
            envelope = PubSubEnvelope.model_validate_json(body)
        except ValidationError as e:
            raise WebhookValidationError("Malformed push envelope.") from e
        try:
            decoded = base64.b64decode(envelope.message.data, validate=True)
        except ValueError as e:
            raise WebhookValidationError("Malformed push payload.") from e
        if len(decoded) > MAX_PUSH_PAYLOAD:
            raise WebhookValidationError("Webhook payload is too large.")
        try:
            push = GmailPushPayload.model_validate_json(decoded)
        except ValidationError as e:
            raise WebhookValidationError("Malformed push payload.") from e
        connection = store.find_connection_by_email("gmail", push.emailAddress)
        if connection is not None:
            queue.enqueue(
                IngestionEvent(
                    id=f"evt-{secrets.token_hex(8)}",
                    provider="gmail",
                    household_id=connection.household_id,
                    connection_id=connection.id,
                    kind="mail_changed",
                    provider_cursor=f"history:{push.historyId}",
                    received_at=datetime.now(UTC),
                ),
                dedupe_key=f"gmail:{envelope.message.messageId}",
            )
        return Response(status_code=204)

    @app.post("/v1/webhooks/outlook")
    async def outlook_webhook(request: Request) -> Response:
        handshake = request.query_params.get("validationToken")
        if handshake is not None:
            if not 1 <= len(handshake) <= 512:
                raise WebhookValidationError("Invalid validation token.")
            return Response(
                content=handshake,
                status_code=200,
                media_type="text/plain",
            )
        body = await request.body()
        if len(body) > MAX_GRAPH_BODY:
            raise WebhookValidationError("Webhook payload is too large.")
        try:
            batch = GraphNotificationBatch.model_validate_json(body)
        except ValidationError as e:
            raise WebhookValidationError("Malformed notification payload.") from e
        for note in batch.value:
            subscription = store.get_webhook_subscription(
                "outlook", note.subscriptionId
            )
            if subscription is None or subscription.status != "active":
                continue
            if not hmac.compare_digest(
                hashlib.sha256(note.clientState.encode()).hexdigest(),
                subscription.client_state_hash,
            ):
                continue
            kind = _graph_event_kind(note)
            if kind is None:
                continue
            dedupe = hashlib.sha256(
                json.dumps(
                    [
                        note.subscriptionId,
                        note.lifecycleEvent or note.changeType,
                        note.resource,
                        note.resourceData.id if note.resourceData else "",
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            queue.enqueue(
                IngestionEvent(
                    id=f"evt-{secrets.token_hex(8)}",
                    provider="outlook",
                    household_id=subscription.household_id,
                    connection_id=subscription.connection_id,
                    kind=kind,
                    provider_cursor=(
                        note.resourceData.id if note.resourceData else None
                    ),
                    received_at=datetime.now(UTC),
                ),
                dedupe_key=f"outlook:{dedupe}",
            )
        return Response(status_code=202)

    return app
