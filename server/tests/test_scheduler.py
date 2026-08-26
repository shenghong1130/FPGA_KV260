from __future__ import annotations

import asyncio
from datetime import timedelta
import pytest

from app.db_models import LeaseStatus, StudentLease, Worker, WorkerState, utcnow
from .conftest import predict, upload

pytestmark = pytest.mark.asyncio


async def test_no_worker_queues_once_per_student(test_context) -> None:
    client, fake = test_context
    for student in ("a", "b", "c", "d"):
        await upload(client, student=student)
    for student in ("a", "b", "c"):
        assert (await predict(client, student)).status_code == 200
    responses = await asyncio.gather(*(predict(client, "d", i) for i in range(3)))
    assert all(response.status_code == 202 for response in responses)
    with fake.application.state.services.database.sessions() as database:
        leases = database.query(StudentLease).filter_by(student_id="d").all()
        assert len(leases) == 1 and leases[0].state == LeaseStatus.QUEUED.value


async def test_lru_pressure_reclaim_runs_queued_request(test_context) -> None:
    client, fake = test_context
    for student in ("a", "b", "c", "d"):
        await upload(client, student=student)
    for student in ("a", "b", "c"):
        await predict(client, student)
    queued = await predict(client, "d")
    services = fake.application.state.services
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        lease.last_activity_at = utcnow() - timedelta(seconds=10)
        database.commit()
    object.__setattr__(services.settings, "lease_reclaim_grace_seconds", 1)
    await services.lease_manager.reap_once()
    for _ in range(100):
        result = (await client.get(f"/requests/{queued.json()['request_id']}")).json()
        if result["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert result["status"] == "completed"


async def test_busy_worker_is_not_reclaimable(test_context) -> None:
    client, fake = test_context
    await upload(client, student="a")
    await predict(client, "a")
    services = fake.application.state.services
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        worker = database.get(Worker, lease.worker_id)
        lease.state = LeaseStatus.BUSY.value
        worker.state = WorkerState.BUSY.value
        lease.last_activity_at = utcnow() - timedelta(hours=1)
        database.commit()
    object.__setattr__(services.settings, "lease_idle_timeout_seconds", 1)
    await services.lease_manager.reap_once()
    with services.database.sessions() as database:
        assert database.get(StudentLease, "a").state == LeaseStatus.BUSY.value


async def test_queued_request_keeps_submission_artifact(test_context) -> None:
    client, fake = test_context
    for student in ("a", "b", "c"):
        await upload(client, student=student)
        await predict(client, student)
    v1 = await upload(client, student="d", bit=b"version-one")
    queued = await predict(client, "d", "old")
    v2 = await upload(client, student="d", bit=b"version-two")
    assert queued.status_code == 202
    assert queued.json()["artifact_id"] == v1["artifact_id"]
    assert queued.json()["version"] == "v1" and v2["version"] == "v2"


async def test_same_student_queued_requests_execute_fifo(test_context) -> None:
    client, fake = test_context
    for student in ("a", "b", "c", "d"):
        await upload(client, student=student)
    for student in ("a", "b", "c"):
        await predict(client, student)
    queued = [await predict(client, "d", value) for value in (1, 2, 3)]
    services = fake.application.state.services
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        lease.last_activity_at = utcnow() - timedelta(seconds=10)
        database.commit()
    object.__setattr__(services.settings, "lease_reclaim_grace_seconds", 1)
    await services.lease_manager.reap_once()
    results = []
    for response in queued:
        for _ in range(100):
            body = (await client.get(f"/requests/{response.json()['request_id']}")).json()
            if body["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        results.append(body["result"])
    assert [item["input"]["value"] for item in results] == [1, 2, 3]
    assert [item["predict_index"] for item in results] == [1, 2, 3]
