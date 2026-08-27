from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import (
    LeaseStatus,
    PredictRequestRecord,
    RequestStatus,
    StudentLease,
    Worker,
    WorkerState,
    utcnow,
)
from .worker_client import WorkerClient, WorkerClientError

LOGGER = logging.getLogger(__name__)
ACTIVE_LEASE_STATES = {
    LeaseStatus.RESERVED.value,
    LeaseStatus.DEPLOYING.value,
    LeaseStatus.READY.value,
    LeaseStatus.BUSY.value,
    LeaseStatus.RELEASING.value,
}


class WorkerRegistry:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        client: WorkerClient,
        config_path: Path,
        health_interval: float,
        failure_threshold: int,
    ) -> None:
        self.sessions = sessions
        self.client = client
        self.config_path = config_path
        self.health_interval = health_interval
        self.failure_threshold = failure_threshold
        self.failures: dict[str, int] = defaultdict(int)
        self.monitor_task: asyncio.Task[None] | None = None

    def _load_config(self) -> list[dict[str, str]]:
        raw: Any = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError("workers config must be a non-empty JSON array")
        entries: list[dict[str, str]] = []
        seen_boards: set[str] = set()
        seen_urls: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each worker config entry must be an object")
            board = str(item.get("board", "")).strip()
            base_url = str(item.get("base_url", "")).strip().rstrip("/")
            if not board or not base_url.startswith(("http://", "https://")):
                raise ValueError(f"invalid worker config entry: {item}")
            if board in seen_boards or base_url in seen_urls:
                raise ValueError(f"duplicate worker config entry: {item}")
            seen_boards.add(board)
            seen_urls.add(base_url)
            entries.append({"board": board, "base_url": base_url})
        return entries

    async def sync_config(self) -> None:
        entries = self._load_config()
        configured = {entry["board"] for entry in entries}
        with self.sessions() as database:
            existing = {
                worker.board: worker
                for worker in database.scalars(select(Worker)).all()
            }
            for entry in entries:
                worker = existing.get(entry["board"])
                if worker is None:
                    database.add(
                        Worker(
                            board=entry["board"],
                            base_url=entry["base_url"],
                            state=WorkerState.OFFLINE.value,
                        )
                    )
                else:
                    worker.base_url = entry["base_url"]
            for board, worker in existing.items():
                if board not in configured and worker.lease_id is None:
                    database.delete(worker)
            database.commit()

    async def recover(self) -> None:
        with self.sessions() as database:
            boards = [worker.board for worker in database.scalars(select(Worker)).all()]
        await asyncio.gather(
            *(self._check_worker(board, recovery=True) for board in boards)
        )

    async def _check_worker(self, board: str, recovery: bool = False) -> None:
        # Phase A: take only the endpoint snapshot needed by the HTTP client.
        # The detached object prevents a SQLAlchemy Session/transaction from
        # remaining open across either network await below.
        with self.sessions() as database:
            worker = database.get(Worker, board)
            if worker is None:
                return
            endpoint = Worker(board=worker.board, base_url=worker.base_url)

        # Phase B: no database Session is open while waiting for Worker I/O.
        try:
            health = await self.client.health(endpoint)
            if not health.get("ok"):
                raise WorkerClientError(f"unhealthy response: {health}")
            status = await self.client.status(endpoint)
        except WorkerClientError as exc:
            self.failures[board] += 1
            if not recovery and self.failures[board] < self.failure_threshold:
                return
            # Phase C (failure): reopen and reconcile against current state.
            with self.sessions() as database:
                worker = database.get(Worker, board)
                if worker is None or worker.base_url != endpoint.base_url:
                    return
                self._mark_offline(database, worker, str(exc))
                database.commit()
            return

        self.failures[board] = 0
        # Phase C (success): fetch fresh persistent state. A Scheduler may have
        # changed IDLE to RESERVED/DEPLOYING/BUSY while HTTP was in flight.
        with self.sessions() as database:
            worker = database.get(Worker, board)
            if worker is None or worker.base_url != endpoint.base_url:
                return
            worker.last_seen = utcnow()
            worker.last_error = None
            remote_lease = status.get("lease_id")
            remote_artifact = status.get("artifact_id")
            active = None
            if worker.lease_id:
                active = database.scalar(select(StudentLease).where(
                    StudentLease.lease_id == worker.lease_id))
            if active and active.state in ACTIVE_LEASE_STATES:
                if not recovery and active.state in {
                    LeaseStatus.RESERVED.value,
                    LeaseStatus.DEPLOYING.value,
                    LeaseStatus.BUSY.value,
                    LeaseStatus.RELEASING.value,
                }:
                    database.commit()
                    return
                if remote_lease == active.lease_id and remote_artifact == active.current_artifact_id:
                    worker.fpga_ready = int(bool(status.get("fpga_ready")))
                    # During normal monitoring, health/status must not overwrite
                    # an in-flight RESERVED/DEPLOYING/BUSY transition. Recovery
                    # after a Central restart has no such in-memory operation.
                    if recovery or worker.state == WorkerState.OFFLINE.value:
                        worker.state = WorkerState.READY.value
                        active.state = LeaseStatus.READY.value
                else:
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = "worker ownership differs from persistent lease"
                    active.state = LeaseStatus.LOST.value
                    active.error = worker.last_error
            elif remote_lease:
                worker.state = WorkerState.ERROR.value
                worker.lease_id = str(remote_lease)
                worker.current_artifact_id = (
                    str(remote_artifact) if remote_artifact is not None else None
                )
                worker.last_error = "worker reports ownership unknown to Central Server"
            else:
                worker.state = WorkerState.IDLE.value
                worker.lease_id = None
                worker.fpga_ready = int(bool(status.get("fpga_ready")))
            database.commit()

    @staticmethod
    def _mark_offline(database: Session, worker: Worker, error: str) -> None:
        worker.state = WorkerState.OFFLINE.value
        worker.last_error = error
        if worker.lease_id:
            active = database.scalar(select(StudentLease).where(
                StudentLease.lease_id == worker.lease_id))
            if active and active.state in ACTIVE_LEASE_STATES:
                active.state = LeaseStatus.LOST.value
                active.error = f"worker {worker.board} offline: {error}"
                running = database.scalar(select(PredictRequestRecord).where(
                    PredictRequestRecord.student_id == active.student_id,
                    PredictRequestRecord.status == RequestStatus.RUNNING.value))
                if running:
                    running.status = RequestStatus.FAILED.value
                    running.error = active.error
                    running.completed_at = utcnow()
                LOGGER.error(
                    "active lease lost: student_id=%s lease_id=%s artifact_id=%s board=%s",
                    active.student_id,
                    active.lease_id,
                    active.current_artifact_id,
                    worker.board,
                )

    async def monitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.health_interval)
                with self.sessions() as database:
                    boards = [
                        worker.board
                        for worker in database.scalars(select(Worker)).all()
                    ]
                results = await asyncio.gather(
                    *(self._check_worker(board) for board in boards),
                    return_exceptions=True,
                )
                for board, result in zip(boards, results, strict=True):
                    if isinstance(result, Exception):
                        LOGGER.error(
                            "worker monitor check failed: board=%s error=%s",
                            board,
                            result,
                        )
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        self.monitor_task = asyncio.create_task(self.monitor(), name="worker-health")

    async def stop(self) -> None:
        if self.monitor_task is None:
            return
        self.monitor_task.cancel()
        await asyncio.gather(self.monitor_task, return_exceptions=True)
