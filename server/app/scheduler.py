from __future__ import annotations

import asyncio
import random

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import SessionRecord, SessionStatus, Worker, WorkerState


class Scheduler:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.allocation_lock = asyncio.Lock()

    async def reserve(self, session_id: str) -> str | None:
        async with self.allocation_lock:
            with self.sessions() as database:
                session = database.get(SessionRecord, session_id)
                if session is None or session.status != SessionStatus.QUEUED.value:
                    return None
                candidates = list(
                    (
                        database.scalars(
                            select(Worker).where(
                                Worker.state == WorkerState.IDLE.value,
                                Worker.session_id.is_(None),
                            )
                        )
                    ).all()
                )
                if not candidates:
                    return None
                worker = random.choice(candidates)
                worker.state = WorkerState.RESERVED.value
                worker.session_id = session.id
                worker.current_artifact_id = session.artifact_id
                worker.fpga_ready = 0
                session.worker_id = worker.board
                session.status = SessionStatus.RESERVED.value
                database.commit()
                return worker.board

    async def oldest_queued(self) -> str | None:
        with self.sessions() as database:
            return database.scalar(
                select(SessionRecord.id)
                .where(SessionRecord.status == SessionStatus.QUEUED.value)
                .order_by(SessionRecord.created_at, SessionRecord.id)
                .limit(1)
            )
