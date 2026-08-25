from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import (
    Artifact,
    ArtifactStatus,
    SessionRecord,
    SessionStatus,
    Worker,
    WorkerState,
    utcnow,
)
from .scheduler import Scheduler
from .worker_client import WorkerClient, WorkerClientError

LOGGER = logging.getLogger(__name__)


class SessionNotFoundError(LookupError):
    pass


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactOwnershipError(PermissionError):
    pass


class SessionConflictError(RuntimeError):
    pass


class WorkerOperationError(RuntimeError):
    pass


class SessionManager:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        scheduler: Scheduler,
        client: WorkerClient,
    ) -> None:
        self.sessions = sessions
        self.scheduler = scheduler
        self.client = client
        self.session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.allocator_event = asyncio.Event()
        self.allocator_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.allocator_task = asyncio.create_task(self._allocator_loop(), name="allocator")
        self.allocator_event.set()

    async def stop(self) -> None:
        if self.allocator_task is None:
            return
        self.allocator_task.cancel()
        await asyncio.gather(self.allocator_task, return_exceptions=True)

    async def create_session(
        self, student_id: str, artifact_id: str
    ) -> SessionRecord:
        with self.sessions() as database:
            artifact = database.get(Artifact, artifact_id)
            if artifact is None:
                raise ArtifactNotFoundError(artifact_id)
            if artifact.student_id != student_id:
                raise ArtifactOwnershipError(artifact_id)
            if artifact.status != ArtifactStatus.READY.value:
                raise SessionConflictError("artifact is not ready")
            session = SessionRecord(
                id=f"sess_{uuid.uuid4().hex}",
                student_id=student_id,
                artifact_id=artifact_id,
                status=SessionStatus.QUEUED.value,
                created_at=utcnow(),
                last_activity_at=utcnow(),
            )
            database.add(session)
            database.commit()
            session_id = session.id
        LOGGER.info(
            "session created: session_id=%s artifact_id=%s", session_id, artifact_id
        )
        worker_id = await self.scheduler.reserve(session_id)
        if worker_id is None:
            LOGGER.info(
                "session queued: session_id=%s artifact_id=%s", session_id, artifact_id
            )
            return await self.get_session(session_id)
        LOGGER.info(
            "worker reserved: session_id=%s artifact_id=%s board=%s",
            session_id,
            artifact_id,
            worker_id,
        )
        await self._deploy(session_id)
        return await self.get_session(session_id)

    async def _deploy(self, session_id: str) -> None:
        with self.sessions() as database:
            session = database.get(SessionRecord, session_id)
            if session is None or session.worker_id is None:
                raise SessionNotFoundError(session_id)
            worker = database.get(Worker, session.worker_id)
            artifact = database.get(Artifact, session.artifact_id)
            if worker is None or artifact is None:
                raise WorkerOperationError("reserved worker or artifact disappeared")
            session.status = SessionStatus.DEPLOYING.value
            worker.state = WorkerState.DEPLOYING.value
            database.commit()
            board = worker.board
            artifact_id = artifact.id
            LOGGER.info(
                "artifact deployment start: session_id=%s artifact_id=%s board=%s",
                session_id,
                artifact_id,
                board,
            )
            try:
                await self.client.deploy(worker, session_id, artifact)
            except WorkerClientError as exc:
                session.status = SessionStatus.FAILED.value
                session.error = str(exc)
                worker.state = WorkerState.ERROR.value
                worker.last_error = str(exc)
                database.commit()
                LOGGER.exception(
                    "artifact deployment failed: session_id=%s artifact_id=%s board=%s",
                    session_id,
                    artifact_id,
                    board,
                )
                raise WorkerOperationError(str(exc)) from exc
            session.status = SessionStatus.READY.value
            session.activated_at = utcnow()
            session.last_activity_at = utcnow()
            worker.state = WorkerState.READY.value
            worker.fpga_ready = 1
            worker.last_error = None
            database.commit()
            LOGGER.info(
                "artifact deployment success: session_id=%s artifact_id=%s board=%s",
                session_id,
                artifact_id,
                board,
            )

    async def _allocator_loop(self) -> None:
        try:
            while True:
                await self.allocator_event.wait()
                self.allocator_event.clear()
                while queued_id := await self.scheduler.oldest_queued():
                    worker_id = await self.scheduler.reserve(queued_id)
                    if worker_id is None:
                        break
                    try:
                        await self._deploy(queued_id)
                    except WorkerOperationError:
                        continue
        except asyncio.CancelledError:
            raise

    async def get_session(self, session_id: str) -> SessionRecord:
        with self.sessions() as database:
            session = database.get(SessionRecord, session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            return session

    async def predict(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        lock = self.session_locks[session_id]
        async with lock:
            with self.sessions() as database:
                session = database.get(SessionRecord, session_id)
                if session is None:
                    raise SessionNotFoundError(session_id)
                if session.status != SessionStatus.READY.value or session.worker_id is None:
                    raise SessionConflictError(
                        f"session must be READY, current status={session.status}"
                    )
                worker = database.get(Worker, session.worker_id)
                if worker is None or worker.session_id != session.id:
                    raise SessionConflictError("worker ownership is inconsistent")
                if worker.state != WorkerState.READY.value:
                    raise SessionConflictError(
                        f"worker must be READY, current state={worker.state}"
                    )
                session.status = SessionStatus.BUSY.value
                worker.state = WorkerState.BUSY.value
                session.last_activity_at = utcnow()
                database.commit()
                board = worker.board
                artifact_id = session.artifact_id
                LOGGER.info(
                    "predict start: session_id=%s artifact_id=%s board=%s",
                    session_id,
                    artifact_id,
                    board,
                )
                try:
                    result = await self.client.predict(worker, session_id, payload)
                except WorkerClientError as exc:
                    session.status = SessionStatus.LOST.value
                    session.error = str(exc)
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = str(exc)
                    database.commit()
                    LOGGER.exception(
                        "predict failed: session_id=%s artifact_id=%s board=%s",
                        session_id,
                        artifact_id,
                        board,
                    )
                    raise WorkerOperationError(str(exc)) from exc
                session.request_count += 1
                session.status = SessionStatus.READY.value
                session.last_activity_at = utcnow()
                worker.state = WorkerState.READY.value
                database.commit()
                LOGGER.info(
                    "predict complete: session_id=%s artifact_id=%s board=%s",
                    session_id,
                    artifact_id,
                    board,
                )
                return result

    async def release(self, session_id: str) -> SessionRecord:
        lock = self.session_locks[session_id]
        async with lock:
            async with self.scheduler.allocation_lock:
                with self.sessions() as database:
                    session = database.get(SessionRecord, session_id)
                    if session is None:
                        raise SessionNotFoundError(session_id)
                    if session.status == SessionStatus.CLOSED.value:
                        return session
                    if session.status == SessionStatus.QUEUED.value:
                        session.status = SessionStatus.CLOSED.value
                        session.released_at = utcnow()
                        database.commit()
                        LOGGER.info(
                            "queued session released: session_id=%s artifact_id=%s",
                            session.id,
                            session.artifact_id,
                        )
                        return session
                    worker = (
                        database.get(Worker, session.worker_id)
                        if session.worker_id
                        else None
                    )
                    session.status = SessionStatus.RELEASING.value
                    if worker and worker.session_id == session.id:
                        worker.state = WorkerState.READY.value
                    database.commit()
                    release_error: str | None = None
                    if worker and worker.session_id == session.id:
                        try:
                            await self.client.release(worker, session.id)
                        except WorkerClientError as exc:
                            release_error = str(exc)
                            worker.state = WorkerState.ERROR.value
                            worker.last_error = release_error
                        else:
                            worker.state = WorkerState.IDLE.value
                            worker.session_id = None
                            worker.fpga_ready = 0
                    session.status = SessionStatus.CLOSED.value
                    session.released_at = utcnow()
                    session.last_activity_at = utcnow()
                    if release_error:
                        session.error = f"release notification failed: {release_error}"
                    database.commit()
                    LOGGER.info(
                        "session release: session_id=%s artifact_id=%s board=%s",
                        session.id,
                        session.artifact_id,
                        session.worker_id,
                    )
            self.allocator_event.set()
            return session

    async def list_workers(self) -> list[Worker]:
        with self.sessions() as database:
            return list(
                database.scalars(select(Worker).order_by(Worker.board)).all()
            )
