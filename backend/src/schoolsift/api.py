from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .demo_store import DEFAULT_DEMO_DIR, DemoOutcome, DemoState, DemoStore
from .domain import ActionPacket, ProposalPayload, ProposalVersion
from .errors import SchoolSiftError

ALLOWED_ORIGINS = ["http://localhost:3210", "http://127.0.0.1:3210"]


class ProcessRequest(BaseModel):
    message_ids: list[str] | None = None


class EditRequest(BaseModel):
    expected_version: int
    payload: ProposalPayload


class DecisionRequest(BaseModel):
    payload_hash: str


class ProcessResponse(BaseModel):
    packets: list[ActionPacket]


class ApproveResponse(BaseModel):
    version: ProposalVersion
    outcome: DemoOutcome


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def create_app(demo_dir: Path | None = None) -> FastAPI:
    store = DemoStore(demo_dir or DEFAULT_DEMO_DIR)
    app = FastAPI(title="SchoolSift Demo API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["GET", "POST"],
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "demo"}

    @app.get("/v1/demo")
    def get_demo() -> DemoState:
        return store.state()

    @app.post("/v1/demo/reset")
    def reset_demo() -> DemoState:
        store.reset()
        return store.state()

    @app.post("/v1/demo/process")
    def process(req: ProcessRequest) -> ProcessResponse:
        return ProcessResponse(packets=store.process(req.message_ids))

    @app.post("/v1/demo/proposals/{proposal_id}/versions")
    def edit(proposal_id: str, req: EditRequest) -> ProposalVersion:
        return store.edit(
            proposal_id, expected_version=req.expected_version, payload=req.payload
        )

    @app.post("/v1/demo/proposals/{proposal_id}/versions/{version}/approve")
    def approve(
        proposal_id: str, version: int, req: DecisionRequest
    ) -> ApproveResponse:
        approved, outcome = store.approve(
            proposal_id, version=version, payload_hash=req.payload_hash
        )
        return ApproveResponse(version=approved, outcome=outcome)

    @app.post("/v1/demo/proposals/{proposal_id}/versions/{version}/reject")
    def reject(proposal_id: str, version: int, req: DecisionRequest) -> ProposalVersion:
        return store.reject(proposal_id, version=version, payload_hash=req.payload_hash)

    return app
