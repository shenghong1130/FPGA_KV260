from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import WorkerResponse
from ..lease_manager import LeaseManager

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
