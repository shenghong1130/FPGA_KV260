from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import timedelta, timezone
import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .audit import AuditLogger
from .hardware_validation import HardwareValidationError, validate_paths
from .config import Settings
from .db_models import (
    Artifact, ArtifactStatus, AuditEvent, LeaseStatus, PredictRequestRecord, RequestStatus,
    StudentCredential, StudentLease, Worker, WorkerState, utcnow,
)
from .scheduler import Scheduler
from .worker_client import WorkerClient, WorkerClientError

LOGGER = logging.getLogger(__name__)


class ArtifactNotFoundError(LookupError):
    pass


class RequestNotFoundError(LookupError):
    pass


class WorkerNotFoundError(LookupError):
    pass


class WorkerNotSafelyReleasableError(RuntimeError):
    pass


class WorkerReleaseError(RuntimeError):
    pass


class LeaseManager:
    """Central-owned student leases and persistent predict requests."""

    def __init__(self, sessions: sessionmaker[Session], scheduler: Scheduler,
                 client: WorkerClient, settings: Settings,
                 audit: AuditLogger) -> None:
        self.sessions = sessions
        self.scheduler = scheduler
        self.client = client
        self.settings = settings
        self.audit = audit
        self.student_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.allocator_event = asyncio.Event()
        self.allocator_task: asyncio.Task[None] | None = None
        self.reaper_task: asyncio.Task[None] | None = None

    async def recover_requests(self) -> None:
        failed: list[tuple[str, str, str, str | None]] = []
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
                failed.append(
                    (request.student_id, request.id, request.artifact_id, request.error)
                )
            for lease in database.scalars(select(StudentLease).where(
                    StudentLease.state.in_([LeaseStatus.RESERVED.value,
                                           LeaseStatus.DEPLOYING.value,
                                           LeaseStatus.BUSY.value,
                                           LeaseStatus.RELEASING.value]))).all():
                lease.state = LeaseStatus.LOST.value
                lease.error = "Central restarted during an unfinished operation"
                if lease.worker_id:
                    worker = database.get(Worker, lease.worker_id)
                    if worker and worker.lease_id == lease.lease_id:
                        worker.state = WorkerState.ERROR.value
            database.commit()
        for student_id, request_id, artifact_id, error in failed:
            self.audit.record(
                "REQUEST_FAILED",
                level="ERROR",
                student_id=student_id,
                artifact_id=artifact_id,
                request_id=request_id,
                message="Predict Request failed during Central recovery",
                details={"error": error},
            )

    def start(self) -> None:
        if self.settings.maintenance_mode:
            return
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
        if self.settings.maintenance_mode:
            raise RuntimeError("KV260 maintenance: scheduling paused")
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
                artifact_id = record.artifact_id
            self.audit.record(
                "REQUEST_CREATED",
                student_id=student_id,
                artifact_id=artifact_id,
                request_id=request_id,
                message="Predict Request created",
            )
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
        self,
        limit: int | None,
        student_id: str | None = None,
        worker_id: str | None = None,
    ) -> list[PredictRequestRecord]:
        with self.sessions() as database:
            query = select(PredictRequestRecord)
            if student_id is not None:
                query = query.where(PredictRequestRecord.student_id == student_id)
            if worker_id is not None:
                query = query.where(PredictRequestRecord.worker_id == worker_id)
            query = query.order_by(
                PredictRequestRecord.created_at.desc(),
                PredictRequestRecord.id.desc(),
            )
            if limit is not None:
                query = query.limit(limit)
            return list(database.scalars(query).all())

    async def student_status(self, student_id: str) -> dict[str, Any]:
        with self.sessions() as database:
            summary = self._student_summary(database, student_id)
            worker_id = summary.pop("worker_id")
            summary["worker_assigned"] = bool(worker_id)
            return summary

    async def list_students(self) -> list[dict[str, Any]]:
        with self.sessions() as database:
            student_ids: set[str] = set()
            for model in (
                Artifact,
                StudentCredential,
                StudentLease,
                PredictRequestRecord,
            ):
                student_ids.update(database.scalars(
                    select(model.student_id).distinct()
                ).all())
            return [
                self._student_summary(database, student_id)
                for student_id in sorted(student_ids)
            ]

    def _student_summary(
        self, database: Session, student_id: str
    ) -> dict[str, Any]:
        artifact = self._latest_artifact(database, student_id)
        lease = database.get(StudentLease, student_id)
        counts = dict(database.execute(select(
            PredictRequestRecord.status, func.count()
        ).where(PredictRequestRecord.student_id == student_id).group_by(
            PredictRequestRecord.status)).all())
        latest_request_at = database.scalar(select(
            PredictRequestRecord.created_at
        ).where(PredictRequestRecord.student_id == student_id).order_by(
            PredictRequestRecord.created_at.desc(),
            PredictRequestRecord.id.desc(),
        ).limit(1))
        last_activity_at = (
            lease.last_activity_at if lease else latest_request_at
        )
        if last_activity_at is None and artifact is not None:
            last_activity_at = artifact.created_at
        queued = counts.get(RequestStatus.QUEUED.value, 0)
        running = counts.get(RequestStatus.RUNNING.value, 0)
        completed = counts.get(RequestStatus.COMPLETED.value, 0)
        failed = counts.get(RequestStatus.FAILED.value, 0)
        cancelled = counts.get(RequestStatus.CANCELLED.value, 0)
        return {
            "student_id": student_id,
            "latest_artifact_id": artifact.id if artifact else None,
            "latest_version": artifact.version if artifact else None,
            "lease_state": (
                lease.state if lease else LeaseStatus.UNASSIGNED.value
            ).lower(),
            "worker_id": lease.worker_id if lease else None,
            "queued_requests": queued,
            "running_requests": running,
            "completed_requests": completed,
            "failed_requests": failed,
            "cancelled_requests": cancelled,
            "total_requests": queued + running + completed + failed + cancelled,
            "last_activity_at": last_activity_at,
        }

    async def list_workers(self) -> list[tuple[Worker, str | None]]:
        with self.sessions() as database:
            owners = {lease.lease_id: lease.student_id for lease in database.scalars(
                select(StudentLease).where(StudentLease.lease_id.is_not(None))).all()}
            return [(worker, owners.get(worker.lease_id)) for worker in database.scalars(
                select(Worker).order_by(Worker.board)).all()]

    @staticmethod
    def _queue(lease: StudentLease) -> None:
        if lease.lease_id or lease.worker_id:
            raise RuntimeError("cannot queue a lease before confirmed remote release")
        if lease.state != LeaseStatus.QUEUED.value:
            lease.queued_at = utcnow()
        lease.state = LeaseStatus.QUEUED.value
        lease.worker_id = None
        lease.lease_id = None
        lease.current_artifact_id = None

    async def _reject_invalid_pending(self, student_id: str) -> None:
        """Reject old incompatible READY artifacts before any board reservation."""
        while True:
            with self.sessions() as database:
                record = database.scalar(select(PredictRequestRecord).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.status == RequestStatus.QUEUED.value,
                ).order_by(PredictRequestRecord.created_at, PredictRequestRecord.id).limit(1))
                if record is None:
                    return
                artifact = database.get(Artifact, record.artifact_id)
                artifact_id = record.artifact_id
            try:
                if artifact is None or artifact.status != ArtifactStatus.READY.value:
                    raise HardwareValidationError("Artifact is not READY; upload a compatible design")
                await asyncio.to_thread(validate_paths, Path(artifact.bit_path), Path(artifact.hwh_path),
                                        artifact.bit_sha256, artifact.hwh_sha256)
                return
            except (HardwareValidationError, OSError) as exc:
                error = str(exc)
            with self.sessions() as database:
                artifact = database.get(Artifact, artifact_id)
                if artifact and artifact.status == ArtifactStatus.READY.value:
                    artifact.status = ArtifactStatus.FAILED.value
                rejected = list(database.scalars(select(PredictRequestRecord).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.artifact_id == artifact_id,
                    PredictRequestRecord.status == RequestStatus.QUEUED.value)).all())
                ids = [pending.id for pending in rejected]
                for pending in rejected:
                    pending.status = RequestStatus.FAILED.value
                    pending.error = error
                    pending.completed_at = utcnow()
                database.commit()
            self.audit.record("ARTIFACT_REJECTED", level="ERROR", student_id=student_id,
                              artifact_id=artifact_id, message=error,
                              details={"stage": "before_worker_reservation"})
            for request_id in ids:
                self.audit.record("REQUEST_FAILED", level="ERROR", student_id=student_id,
                                  artifact_id=artifact_id, request_id=request_id,
                                  message="Artifact rejected before Worker reservation", details={"error": error})

    async def authorize_release_retry(self, student_id: str, expected_lease_id: str, reason: str) -> bool:
        """Explicit operator action only; never clears ownership or asserts hardware safety."""
        if not reason.strip():
            raise ValueError("operator reason required")
        async with self.student_locks[student_id]:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                if (lease is None or lease.lease_id != expected_lease_id or
                        lease.state not in {LeaseStatus.ERROR.value, LeaseStatus.LOST.value}):
                    return False
                lease.release_attempts = 0
                lease.release_retry_at = None
                lease.release_blocked = 0
                database.add(AuditEvent(id=f"evt_{uuid.uuid4().hex}", event_type="RELEASE_RETRY_AUTHORIZED",
                    level="WARNING", actor_type="administrator", student_id=student_id,
                    message=reason, details={"lease_id": expected_lease_id}))
                database.commit()
            return True

    async def _process_student_locked(self, student_id: str) -> None:
        if self.settings.maintenance_mode:
            return
        await self._reject_invalid_pending(student_id)
        with self.sessions() as database:
            lease = database.get(StudentLease, student_id)
            if lease is None:
                return
            needs_cleanup = lease.state in {LeaseStatus.ERROR.value, LeaseStatus.LOST.value}
        if needs_cleanup:
            if not await self._release_locked(student_id, "failure recovery", recovery=True):
                return
        with self.sessions() as database:
            lease = database.get(StudentLease, student_id)
            if not lease.worker_id and not lease.lease_id:
                for exhausted in database.scalars(select(PredictRequestRecord).where(
                        PredictRequestRecord.student_id == student_id,
                        PredictRequestRecord.status == RequestStatus.QUEUED.value,
                        PredictRequestRecord.deploy_attempts >= 3)).all():
                    exhausted.status = RequestStatus.FAILED.value
                    exhausted.error = "deployment retry budget exhausted (3 attempts)"
                    exhausted.completed_at = utcnow()
                has_pending = database.scalar(select(PredictRequestRecord.id).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.status == RequestStatus.QUEUED.value).limit(1))
                if not has_pending:
                    lease.state = LeaseStatus.UNASSIGNED.value
                    database.commit()
                    return
            if lease.state == LeaseStatus.UNASSIGNED.value:
                self._queue(lease)
                database.commit()
            has_worker = bool(lease.worker_id and lease.lease_id)
        if not has_worker:
            worker_id = await self.scheduler.reserve_lease(student_id)
            if worker_id is None:
                return
            LOGGER.info("worker reserved: student_id=%s board=%s", student_id, worker_id)
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                pending = database.scalar(select(PredictRequestRecord).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.status == RequestStatus.QUEUED.value,
                ).order_by(
                    PredictRequestRecord.created_at, PredictRequestRecord.id
                ).limit(1))
                lease_id = lease.lease_id if lease else None
                artifact_id = pending.artifact_id if pending else None
            self.audit.record(
                "WORKER_ASSIGNED",
                student_id=student_id,
                board=worker_id,
                artifact_id=artifact_id,
                message="Worker assigned to student",
                details={"lease_id": lease_id},
            )
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
            if artifact.status != ArtifactStatus.READY.value:
                record.status = RequestStatus.FAILED.value
                record.error = "Artifact is not READY; upload a compatible design"
                record.completed_at = utcnow()
                lease.state = LeaseStatus.ERROR.value
                lease.error = record.error
                worker.state = WorkerState.ERROR.value
                worker.last_error = record.error
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
            worker.fpga_ready = 0
            database.commit()

        # No SQLAlchemy Session is open during the potentially 120-second
        # Worker deployment request.
        try:
            # Revalidate old READY artifacts too, before any Worker mutation.
            try:
                await asyncio.to_thread(
                    validate_paths, Path(artifact.bit_path), Path(artifact.hwh_path),
                    artifact.bit_sha256, artifact.hwh_sha256,
                )
            except (HardwareValidationError, OSError) as exc:
                raise WorkerClientError(str(exc), permanent=True) from exc
            for attempt in range(3):
                # Persist before I/O: timeouts, stale results and Central
                # restarts cannot reset this request's total retry budget.
                with self.sessions() as database:
                    pending = database.get(PredictRequestRecord, request_id)
                    current = database.get(StudentLease, student_id)
                    target = database.get(Worker, worker_board)
                    if (pending is None or pending.status != RequestStatus.QUEUED.value or
                            current is None or current.lease_id != lease_id or
                            current.state != LeaseStatus.DEPLOYING.value or
                            target is None or target.lease_id != lease_id or
                            target.base_url != worker_base_url or
                            target.state != WorkerState.DEPLOYING.value):
                        return False
                    if pending.deploy_attempts >= 3:
                        raise WorkerClientError("deployment retry budget exhausted (3 attempts)")
                    pending.deploy_attempts += 1
                    database.commit()
                try:
                    await self.client.deploy(worker, lease_id, artifact)
                    break
                except WorkerClientError as exc:
                    if not exc.retryable or exc.permanent or attempt == 2:
                        raise
                    await asyncio.sleep(0.1 * (2 ** attempt))
                    with self.sessions() as database:
                        current = database.get(StudentLease, student_id)
                        current_worker = database.get(Worker, worker_board)
                        if (current is None or current.lease_id != lease_id or
                                current.state != LeaseStatus.DEPLOYING.value or
                                current_worker is None or current_worker.lease_id != lease_id or
                                current_worker.base_url != worker_base_url or
                                current_worker.state != WorkerState.DEPLOYING.value):
                            return False
        except WorkerClientError as exc:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                worker = database.get(Worker, worker_board)
                if (lease is not None and worker is not None and
                        lease.lease_id == lease_id and worker.lease_id == lease_id and
                        lease.state == LeaseStatus.DEPLOYING.value):
                    lease.state = LeaseStatus.ERROR.value
                    lease.error = str(exc)
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = str(exc)
                    worker.fpga_ready = 0
                    if exc.permanent:
                        database.get(Artifact, artifact_id).status = ArtifactStatus.FAILED.value
                    # Terminalize these requests; automatic cleanup must never
                    # requeue the same failing deployment across the cluster.
                    for pending in database.scalars(select(PredictRequestRecord).where(
                            PredictRequestRecord.student_id == student_id,
                            PredictRequestRecord.artifact_id == artifact_id,
                            PredictRequestRecord.status == RequestStatus.QUEUED.value)).all():
                        pending.status = RequestStatus.FAILED.value
                        pending.error = str(exc)
                        pending.worker_id = worker_board
                        pending.completed_at = utcnow()
                    database.commit()
            self.allocator_event.set()
            self.audit.record(
                "FPGA_DEPLOY_FAILED",
                level="ERROR",
                student_id=student_id,
                board=worker_board,
                artifact_id=artifact_id,
                request_id=request_id,
                message="FPGA deploy failed",
                details={"error": str(exc)},
            )
            return False

        # Reconcile using fresh state so a stale network result cannot revive a
        # Lease that became LOST/OFFLINE while deployment was in flight.
        with self.sessions() as database:
            lease = database.get(StudentLease, student_id)
            worker = database.get(Worker, worker_board)
            if (lease is None or worker is None or lease.lease_id != lease_id or
                    worker.lease_id != lease_id or
                    worker.base_url != worker_base_url or
                    lease.state != LeaseStatus.DEPLOYING.value or
                    worker.state != WorkerState.DEPLOYING.value):
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
        self.audit.record(
            "FPGA_DEPLOYED",
            student_id=student_id,
            board=worker_board,
            artifact_id=artifact_id,
            request_id=request_id,
            message="FPGA overlay deployed",
        )
        return True

    async def _drain_locked(self, student_id: str) -> None:
        while True:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                if (lease is None or not lease.worker_id or not lease.lease_id or
                        lease.state not in {LeaseStatus.RESERVED.value, LeaseStatus.READY.value}):
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
                record.worker_id = worker_board
                record.status = RequestStatus.RUNNING.value
                record.started_at = utcnow()
                started_at = record.started_at
                artifact_id = record.artifact_id
                lease.state = LeaseStatus.BUSY.value
                lease.last_activity_at = utcnow()
                worker.state = WorkerState.BUSY.value
                database.commit()
            self.audit.record(
                "REQUEST_STARTED",
                student_id=student_id,
                board=worker_board,
                artifact_id=artifact_id,
                request_id=request_id,
                message="Predict Request started",
            )

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
                    bad_payload = exc.status_code == 422
                    lease.state = LeaseStatus.READY.value if bad_payload else LeaseStatus.ERROR.value
                    lease.error = None if bad_payload else str(exc)
                    worker.state = WorkerState.READY.value if bad_payload else WorkerState.ERROR.value
                    worker.last_error = None if bad_payload else str(exc)
                    if not bad_payload:
                        worker.fpga_ready = 0
                        if exc.permanent:
                            # A proven FPGA execution failure must not circulate
                            # to other boards via already queued requests.
                            database.get(Artifact, artifact_id).status = ArtifactStatus.FAILED.value
                            for pending in database.scalars(select(PredictRequestRecord).where(
                                    PredictRequestRecord.artifact_id == artifact_id,
                                    PredictRequestRecord.status == RequestStatus.QUEUED.value)).all():
                                pending.status = RequestStatus.FAILED.value
                                pending.error = str(exc)
                                pending.completed_at = utcnow()
                            database.add(AuditEvent(id=f"evt_{uuid.uuid4().hex}",
                                event_type="ARTIFACT_QUARANTINED", level="ERROR",
                                actor_type="system", student_id=student_id, board=worker_board,
                                artifact_id=artifact_id, request_id=request_id,
                                message="FPGA execution failed; Artifact quarantined before cross-board retry",
                                details={"error": str(exc), "lease_id": lease_id}))
                    database.commit()
                    completed_at = record.completed_at
                duration = (
                    (completed_at - started_at).total_seconds()
                    if completed_at and started_at else None
                )
                self.audit.record(
                    "REQUEST_FAILED",
                    level="ERROR",
                    student_id=student_id,
                    board=worker_board,
                    artifact_id=artifact_id,
                    request_id=request_id,
                    message="Predict Request failed",
                    details={"duration": duration, "error": str(exc)},
                )
                self.allocator_event.set()
                if bad_payload:
                    continue
                return

            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                worker = database.get(Worker, worker_board)
                record = database.get(PredictRequestRecord, request_id)
                if (lease is None or worker is None or record is None or
                        lease.lease_id != lease_id or worker.lease_id != lease_id or
                        worker.base_url != worker_base_url or
                        lease.state != LeaseStatus.BUSY.value or
                        worker.state != WorkerState.BUSY.value or
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
                completed_at = record.completed_at
                compact_result = {
                    key: record.result[key]
                    for key in ("status", "predicted_class")
                    if key in record.result
                }
            compact_result["duration"] = (
                (completed_at - started_at).total_seconds()
                if completed_at and started_at else None
            )
            self.audit.record(
                "REQUEST_COMPLETED",
                student_id=student_id,
                board=worker_board,
                artifact_id=artifact_id,
                request_id=request_id,
                message="Predict Request completed",
                details=compact_result,
            )

    async def _allocator_loop(self) -> None:
        try:
            while True:
                await self.allocator_event.wait()
                self.allocator_event.clear()
                while student_id := await self.scheduler.oldest_queued_student():
                    try:
                        async with self.student_locks[student_id]:
                            before = (await self.student_status(student_id))["lease_state"]
                            await self._process_student_locked(student_id)
                            after = (await self.student_status(student_id))["lease_state"]
                    except Exception:
                        LOGGER.exception("allocator failed for student=%s", student_id)
                        break
                    if before == after == "queued":
                        break
        except asyncio.CancelledError:
            raise

    async def release_worker(self, board: str, reason: str) -> str:
        with self.sessions() as database:
            worker = database.get(Worker, board)
            if worker is None:
                raise WorkerNotFoundError(board)
            if worker.state != WorkerState.READY.value or not worker.lease_id:
                raise WorkerNotSafelyReleasableError(board)
            lease = database.scalar(select(StudentLease).where(
                StudentLease.worker_id == board,
                StudentLease.lease_id == worker.lease_id,
            ))
            if lease is None or lease.state != LeaseStatus.READY.value:
                raise WorkerNotSafelyReleasableError(board)
            student_id = lease.student_id
            lease_id = lease.lease_id

        released = await self.release_student(
            student_id,
            reason,
            expected_worker_id=board,
            expected_lease_id=lease_id,
            raise_on_worker_error=True,
        )
        if not released:
            raise WorkerNotSafelyReleasableError(board)
        return student_id

    async def release_student(
        self,
        student_id: str,
        reason: str,
        *,
        expected_worker_id: str | None = None,
        expected_lease_id: str | None = None,
        raise_on_worker_error: bool = False,
    ) -> bool:
        async with self.student_locks[student_id]:
            return await self._release_locked(
                student_id,
                reason,
                expected_worker_id=expected_worker_id,
                expected_lease_id=expected_lease_id,
                raise_on_worker_error=raise_on_worker_error,
            )

    async def _release_locked(
        self,
        student_id: str,
        reason: str,
        *,
        expected_worker_id: str | None = None,
        expected_lease_id: str | None = None,
        raise_on_worker_error: bool = False,
        recovery: bool = False,
    ) -> bool:
        # Publish RELEASING while holding the existing lock order, then close
        # the database Session before the Worker HTTP request.
        async with self.scheduler.allocation_lock:
            with self.sessions() as database:
                lease = database.get(StudentLease, student_id)
                if (lease is None or lease.state not in (
                        {LeaseStatus.ERROR.value, LeaseStatus.LOST.value, LeaseStatus.RELEASING.value}
                        if recovery else {LeaseStatus.READY.value}) or
                        not lease.worker_id or not lease.lease_id or
                        (expected_worker_id is not None and
                         lease.worker_id != expected_worker_id) or
                        (expected_lease_id is not None and
                         lease.lease_id != expected_lease_id)):
                    return False
                pending = database.scalar(select(func.count()).select_from(
                    PredictRequestRecord).where(
                    PredictRequestRecord.student_id == student_id,
                    PredictRequestRecord.status.in_([
                        RequestStatus.QUEUED.value, RequestStatus.RUNNING.value])))
                worker = database.get(Worker, lease.worker_id)
                if ((pending and not recovery) or worker is None or
                        (not recovery and worker.state != WorkerState.READY.value) or
                        worker.lease_id != lease.lease_id):
                    return False
                if lease.release_blocked or lease.release_attempts >= 3:
                    return False
                now = utcnow()
                retry_at = lease.release_retry_at
                if retry_at is not None:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    if now < retry_at:
                        return False
                lease.release_attempts += 1
                lease_id = lease.lease_id
                worker_board = worker.board
                worker_base_url = worker.base_url
                lease.state = LeaseStatus.RELEASING.value
                worker.state = WorkerState.ERROR.value if recovery else WorkerState.READY.value
                database.commit()

        try:
            acknowledgement = await self.client.release(worker, lease_id)
            if acknowledgement.get("ok") is not True or acknowledgement.get("lease_id") != lease_id:
                raise WorkerClientError("release acknowledgement identity mismatch")
        except WorkerClientError as exc:
            async with self.scheduler.allocation_lock:
                with self.sessions() as database:
                    lease = database.get(StudentLease, student_id)
                    worker = database.get(Worker, worker_board)
                    if (lease is None or worker is None or lease.lease_id != lease_id or
                            worker.lease_id != lease_id or worker.base_url != worker_base_url or
                            lease.state != LeaseStatus.RELEASING.value):
                        return False
                    lease.state = LeaseStatus.ERROR.value
                    lease.error = str(exc)
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = str(exc)
                    lease.release_retry_at = utcnow() + timedelta(seconds=2 ** (lease.release_attempts - 1))
                    lease.release_blocked = int(exc.status_code in {409, 422} or lease.release_attempts >= 3)
                    blocked = bool(lease.release_blocked)
                    attempts = lease.release_attempts
                    if blocked:
                        lease.error = f"{exc}; automatic release stopped after {attempts} attempt(s); operator recovery required"
                        worker.last_error = lease.error
                    database.commit()
            if blocked:
                self.audit.record("RELEASE_RETRY_EXHAUSTED", level="ERROR", student_id=student_id,
                                  board=worker_board, message="Worker remains isolated; automatic release stopped",
                                  details={"lease_id": lease_id, "attempts": attempts, "error": str(exc)})
            if raise_on_worker_error:
                raise WorkerReleaseError(str(exc)) from exc
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
                lease.release_attempts = 0
                lease.release_retry_at = None
                lease.release_blocked = 0
                if recovery and pending:
                    self._queue(lease)
                database.commit()
                LOGGER.info("lease released: student_id=%s reason=%s", student_id, reason)
        self.audit.record(
            "WORKER_RELEASED",
            student_id=student_id,
            board=worker_board,
            message="Worker lease released",
            details={"reason": reason},
        )
        self.allocator_event.set()
        return True

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.lease_reaper_interval_seconds)
                try:
                    await self.reap_once()
                except Exception:
                    LOGGER.exception("lease reaper iteration failed")
        except asyncio.CancelledError:
            raise

    async def reap_once(self) -> None:
        now = utcnow()
        with self.sessions() as database:
            lost = list(database.scalars(select(StudentLease).where(
                StudentLease.state.in_([LeaseStatus.LOST.value, LeaseStatus.ERROR.value,
                                       LeaseStatus.RELEASING.value])
            )).all())
        for lease in lost:
            async with self.student_locks[lease.student_id]:
                await self._release_locked(lease.student_id, "failure recovery", recovery=True)
        with self.sessions() as database:
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
