from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..db_models import PredictRequestRecord, StudentLease, Worker

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    sessions = request.app.state.services.database.sessions
    with sessions() as database:
        workers = list(database.scalars(select(Worker)).all())
        lease_rows = list(database.scalars(select(StudentLease)).all())
        request_rows = list(database.scalars(select(PredictRequestRecord)).all())
    worker_counts = Counter(worker.state.lower() for worker in workers)
    lease_counts = Counter(lease.state.lower() for lease in lease_rows)
    request_counts = Counter(item.status.lower() for item in request_rows)
    return {
        "ok": True,
        "workers": {"total": len(workers), **dict(sorted(worker_counts.items()))},
        "leases": dict(sorted(lease_counts.items())),
        "requests": dict(sorted(request_counts.items())),
    }
