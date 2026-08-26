from __future__ import annotations

import asyncio
import random
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import LeaseStatus, StudentLease, Worker, WorkerState, utcnow


class Scheduler:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.allocation_lock = asyncio.Lock()

    async def reserve_lease(self, student_id: str) -> str | None:
        async with self.allocation_lock:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                if lease is None or lease.state != LeaseStatus.QUEUED.value:
                    return None
                oldest = database.scalar(select(StudentLease.student_id).where(
                    StudentLease.state == LeaseStatus.QUEUED.value,
                ).order_by(StudentLease.queued_at, StudentLease.student_id).limit(1))
                if oldest != student_id:
                    return None
                candidates = list(database.scalars(select(Worker).where(
                    Worker.state == WorkerState.IDLE.value,
                    Worker.lease_id.is_(None),
                )).all())
                if not candidates:
                    return None
                worker = random.choice(candidates)
                lease_id = f"lease_{uuid.uuid4().hex}"
                worker.state = WorkerState.RESERVED.value
                worker.lease_id = lease_id
                worker.current_artifact_id = None
                worker.fpga_ready = 0
                lease.lease_id = lease_id
                lease.worker_id = worker.board
                lease.state = LeaseStatus.RESERVED.value
                lease.last_activity_at = utcnow()
                lease.error = None
                database.commit()
                return worker.board

    async def oldest_queued_student(self) -> str | None:
        with self.sessions() as database:
            return database.scalar(select(StudentLease.student_id).where(
                StudentLease.state == LeaseStatus.QUEUED.value,
            ).order_by(StudentLease.queued_at, StudentLease.student_id).limit(1))
