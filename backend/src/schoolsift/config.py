from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, model_validator

Environment = Literal["local", "aws"]
AuthMode = Literal["local", "cognito"]

DEFAULT_ORIGINS = ("http://localhost:3210", "http://127.0.0.1:3210")


class Settings(BaseModel):
    environment: Environment = "local"
    auth_mode: AuthMode = "local"
    database_path: Path = Path(".schoolsift/schoolsift.db")
    content_directory: Path = Path(".schoolsift/content")
    allowed_origins: tuple[str, ...] = DEFAULT_ORIGINS
    aws_region: str | None = None
    bedrock_model_id: str | None = None
    cognito_issuer: str | None = None
    cognito_audience: str | None = None
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    outlook_client_id: str | None = None
    outlook_client_secret: str | None = None
    gmail_push_audience: str | None = None
    gmail_push_service_account: str | None = None
    gmail_topic: str | None = None
    public_web_url: AnyHttpUrl = AnyHttpUrl("http://localhost:3210")
    public_api_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")

    @model_validator(mode="after")
    def _aws_requires_full_configuration(self) -> Settings:
        push_pair = (self.gmail_push_audience, self.gmail_push_service_account)
        if (push_pair[0] is None) != (push_pair[1] is None):
            raise ValueError(
                "gmail_push_audience and gmail_push_service_account"
                " must be configured together"
            )
        if self.auth_mode == "cognito" and (
            self.cognito_issuer is None or self.cognito_audience is None
        ):
            raise ValueError(
                "auth_mode='cognito' requires cognito_issuer and cognito_audience"
            )
        if self.environment != "aws":
            return self
        if self.auth_mode != "cognito":
            raise ValueError("environment='aws' requires auth_mode='cognito'")
        missing = [
            name
            for name in (
                "aws_region",
                "bedrock_model_id",
                "cognito_issuer",
                "cognito_audience",
                "gmail_push_audience",
                "gmail_push_service_account",
                "gmail_topic",
            )
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError("environment='aws' requires: " + ", ".join(missing))
        if not str(self.public_api_url).startswith("https://"):
            raise ValueError("environment='aws' requires an https public_api_url")
        return self

    @classmethod
    def from_env(cls) -> Settings:
        origins = os.environ.get("SCHOOLSIFT_ALLOWED_ORIGINS", "")
        return cls(
            environment=os.environ.get("SCHOOLSIFT_ENVIRONMENT", "local"),  # type: ignore[arg-type]
            auth_mode=os.environ.get("SCHOOLSIFT_AUTH_MODE", "local"),  # type: ignore[arg-type]
            database_path=Path(
                os.environ.get("SCHOOLSIFT_DATABASE_PATH", ".schoolsift/schoolsift.db")
            ),
            content_directory=Path(
                os.environ.get("SCHOOLSIFT_CONTENT_DIRECTORY", ".schoolsift/content")
            ),
            allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip())
            or DEFAULT_ORIGINS,
            aws_region=os.environ.get("SCHOOLSIFT_AWS_REGION"),
            bedrock_model_id=os.environ.get("SCHOOLSIFT_BEDROCK_MODEL_ID"),
            cognito_issuer=os.environ.get("SCHOOLSIFT_COGNITO_ISSUER"),
            cognito_audience=os.environ.get("SCHOOLSIFT_COGNITO_AUDIENCE"),
            gmail_client_id=os.environ.get("GMAIL_CLIENT_ID"),
            gmail_client_secret=os.environ.get("GMAIL_CLIENT_SECRET"),
            outlook_client_id=os.environ.get("OUTLOOK_CLIENT_ID"),
            outlook_client_secret=os.environ.get("OUTLOOK_CLIENT_SECRET"),
            gmail_push_audience=os.environ.get("SCHOOLSIFT_GMAIL_PUSH_AUDIENCE"),
            gmail_push_service_account=os.environ.get(
                "SCHOOLSIFT_GMAIL_PUSH_SERVICE_ACCOUNT"
            ),
            gmail_topic=os.environ.get("SCHOOLSIFT_GMAIL_TOPIC"),
            public_web_url=os.environ.get(  # type: ignore[arg-type]
                "SCHOOLSIFT_PUBLIC_WEB_URL", "http://localhost:3210"
            ),
            public_api_url=os.environ.get(  # type: ignore[arg-type]
                "SCHOOLSIFT_PUBLIC_API_URL", "http://127.0.0.1:8000"
            ),
        )
