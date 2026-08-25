from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import WorkerResponse
from ..session_manager import SessionManager

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=list[WorkerResponse])
async def list_workers(request: Request) -> list[WorkerResponse]:
    manager: SessionManager = request.app.state.services.session_manager
    workers = await manager.list_workers()
    return [
        WorkerResponse(
            board=worker.board,
            state=worker.state.lower(),
            session_id=worker.session_id,
            artifact_id=worker.current_artifact_id,
            fpga_ready=bool(worker.fpga_ready),
            last_seen=worker.last_seen,
            last_error=worker.last_error,
        )
        for worker in workers
    ]
