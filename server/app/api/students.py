from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status as http_status

from ..lease_manager import LeaseManager
from ..schemas import PasswordChangeRequest, PasswordChangeResponse, StudentStatusResponse
from ..student_auth import (
    InvalidStudentCredentialsError,
    PasswordPolicyError,
    StudentAuth,
)

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/{student_id}/status", response_model=StudentStatusResponse)
async def status(
    student_id: str,
    request: Request,
    student_password: str | None = Header(default=None, alias="X-Student-Password"),
) -> StudentStatusResponse:
    await _authenticate(request, student_id, student_password)
    manager: LeaseManager = request.app.state.services.lease_manager
    return StudentStatusResponse(**await manager.student_status(student_id))


@router.post("/{student_id}/password", response_model=PasswordChangeResponse)
async def change_password(
    student_id: str,
    body: PasswordChangeRequest,
    request: Request,
    student_password: str | None = Header(default=None, alias="X-Student-Password"),
) -> PasswordChangeResponse:
    auth: StudentAuth = request.app.state.services.student_auth
    try:
        await auth.change_password(student_id, student_password, body.new_password)
    except InvalidStudentCredentialsError as exc:
        request.app.state.services.audit.record(
            "AUTH_FAILED",
            level="WARNING",
            student_id=student_id,
            message="Student authentication failed",
        )
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid student credentials",
        ) from exc
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.services.audit.record(
        "STUDENT_PASSWORD_CHANGED",
        level="WARNING",
        student_id=student_id,
        message="Student password changed",
    )
    return PasswordChangeResponse(student_id=student_id, password_changed=True)


async def _authenticate(
    request: Request, student_id: str, password: str | None
) -> None:
    auth: StudentAuth = request.app.state.services.student_auth
    try:
        await auth.authenticate(student_id, password)
    except InvalidStudentCredentialsError as exc:
        request.app.state.services.audit.record(
            "AUTH_FAILED",
            level="WARNING",
            student_id=student_id,
            message="Student authentication failed",
        )
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid student credentials",
        ) from exc
