from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from ..db_models import SessionRecord, SessionStatus
from ..schemas import PredictRequest, SessionCreate, SessionResponse
from ..session_manager import (
    ArtifactNotFoundError,
    ArtifactOwnershipError,
    SessionConflictError,
    SessionManager,
    SessionNotFoundError,
    WorkerOperationError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _response(session: SessionRecord) -> SessionResponse:
    return SessionResponse(
        session_id=session.id,
        student_id=session.student_id,
        artifact_id=session.artifact_id,
        status=session.status.lower(),
        worker=session.worker_id,
        request_count=session.request_count,
        error=session.error,
    )


def _manager(request: Request) -> SessionManager:
    return request.app.state.services.session_manager


@router.post("", response_model=SessionResponse)
async def create_session(
    body: SessionCreate, request: Request, response: Response
) -> SessionResponse:
    try:
        session = await _manager(request).create_session(body.student_id, body.artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    except ArtifactOwnershipError as exc:
        raise HTTPException(status_code=403, detail="artifact belongs to another student") from exc
    except SessionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkerOperationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    response.status_code = (
        status.HTTP_202_ACCEPTED
        if session.status == SessionStatus.QUEUED.value
        else status.HTTP_201_CREATED
    )
    return _response(session)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, request: Request) -> SessionResponse:
    try:
        session = await _manager(request).get_session(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return _response(session)


@router.post("/{session_id}/predict")
async def predict(
    session_id: str, body: PredictRequest, request: Request
) -> dict[str, object]:
    try:
        return await _manager(request).predict(session_id, body.payload)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkerOperationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/{session_id}", response_model=SessionResponse)
async def release_session(session_id: str, request: Request) -> SessionResponse:
    try:
        session = await _manager(request).release(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return _response(session)
