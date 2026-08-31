from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Request, status

from ..lease_manager import (
    LeaseManager,
    WorkerNotFoundError,
    WorkerNotSafelyReleasableError,
    WorkerReleaseError,
)
from ..schemas import WorkerReleaseResponse, WorkerResponse

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=list[WorkerResponse])
async def list_workers(request: Request) -> list[WorkerResponse]:
    manager: LeaseManager = request.app.state.services.lease_manager
    workers = await manager.list_workers()
    return [
        WorkerResponse(
            board=worker.board,
            state=worker.state.lower(),
            lease_id=worker.lease_id,
            student_id=student_id,
            artifact_id=worker.current_artifact_id,
            fpga_ready=bool(worker.fpga_ready),
            last_seen=worker.last_seen,
            last_error=worker.last_error,
        )
        for worker, student_id in workers
    ]


@router.post("/{board}/release", response_model=WorkerReleaseResponse)
async def release_worker(
    board: str,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> WorkerReleaseResponse:
    configured_token = request.app.state.services.settings.admin_action_token
    if configured_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="manual admin actions are not configured",
        )
    if admin_token is None or not hmac.compare_digest(admin_token, configured_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin action token",
        )

    manager: LeaseManager = request.app.state.services.lease_manager
    try:
        student_id = await manager.release_worker(
            board, f"manual UI release: {board}"
        )
    except WorkerNotFoundError as exc:
        raise HTTPException(status_code=404, detail="worker not found") from exc
    except WorkerNotSafelyReleasableError as exc:
        raise HTTPException(
            status_code=409, detail="worker is not safely releasable"
        ) from exc
    except WorkerReleaseError as exc:
        raise HTTPException(
            status_code=502, detail=f"worker release failed: {exc}"
        ) from exc
    return WorkerReleaseResponse(
        released=True, board=board, student_id=student_id
    )
