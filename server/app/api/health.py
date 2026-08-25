from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..db_models import SessionRecord, Worker

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    sessions = request.app.state.services.database.sessions
    with sessions() as database:
        workers = list(database.scalars(select(Worker)).all())
        session_rows = list(database.scalars(select(SessionRecord)).all())
    worker_counts = Counter(worker.state.lower() for worker in workers)
    session_counts = Counter(session.status.lower() for session in session_rows)
    return {
        "ok": True,
        "workers": {"total": len(workers), **dict(sorted(worker_counts.items()))},
        "sessions": dict(sorted(session_counts.items())),
    }
