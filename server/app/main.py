from __future__ import annotations

import logging
import mimetypes
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import URL
from starlette.responses import RedirectResponse, Response

from .api import artifacts, health, predict, students, workers
from .artifact_store import ArtifactStore
from .config import Settings
from .database import Database
from .scheduler import Scheduler
from .lease_manager import LeaseManager
from .worker_client import WorkerClient
from .worker_registry import WorkerRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@dataclass(slots=True)
class Services:
    settings: Settings
    database: Database
    artifact_store: ArtifactStore
    worker_client: WorkerClient
    worker_registry: WorkerRegistry
    scheduler: Scheduler
    lease_manager: LeaseManager


class DashboardStaticFiles(StaticFiles):
    """Serve the small bundled dashboard without relying on the process cwd."""

    async def check_config(self) -> None:
        if self.directory is None or not Path(self.directory).is_dir():
            raise RuntimeError(
                f"StaticFiles directory '{self.directory}' does not exist."
            )

    async def get_response(self, path: str, scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=405)

        full_path, stat_result = self.lookup_path(path)
        if stat_result is not None and stat.S_ISDIR(stat_result.st_mode) and self.html:
            full_path, stat_result = self.lookup_path(str(Path(path) / "index.html"))
            if stat_result is not None and stat.S_ISREG(stat_result.st_mode):
                if not scope["path"].endswith("/"):
                    url = URL(scope=scope).replace(path=scope["path"] + "/")
                    return RedirectResponse(url=url)

        if stat_result is None or not stat.S_ISREG(stat_result.st_mode):
            raise HTTPException(status_code=404)

        content = Path(full_path).read_bytes()
        media_type, _ = mimetypes.guess_type(full_path)
        return Response(
            content=b"" if scope["method"] == "HEAD" else content,
            media_type=media_type or "application/octet-stream",
        )


def create_app(
    settings: Settings | None = None, worker_client: WorkerClient | None = None
) -> FastAPI:
    selected_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        selected_settings.artifact_root.mkdir(parents=True, exist_ok=True)
        database = Database(selected_settings.database_url)
        database.initialize()
        client = worker_client or WorkerClient(selected_settings)
        artifact_store = ArtifactStore(
            selected_settings.artifact_root,
            database.sessions,
            selected_settings.max_bit_size,
            selected_settings.max_hwh_size,
        )
        scheduler = Scheduler(database.sessions)
        registry = WorkerRegistry(
            database.sessions,
            client,
            selected_settings.workers_config,
            selected_settings.health_interval_seconds,
            selected_settings.health_failure_threshold,
        )
        manager = LeaseManager(database.sessions, scheduler, client, selected_settings)
        services = Services(
            settings=selected_settings,
            database=database,
            artifact_store=artifact_store,
            worker_client=client,
            worker_registry=registry,
            scheduler=scheduler,
            lease_manager=manager,
        )
        app.state.services = services
        await registry.sync_config()
        await manager.recover_requests()
        await registry.recover()
        manager.start()
        registry.start()
        try:
            yield
        finally:
            await registry.stop()
            await manager.stop()
            await client.close()
            database.close()

    application = FastAPI(
        title="KV260 Central Scheduler",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(health.router)
    application.include_router(artifacts.router)
    application.include_router(predict.router)
    application.include_router(students.router)
    application.include_router(workers.router)
    ui_dir = Path(__file__).resolve().parents[1] / "ui"
    application.mount(
        "/ui",
        DashboardStaticFiles(directory=ui_dir, html=True),
        name="ui",
    )
    return application


app = create_app()
