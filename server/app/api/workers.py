from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..admin_auth import require_admin_token
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


@router.post(
    "/{board}/release",
    response_model=WorkerReleaseResponse,
    dependencies=[Depends(require_admin_token)],
)
async def release_worker(
    board: str,
    request: Request,
) -> WorkerReleaseResponse:
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
    request.app.state.services.audit.record(
        "ADMIN_WORKER_RELEASE",
        level="WARNING",
        actor_type="admin",
        student_id=student_id,
        board=board,
        message="Administrator released Worker lease",
    )
    return WorkerReleaseResponse(
        released=True, board=board, student_id=student_id
    )
