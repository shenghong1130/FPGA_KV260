from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import SessionRecord, SessionStatus, Worker, WorkerState, utcnow
from .worker_client import WorkerClient, WorkerClientError

LOGGER = logging.getLogger(__name__)
ACTIVE_SESSION_STATES = {
    SessionStatus.RESERVED.value,
    SessionStatus.DEPLOYING.value,
    SessionStatus.READY.value,
    SessionStatus.BUSY.value,
    SessionStatus.RELEASING.value,
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
                if board not in configured and worker.session_id is None:
                    database.delete(worker)
            database.commit()

    async def recover(self) -> None:
        with self.sessions() as database:
            boards = [worker.board for worker in database.scalars(select(Worker)).all()]
        for board in boards:
            await self._check_worker(board, recovery=True)

    async def _check_worker(self, board: str, recovery: bool = False) -> None:
        with self.sessions() as database:
            worker = database.get(Worker, board)
            if worker is None:
                return
            try:
                health = await self.client.health(worker)
                if not health.get("ok"):
                    raise WorkerClientError(f"unhealthy response: {health}")
                status = await self.client.status(worker)
            except WorkerClientError as exc:
                self.failures[board] += 1
                if recovery or self.failures[board] >= self.failure_threshold:
                    await self._mark_offline(database, worker, str(exc))
                    database.commit()
                return

            self.failures[board] = 0
            worker.last_seen = utcnow()
            worker.last_error = None
            remote_session = status.get("session_id")
            remote_artifact = status.get("artifact_id")
            active = None
            if worker.session_id:
                active = database.get(SessionRecord, worker.session_id)
            if active and active.status in ACTIVE_SESSION_STATES:
                if remote_session == active.id and remote_artifact == active.artifact_id:
                    worker.fpga_ready = int(bool(status.get("fpga_ready")))
                    # During normal monitoring, health/status must not overwrite
                    # an in-flight RESERVED/DEPLOYING/BUSY transition. Recovery
                    # after a Central restart has no such in-memory operation.
                    if recovery or worker.state == WorkerState.OFFLINE.value:
                        worker.state = WorkerState.READY.value
                        active.status = SessionStatus.READY.value
                else:
                    worker.state = WorkerState.ERROR.value
                    worker.last_error = "worker ownership differs from persistent session"
                    active.status = SessionStatus.LOST.value
                    active.error = worker.last_error
            elif remote_session:
                worker.state = WorkerState.ERROR.value
                worker.session_id = str(remote_session)
                worker.current_artifact_id = (
                    str(remote_artifact) if remote_artifact is not None else None
                )
                worker.last_error = "worker reports ownership unknown to Central Server"
            else:
                worker.state = WorkerState.IDLE.value
                worker.session_id = None
                worker.fpga_ready = int(bool(status.get("fpga_ready")))
            database.commit()

    @staticmethod
    async def _mark_offline(
        database: Session, worker: Worker, error: str
    ) -> None:
        worker.state = WorkerState.OFFLINE.value
        worker.last_error = error
        if worker.session_id:
            active = database.get(SessionRecord, worker.session_id)
            if active and active.status in ACTIVE_SESSION_STATES:
                active.status = SessionStatus.LOST.value
                active.error = f"worker {worker.board} offline: {error}"
                LOGGER.error(
                    "active session lost: session_id=%s artifact_id=%s board=%s",
                    active.id,
                    active.artifact_id,
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
                await asyncio.gather(
                    *(self._check_worker(board) for board in boards),
                    return_exceptions=False,
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
