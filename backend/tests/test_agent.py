from __future__ import annotations

from datetime import UTC, datetime

import botocore.exceptions
import pytest
from botocore.exceptions import ClientError

from schoolsift.agent import StrandsMessageAnalyzer
from schoolsift.errors import ModelAccessError
from schoolsift.models import (
    CalendarRecord,
    DocumentContent,
    HouseholdContext,
    NormalizedMessage,
    RelatedAction,
)


class FakeSource:
    def get_household_context(self, household_id: str) -> HouseholdContext:
        return HouseholdContext(
            household_id=household_id,
            children=["Kid"],
            connections=["me@example.com"],
        )

    def get_message(self, household_id: str, message_id: str) -> NormalizedMessage:
        return NormalizedMessage(
            message_id=message_id,
            thread_id="thread-1",
            sender_email="o@example.org",
            reply_to="o@example.org",
            subject="Question",
            received_at=datetime(2026, 9, 10, tzinfo=UTC),
            source_confirmed=True,
            body="Body text.",
            attachment_ids=[],
        )

    def get_document(self, household_id: str, document_id: str) -> DocumentContent:
        raise KeyError(document_id)

    def find_related_actions(
        self, household_id: str, thread_key: str
    ) -> list[RelatedAction]:
        return []

    def find_calendar_conflicts(
        self, household_id: str, start: datetime, end: datetime
    ) -> list[CalendarRecord]:
        return []


class RaisingAgent:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def structured_output(self, *args: object, **kwargs: object) -> object:
        raise self.exc


def analyzer_raising(exc: BaseException) -> StrandsMessageAnalyzer:
    analyzer = StrandsMessageAnalyzer(FakeSource(), model_id="m", region="us-east-1")
    analyzer._agent = RaisingAgent(exc)
    return analyzer


def client_error(code: str, message: str = "sensitive raw AWS detail") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            client_error(
                "ResourceNotFoundException",
                "Model use case details have not been submitted",
            ),
            "Bedrock model access is not set up for this AWS account"
            " (Anthropic use case form).",
        ),
        (
            client_error("ResourceNotFoundException", "model not found"),
            "Bedrock request failed (ResourceNotFoundException).",
        ),
        (
            client_error("AccessDeniedException"),
            "This AWS account cannot invoke the Bedrock model yet (access denied).",
        ),
        (
            client_error(
                "ValidationException",
                "with on-demand throughput isn't supported; an inference"
                " profile is required",
            ),
            "The configured Bedrock model id needs a cross-region"
            " inference profile id.",
        ),
        (
            client_error("ThrottlingException"),
            "Bedrock is busy; try again in a moment.",
        ),
        (
            client_error("ServiceUnavailableException"),
            "Bedrock is busy; try again in a moment.",
        ),
        (
            client_error("ModelNotReadyException"),
            "Bedrock is busy; try again in a moment.",
        ),
        (
            client_error("SomethingElseException"),
            "Bedrock request failed (SomethingElseException).",
        ),
        (
            botocore.exceptions.NoCredentialsError(),
            "AWS credentials are missing; Bedrock cannot be called.",
        ),
        (
            botocore.exceptions.EndpointConnectionError(
                endpoint_url="https://bedrock.example"
            ),
            "Bedrock is unreachable; check the AWS region and network.",
        ),
    ],
)
def test_bedrock_failures_map_to_model_access(
    exc: BaseException, expected: str
) -> None:
    analyzer = analyzer_raising(exc)
    with pytest.raises(ModelAccessError) as caught:
        analyzer.analyze("hh-1", "msg-1")
    assert caught.value.status_code == 503
    assert caught.value.code == "MODEL_UNAVAILABLE"
    assert caught.value.message == expected
    assert "sensitive raw AWS detail" not in caught.value.message


def test_non_bedrock_error_passes_through() -> None:
    analyzer = analyzer_raising(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        analyzer.analyze("hh-1", "msg-1")
