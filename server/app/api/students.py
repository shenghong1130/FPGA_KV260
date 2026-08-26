from __future__ import annotations

from fastapi import APIRouter, Request

from ..lease_manager import LeaseManager
from ..schemas import StudentStatusResponse

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/{student_id}/status", response_model=StudentStatusResponse)
async def status(student_id: str, request: Request) -> StudentStatusResponse:
    manager: LeaseManager = request.app.state.services.lease_manager
    return StudentStatusResponse(**await manager.student_status(student_id))
