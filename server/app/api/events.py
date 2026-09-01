from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..audit import AUDIT_EVENT_TYPES, AuditLogger
from ..schemas import AuditEventResponse, AuditEventTypeResponse

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/types", response_model=list[AuditEventTypeResponse])
async def list_event_types() -> list[AuditEventTypeResponse]:
    return [
        AuditEventTypeResponse(value=value, label=label)
        for value, label in AUDIT_EVENT_TYPES
    ]


@router.get("", response_model=list[AuditEventResponse])
async def list_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    event_type: str | None = Query(default=None, min_length=1, max_length=64),
    level: str | None = Query(default=None, min_length=1, max_length=16),
    student_id: str | None = Query(default=None, min_length=1, max_length=128),
    board: str | None = Query(default=None, min_length=1, max_length=64),
    artifact_id: str | None = Query(default=None, min_length=1, max_length=64),
    request_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> list[AuditEventResponse]:
    audit: AuditLogger = request.app.state.services.audit
    events = audit.list_events(
        limit=limit,
        event_type=event_type,
        level=level,
        student_id=student_id,
        board=board,
        artifact_id=artifact_id,
        request_id=request_id,
    )
    return [AuditEventResponse.model_validate(event, from_attributes=True) for event in events]
