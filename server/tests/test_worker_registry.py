from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import event

from app.db_models import LeaseStatus, StudentLease, Worker, WorkerState, utcnow
from app.config import Settings
from app.worker_client import WorkerClientError

pytestmark = pytest.mark.asyncio


async def test_default_health_interval_remains_five_seconds(monkeypatch) -> None:
    monkeypatch.delenv("HEALTH_INTERVAL_SECONDS", raising=False)
    assert Settings.from_env().health_interval_seconds == 5.0


async def test_health_check_holds_no_database_transaction_during_network_wait(
    test_context,
) -> None:
    client, fake = test_context
    services = fake.application.state.services
    entered = asyncio.Event()
    resume = asyncio.Event()
    original_health = fake.health

    async def blocked_health(worker):
        entered.set()
        await resume.wait()
        return await original_health(worker)

    fake.health = blocked_health
    active_transactions = 0

    def began(*_args):
        nonlocal active_transactions
        active_transactions += 1

    def ended(*_args):
        nonlocal active_transactions
        active_transactions -= 1

    event.listen(services.database.engine, "begin", began)
    event.listen(services.database.engine, "commit", ended)
    event.listen(services.database.engine, "rollback", ended)
    try:
        check = asyncio.create_task(
            services.worker_registry._check_worker("mock-kv2601")
        )
        await entered.wait()
        assert active_transactions == 0
        response = await asyncio.wait_for(client.get("/health"), timeout=0.25)
        assert response.status_code == 200
        assert active_transactions == 0
        resume.set()
        await check
    finally:
        event.remove(services.database.engine, "begin", began)
        event.remove(services.database.engine, "commit", ended)
        event.remove(services.database.engine, "rollback", ended)


async def test_monitor_does_not_overwrite_busy_transition(test_context) -> None:
    _, fake = test_context
    services = fake.application.state.services
    with services.database.sessions() as database:
        worker = database.get(Worker, "mock-kv2601")
        worker.state = WorkerState.BUSY.value
        worker.lease_id = "lease_busy"
        worker.current_artifact_id = "art_busy"
        database.add(StudentLease(
            student_id="busy-student",
            lease_id="lease_busy",
            worker_id=worker.board,
            current_artifact_id="art_busy",
            state=LeaseStatus.BUSY.value,
            created_at=utcnow(),
            last_activity_at=utcnow(),
        ))
        database.commit()
    fake.states["mock-kv2601"] = {
        "lease_id": None, "artifact_id": None, "predict_count": 0
    }
    await services.worker_registry._check_worker("mock-kv2601")
    with services.database.sessions() as database:
        worker = database.get(Worker, "mock-kv2601")
        lease = database.get(StudentLease, "busy-student")
        assert worker.state == WorkerState.BUSY.value
        assert lease.state == LeaseStatus.BUSY.value


async def test_failure_threshold_and_recovery(test_context) -> None:
    _, fake = test_context
    services = fake.application.state.services

    async def failed(_worker):
        raise WorkerClientError("unreachable")

    fake.health = failed
    await services.worker_registry._check_worker("mock-kv2601")
    with services.database.sessions() as database:
        assert database.get(Worker, "mock-kv2601").state == WorkerState.IDLE.value
    await services.worker_registry._check_worker("mock-kv2601")
    await services.worker_registry._check_worker("mock-kv2601")
    with services.database.sessions() as database:
        assert database.get(Worker, "mock-kv2601").state == WorkerState.OFFLINE.value

    async def healthy(_worker):
        return {"ok": True}

    fake.health = healthy
    await services.worker_registry._check_worker("mock-kv2601")
    with services.database.sessions() as database:
        worker = database.get(Worker, "mock-kv2601")
        assert worker.state == WorkerState.IDLE.value
        assert worker.last_error is None


async def test_twenty_worker_checks_are_concurrent(test_context) -> None:
    client, fake = test_context
    services = fake.application.state.services
    with services.database.sessions() as database:
        for index in range(4, 21):
            database.add(Worker(
                board=f"mock-kv260{index}",
                base_url=f"http://mock{index}",
                state=WorkerState.OFFLINE.value,
            ))
        database.commit()

    async def delayed_health(worker):
        await asyncio.sleep(0.08 if worker.board.endswith(("7", "9")) else 0.03)
        if worker.board.endswith("8"):
            raise WorkerClientError("simulated timeout")
        return {"ok": True}

    async def delayed_status(worker):
        await asyncio.sleep(0.03)
        return {
            "board": worker.board,
            "fpga_ready": False,
            "lease_id": None,
            "artifact_id": None,
        }

    fake.health = delayed_health
    fake.status = delayed_status
    started = time.monotonic()
    checks = asyncio.gather(*(
        services.worker_registry._check_worker(f"mock-kv260{index}")
        for index in range(1, 21)
    ))
    await asyncio.sleep(0.01)
    health, workers, artifacts, dashboard = await asyncio.gather(
        asyncio.wait_for(client.get("/health"), timeout=0.25),
        asyncio.wait_for(client.get("/workers"), timeout=0.25),
        asyncio.wait_for(client.get("/fpga/artifacts"), timeout=0.25),
        asyncio.wait_for(client.get("/ui/"), timeout=0.25),
    )
    assert (
        health.status_code == workers.status_code == artifacts.status_code
        == dashboard.status_code == 200
    )
    await checks
    assert time.monotonic() - started < 0.5


async def test_remote_ownership_mismatch_marks_error_and_lost(test_context) -> None:
    _, fake = test_context
    services = fake.application.state.services
    with services.database.sessions() as database:
        worker = database.get(Worker, "mock-kv2601")
        worker.state = WorkerState.READY.value
        worker.lease_id = "lease_local"
        worker.current_artifact_id = "art_local"
        database.add(StudentLease(
            student_id="owner",
            lease_id="lease_local",
            worker_id=worker.board,
            current_artifact_id="art_local",
            state=LeaseStatus.READY.value,
            created_at=utcnow(),
            last_activity_at=utcnow(),
        ))
        database.commit()
    fake.states["mock-kv2601"] = {
        "lease_id": "lease_remote",
        "artifact_id": "art_remote",
        "predict_count": 0,
    }
    await services.worker_registry._check_worker("mock-kv2601")
    with services.database.sessions() as database:
        assert database.get(Worker, "mock-kv2601").state == WorkerState.ERROR.value
        assert database.get(StudentLease, "owner").state == LeaseStatus.LOST.value


async def test_repeated_monitor_checks_do_not_deadlock(test_context) -> None:
    client, fake = test_context
    services = fake.application.state.services
    for _ in range(12):
        await asyncio.wait_for(asyncio.gather(*(
            services.worker_registry._check_worker(f"mock-kv260{index}")
            for index in range(1, 4)
        )), timeout=0.5)
        assert (await asyncio.wait_for(client.get("/health"), timeout=0.25)).status_code == 200
