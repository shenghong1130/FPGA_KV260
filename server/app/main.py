from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import FastAPI

from .api import artifacts, health, sessions, workers
from .artifact_store import ArtifactStore
from .config import Settings
from .database import Database
from .scheduler import Scheduler
from .session_manager import SessionManager
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
    session_manager: SessionManager


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
        manager = SessionManager(database.sessions, scheduler, client)
        services = Services(
            settings=selected_settings,
            database=database,
            artifact_store=artifact_store,
            worker_client=client,
            worker_registry=registry,
            scheduler=scheduler,
            session_manager=manager,
        )
        app.state.services = services
        await registry.sync_config()
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
    application.include_router(sessions.router)
    application.include_router(workers.router)
    return application


app = create_app()
