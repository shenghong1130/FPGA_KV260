from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db_models import (
    Artifact, ArtifactStatus, LeaseStatus, PredictRequestRecord, RequestStatus,
    StudentLease, Worker, WorkerState, utcnow,
)
from .scheduler import Scheduler
from .worker_client import WorkerClient, WorkerClientError

LOGGER = logging.getLogger(__name__)


class ArtifactNotFoundError(LookupError):
    pass


class RequestNotFoundError(LookupError):
    pass


class LeaseManager:
    """Central-owned student leases and persistent predict requests."""

    def __init__(self, sessions: sessionmaker[Session], scheduler: Scheduler,
                 client: WorkerClient, settings: Settings) -> None:
        self.sessions = sessions
        self.scheduler = scheduler
        self.client = client
        self.settings = settings
        self.student_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.allocator_event = asyncio.Event()
        self.allocator_task: asyncio.Task[None] | None = None
        self.reaper_task: asyncio.Task[None] | None = None

    async def recover_requests(self) -> None:
        with self.sessions() as database:
            for request in database.scalars(select(PredictRequestRecord).where(
                    PredictRequestRecord.status == RequestStatus.RUNNING.value)).all():
                request.status = RequestStatus.FAILED.value
                request.error = "Central restarted while request was running"
                request.completed_at = utcnow()
                lease = database.get(StudentLease, request.student_id)
                if lease:
                    lease.state = LeaseStatus.LOST.value
                    lease.error = request.error
            database.commit()

    def start(self) -> None:
        self.allocator_task = asyncio.create_task(self._allocator_loop(), name="lease-allocator")
        self.reaper_task = asyncio.create_task(self._reaper_loop(), name="lease-reaper")
        self.allocator_event.set()

    async def stop(self) -> None:
        tasks = [task for task in (self.allocator_task, self.reaper_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _latest_artifact(self, database: Session, student_id: str) -> Artifact | None:
        return database.scalar(select(Artifact).where(
            Artifact.student_id == student_id,
            Artifact.status == ArtifactStatus.READY.value,
        ).order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(1))

    async def submit_predict(self, student_id: str,
                             payload: dict[str, Any]) -> PredictRequestRecord:
        async with self.student_locks[student_id]:
            with self.sessions() as database:
                artifact = self._latest_artifact(database, student_id)
                if artifact is None:
                    raise ArtifactNotFoundError(student_id)
                record = PredictRequestRecord(
                    id=f"req_{uuid.uuid4().hex}", student_id=student_id,
                    artifact_id=artifact.id, artifact_version=artifact.version,
                    status=RequestStatus.QUEUED.value, payload=payload,
                    created_at=utcnow(),
                )
                lease = database.get(StudentLease, student_id)
                if lease is None:
                    lease = StudentLease(
                        student_id=student_id, state=LeaseStatus.UNASSIGNED.value,
                        created_at=utcnow(), last_activity_at=utcnow(),
                    )
                    database.add(lease)
                database.add(record)
                database.commit()
                request_id = record.id
            await self._process_student_locked(student_id)
            result = await self.get_request(request_id)
            if result.status == RequestStatus.QUEUED.value:
                self.allocator_event.set()
            return result

    async def get_request(self, request_id: str) -> PredictRequestRecord:
        with self.sessions() as database:
            record = database.get(PredictRequestRecord, request_id)
            if record is None:
                raise RequestNotFoundError(request_id)
            return record

    async def list_requests(
        self, limit: int, student_id: str | None = None
    ) -> list[PredictRequestRecord]:
        with self.sessions() as database:
            query = select(PredictRequestRecord)
            if student_id is not None:
                query = query.where(PredictRequestRecord.student_id == student_id)
            query = query.order_by(
                PredictRequestRecord.created_at.desc(),
                PredictRequestRecord.id.desc(),
            ).limit(limit)
            return list(database.scalars(query).all())

    async def student_status(self, student_id: str) -> dict[str, Any]:
        with self.sessions() as database:
            artifact = self._latest_artifact(database, student_id)
            lease = database.get(StudentLease, student_id)
            counts = dict(database.execute(select(
                PredictRequestRecord.status, func.count()
            ).where(PredictRequestRecord.student_id == student_id).group_by(
                PredictRequestRecord.status)).all())
            return {
                "student_id": student_id,
                "latest_artifact_id": artifact.id if artifact else None,
                "latest_version": artifact.version if artifact else None,
                "lease_state": (lease.state if lease else LeaseStatus.UNASSIGNED.value).lower(),
                "worker_assigned": bool(lease and lease.worker_id),
                "queued_requests": counts.get(RequestStatus.QUEUED.value, 0),
                "running_requests": counts.get(RequestStatus.RUNNING.value, 0),
                "last_activity_at": lease.last_activity_at if lease else None,
            }

    async def list_workers(self) -> list[tuple[Worker, str | None]]:
        with self.sessions() as database:
            owners = {lease.lease_id: lease.student_id for lease in database.scalars(
                select(StudentLease).where(StudentLease.lease_id.is_not(None))).all()}
            return [(worker, owners.get(worker.lease_id)) for worker in database.scalars(
                select(Worker).order_by(Worker.board)).all()]

    @staticmethod
    def _queue(lease: StudentLease) -> None:
        if lease.state != LeaseStatus.QUEUED.value:
            lease.queued_at = utcnow()
        lease.state = LeaseStatus.QUEUED.value
        lease.worker_id = None
        lease.lease_id = None
        lease.current_artifact_id = None

    async def _process_student_locked(self, student_id: str) -> None:
        with self.sessions() as database:
            lease = database.get(StudentLease, student_id)
            if lease is None:
                return
            if lease.state in {LeaseStatus.UNASSIGNED.value, LeaseStatus.ERROR.value,
                               LeaseStatus.LOST.value}:
                self._queue(lease)
                database.commit()
            has_worker = bool(lease.worker_id and lease.lease_id)
        if not has_worker:
            worker_id = await self.scheduler.reserve_lease(student_id)
            if worker_id is None:
                return
            LOGGER.info("worker reserved: student_id=%s board=%s", student_id, worker_id)
        await self._drain_locked(student_id)

    async def _deploy(self, student_id: str, request_id: str) -> bool:
        # Snapshot and publish DEPLOYING in a short transaction.
        with self.sessions() as database:
            lease = database.get(StudentLease, student_id)
            record = database.get(PredictRequestRecord, request_id)
            if (lease is None or record is None or not lease.worker_id or
                    not lease.lease_id):
                return False
            worker = database.get(Worker, lease.worker_id)
            artifact = database.get(Artifact, record.artifact_id)
            if worker is None or artifact is None or worker.lease_id != lease.lease_id:
                lease.state = LeaseStatus.LOST.value
                lease.error = "worker ownership or artifact unavailable"
                database.commit()
                return False
            if (lease.current_artifact_id == artifact.id and
                    worker.current_artifact_id == artifact.id and
                    worker.state in {WorkerState.READY.value, WorkerState.BUSY.value}):
                return True
            lease_id = lease.lease_id
            worker_board = worker.board
            worker_base_url = worker.base_url
            artifact_id = artifact.id
            lease.state = LeaseStatus.DEPLOYING.value
            worker.state = WorkerState.DEPLOYING.value
            database.commit()

        # No SQLAlchemy Session is open during the potentially 120-second
        # Worker deployment request.
        try:
            await self.client.deploy(worker, lease_id, artifact)
        except WorkerClientError as exc:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                worker = database.get(Worker, worker_board)
                if (lease is not None and worker is not None and
                        lease.lease_id == lease_id and worker.lease_id == lease_id and
                        lease.state == LeaseStatus.DEPLOYING.value):
                    self._queue(lease)
                    lease.error = str(exc)
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = str(exc)
                    database.commit()
            self.allocator_event.set()
            return False

        # Reconcile using fresh state so a stale network result cannot revive a
        # Lease that became LOST/OFFLINE while deployment was in flight.
        with self.sessions() as database:
            lease = database.get(StudentLease, student_id)
            worker = database.get(Worker, worker_board)
            if (lease is None or worker is None or lease.lease_id != lease_id or
                    worker.lease_id != lease_id or
                    worker.base_url != worker_base_url or
                    lease.state != LeaseStatus.DEPLOYING.value):
                return False
            lease.current_artifact_id = artifact_id
            lease.state = LeaseStatus.READY.value
            lease.activated_at = lease.activated_at or utcnow()
            lease.last_activity_at = utcnow()
            worker.current_artifact_id = artifact_id
            worker.state = WorkerState.READY.value
            worker.fpga_ready = 1
            worker.last_error = None
            database.commit()
            return True

    async def _drain_locked(self, student_id: str) -> None:
        while True:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                if lease is None or not lease.worker_id or not lease.lease_id:
                    return
                record = database.scalar(select(PredictRequestRecord).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.status == RequestStatus.QUEUED.value,
                ).order_by(PredictRequestRecord.created_at, PredictRequestRecord.id).limit(1))
                if record is None:
                    lease.state = LeaseStatus.READY.value
                    database.commit()
                    return
                worker = database.get(Worker, lease.worker_id)
                artifact = database.get(Artifact, record.artifact_id)
                if worker is None or artifact is None or worker.lease_id != lease.lease_id:
                    lease.state = LeaseStatus.LOST.value
                    lease.error = "worker ownership or artifact unavailable"
                    database.commit()
                    return
                request_id = record.id

            if not await self._deploy(student_id, request_id):
                return

            # Mark RUNNING and take the Worker/payload snapshot, then close the
            # transaction before waiting for FPGA execution.
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                record = database.get(PredictRequestRecord, request_id)
                if (lease is None or record is None or not lease.worker_id or
                        not lease.lease_id or record.status != RequestStatus.QUEUED.value):
                    return
                worker = database.get(Worker, lease.worker_id)
                if (worker is None or worker.lease_id != lease.lease_id or
                        worker.state != WorkerState.READY.value or
                        lease.state != LeaseStatus.READY.value):
                    return
                lease_id = lease.lease_id
                worker_board = worker.board
                worker_base_url = worker.base_url
                request_payload = dict(record.payload)
                record.status = RequestStatus.RUNNING.value
                record.started_at = utcnow()
                lease.state = LeaseStatus.BUSY.value
                lease.last_activity_at = utcnow()
                worker.state = WorkerState.BUSY.value
                database.commit()

            try:
                result = await self.client.predict(worker, lease_id, request_payload)
            except WorkerClientError as exc:
                with self.sessions() as database:
                    lease = database.get(StudentLease, student_id)
                    worker = database.get(Worker, worker_board)
                    record = database.get(PredictRequestRecord, request_id)
                    if (lease is None or worker is None or record is None or
                            lease.lease_id != lease_id or worker.lease_id != lease_id or
                            record.status != RequestStatus.RUNNING.value):
                        return
                    record.status = RequestStatus.FAILED.value
                    record.error = str(exc)
                    record.completed_at = utcnow()
                    self._queue(lease)
                    lease.error = str(exc)
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = str(exc)
                    database.commit()
                self.allocator_event.set()
                return

            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                worker = database.get(Worker, worker_board)
                record = database.get(PredictRequestRecord, request_id)
                if (lease is None or worker is None or record is None or
                        lease.lease_id != lease_id or worker.lease_id != lease_id or
                        worker.base_url != worker_base_url or
                        lease.state != LeaseStatus.BUSY.value or
                        record.status != RequestStatus.RUNNING.value):
                    return
                # Worker ownership and board identity are internal details.
                record.result = {
                    key: value for key, value in result.items()
                    if key not in {"lease_id", "session_id", "board"}
                }
                record.status = RequestStatus.COMPLETED.value
                record.completed_at = utcnow()
                lease.request_count += 1
                lease.state = LeaseStatus.READY.value
                lease.last_activity_at = utcnow()
                worker.state = WorkerState.READY.value
                database.commit()

    async def _allocator_loop(self) -> None:
        try:
            while True:
                await self.allocator_event.wait()
                self.allocator_event.clear()
                while student_id := await self.scheduler.oldest_queued_student():
                    async with self.student_locks[student_id]:
                        before = (await self.student_status(student_id))["lease_state"]
                        await self._process_student_locked(student_id)
                        after = (await self.student_status(student_id))["lease_state"]
                    if before == after == "queued":
                        break
        except asyncio.CancelledError:
            raise

    async def release_student(self, student_id: str, reason: str) -> bool:
        async with self.student_locks[student_id]:
            return await self._release_locked(student_id, reason)

    async def _release_locked(self, student_id: str, reason: str) -> bool:
        # Publish RELEASING while holding the existing lock order, then close
        # the database Session before the Worker HTTP request.
        async with self.scheduler.allocation_lock:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                if (lease is None or lease.state != LeaseStatus.READY.value or
                        not lease.worker_id or not lease.lease_id):
                    return False
                pending = database.scalar(select(func.count()).select_from(
                    PredictRequestRecord).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.status.in_([
                        RequestStatus.QUEUED.value, RequestStatus.RUNNING.value])))
                worker = database.get(Worker, lease.worker_id)
                if pending or worker is None or worker.state != WorkerState.READY.value:
                    return False
                lease_id = lease.lease_id
                worker_board = worker.board
                worker_base_url = worker.base_url
                lease.state = LeaseStatus.RELEASING.value
                database.commit()

        try:
            await self.client.release(worker, lease_id)
        except WorkerClientError as exc:
            async with self.scheduler.allocation_lock:
                with self.sessions() as database:
                    lease = database.get(StudentLease, student_id)
                    worker = database.get(Worker, worker_board)
                    if (lease is None or worker is None or lease.lease_id != lease_id or
                            worker.lease_id != lease_id):
                        return False
                    lease.state = LeaseStatus.ERROR.value
                    lease.error = str(exc)
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = str(exc)
                    database.commit()
            return False

        async with self.scheduler.allocation_lock:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                worker = database.get(Worker, worker_board)
                if (lease is None or worker is None or lease.lease_id != lease_id or
                        worker.lease_id != lease_id or
                        worker.base_url != worker_base_url or
                        lease.state != LeaseStatus.RELEASING.value):
                    return False
                worker.state = WorkerState.IDLE.value
                worker.lease_id = None
                worker.current_artifact_id = None
                worker.fpga_ready = 0
                lease.state = LeaseStatus.UNASSIGNED.value
                lease.worker_id = None
                lease.lease_id = None
                lease.current_artifact_id = None
                lease.released_at = utcnow()
                lease.error = None
                database.commit()
                LOGGER.info("lease released: student_id=%s reason=%s", student_id, reason)
        self.allocator_event.set()
        return True

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.lease_reaper_interval_seconds)
                await self.reap_once()
        except asyncio.CancelledError:
            raise

    async def reap_once(self) -> None:
        now = utcnow()
        with self.sessions() as database:
            lost = list(database.scalars(select(StudentLease).where(
                StudentLease.state.in_([LeaseStatus.LOST.value, LeaseStatus.ERROR.value])
            )).all())
            for lease in lost:
                pending = database.scalar(select(func.count()).select_from(
                    PredictRequestRecord).where(
                    PredictRequestRecord.student_id == lease.student_id,
                    PredictRequestRecord.status == RequestStatus.QUEUED.value))
                if pending:
                    self._queue(lease)
            database.commit()
            queued = bool(database.scalar(select(func.count()).select_from(
                StudentLease).where(StudentLease.state == LeaseStatus.QUEUED.value)))
            ready = list(database.scalars(select(StudentLease).where(
                StudentLease.state == LeaseStatus.READY.value,
                StudentLease.worker_id.is_not(None),
            ).order_by(StudentLease.last_activity_at, StudentLease.student_id)).all())
        if lost:
            self.allocator_event.set()
        for lease in ready:
            timestamp = lease.last_activity_at
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=now.tzinfo)
            idle = (now - timestamp).total_seconds()
            if idle >= self.settings.lease_idle_timeout_seconds:
                if await self.release_student(lease.student_id, "idle timeout"):
                    continue
            if queued and idle >= self.settings.lease_reclaim_grace_seconds:
                if await self.release_student(lease.student_id, "LRU pressure reclaim"):
                    break
