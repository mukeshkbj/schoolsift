from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from schoolsift.config import Settings


def test_defaults_are_local_and_empty():
    s = Settings()
    assert s.environment == "local"
    assert s.database_path == Path(".schoolsift/schoolsift.db")
    assert "http://localhost:3210" in s.allowed_origins
    assert s.aws_region is None
    assert s.gmail_client_id is None
    assert str(s.public_web_url).rstrip("/") == "http://localhost:3210"


def test_aws_mode_fails_closed_without_region_and_model():
    with pytest.raises(ValidationError):
        Settings(environment="aws")
    with pytest.raises(ValidationError):
        Settings(environment="aws", aws_region="us-west-2")


def test_aws_mode_requires_auth_configuration():
    with pytest.raises(ValidationError):
        Settings(
            environment="aws",
            aws_region="us-west-2",
            bedrock_model_id="model-x",
        )
    ok = Settings(
        environment="aws",
        auth_mode="cognito",
        aws_region="us-west-2",
        bedrock_model_id="model-x",
        cognito_issuer="https://cognito-idp.us-west-2.amazonaws.com/pool",
        cognito_audience="client-id",
        gmail_push_audience="aud",
        gmail_push_service_account="push@gserviceaccount.com",
        gmail_topic="projects/p/topics/t",
        public_api_url="https://api.example.com",
    )
    assert ok.environment == "aws"


def test_aws_mode_requires_cognito_auth():
    with pytest.raises(ValidationError):
        Settings(
            environment="aws",
            auth_mode="local",
            aws_region="us-west-2",
            bedrock_model_id="model-x",
            cognito_issuer="https://cognito-idp.us-west-2.amazonaws.com/pool",
            cognito_audience="client-id",
            gmail_push_audience="aud",
            gmail_push_service_account="push@gserviceaccount.com",
            gmail_topic="projects/p/topics/t",
            public_api_url="https://api.example.com",
        )


def test_cognito_mode_requires_pool_configuration():
    with pytest.raises(ValidationError):
        Settings(auth_mode="cognito")
    with pytest.raises(ValidationError):
        Settings(
            auth_mode="cognito",
            cognito_issuer="https://cognito-idp.us-west-2.amazonaws.com/pool",
        )


def test_push_settings_must_be_paired():
    with pytest.raises(ValidationError):
        Settings(gmail_push_audience="aud")
    with pytest.raises(ValidationError):
        Settings(gmail_push_service_account="push@gserviceaccount.com")
    ok = Settings(
        gmail_push_audience="aud",
        gmail_push_service_account="push@gserviceaccount.com",
    )
    assert ok.gmail_push_audience == "aud"


def test_from_env_reads(monkeypatch, tmp_path):
    db = tmp_path / "x.db"
    monkeypatch.setenv("SCHOOLSIFT_DATABASE_PATH", str(db))
    monkeypatch.setenv(
        "SCHOOLSIFT_ALLOWED_ORIGINS", "http://localhost:3210, http://x.test"
    )
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gid")
    s = Settings.from_env()
    assert s.database_path == db
    assert s.allowed_origins == ("http://localhost:3210", "http://x.test")
    assert s.gmail_client_id == "gid"
