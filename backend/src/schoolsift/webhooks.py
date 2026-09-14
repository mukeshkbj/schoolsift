from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class GooglePushVerifier(Protocol):
    def verify(self, bearer_token: str, *, audience: str) -> Mapping[str, object]: ...


class GoogleOIDCPushVerifier:
    def verify(self, bearer_token: str, *, audience: str) -> Mapping[str, object]:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            bearer_token, google_requests.Request(), audience=audience
        )
        return dict(claims)


class PubSubMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messageId: str = Field(min_length=1, max_length=500)
    data: str = Field(min_length=1, max_length=20_000)
    publishTime: str = Field(min_length=1, max_length=100)


class PubSubEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: PubSubMessage
    subscription: str = Field(min_length=1, max_length=1_000)


class GmailPushPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emailAddress: str = Field(min_length=1, max_length=500)
    historyId: int = Field(gt=0)


class GraphResourceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", max_length=2_000)


class GraphNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscriptionId: str = Field(min_length=1, max_length=500)
    clientState: str = Field(default="", max_length=1_000)
    changeType: str = Field(default="", max_length=100)
    resource: str = Field(default="", max_length=2_000)
    resourceData: GraphResourceData | None = None
    lifecycleEvent: str | None = Field(default=None, max_length=100)


class GraphNotificationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: list[GraphNotification] = Field(max_length=100)
