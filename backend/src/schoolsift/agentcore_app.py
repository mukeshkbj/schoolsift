from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any

from bedrock_agentcore import BedrockAgentCoreApp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent import AgentDataSource, StrandsMessageAnalyzer
from .errors import AgentNotConfiguredError, BadRequestError

DataSourceFactory = Callable[[], AgentDataSource]


class RuntimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=500)


def create_runtime_app(
    factory: DataSourceFactory | None,
    *,
    model_id: str | None = None,
    region: str | None = None,
) -> BedrockAgentCoreApp:
    app = BedrockAgentCoreApp()

    @app.entrypoint
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        if factory is None or not model_id or not region:
            raise AgentNotConfiguredError("The analysis runtime is not configured.")
        try:
            request = RuntimeInput.model_validate(payload)
        except ValidationError as e:
            raise BadRequestError("Invalid invocation payload.") from e
        analyzer = StrandsMessageAnalyzer(factory(), model_id=model_id, region=region)
        draft = analyzer.analyze(request.household_id, request.message_id)
        return draft.model_dump(mode="json")

    return app


def _env_factory() -> AgentDataSource:
    spec = os.environ.get("SCHOOLSIFT_AGENT_DATA_FACTORY")
    if not spec:
        raise AgentNotConfiguredError("No data source factory is configured.")
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    source: AgentDataSource = factory()
    return source


app = create_runtime_app(
    _env_factory,
    model_id=os.environ.get("SCHOOLSIFT_BEDROCK_MODEL_ID"),
    region=os.environ.get("SCHOOLSIFT_AWS_REGION"),
)

if __name__ == "__main__":
    app.run()
