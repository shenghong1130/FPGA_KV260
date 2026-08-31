from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db_models import (
    LeaseStatus, PredictRequestRecord, RequestStatus, StudentLease, Worker,
    WorkerState, utcnow,
)
from app.worker_client import WorkerClientError
from .conftest import password_headers, predict, upload

pytestmark = pytest.mark.asyncio
ADMIN_TOKEN = "test-admin-action-token"


def configure_admin(fake) -> None:
    object.__setattr__(
        fake.application.state.services.settings,
        "admin_action_token",
        ADMIN_TOKEN,
    )


def admin_headers(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token}


async def ready_worker(client, student: str = "a") -> dict:
    await upload(client, student=student)
    assert (await predict(client, student)).json()["status"] == "completed"
    return next(
        row for row in (await client.get("/workers")).json()
        if row["student_id"] == student
    )


async def test_idle_timeout_releases_without_student_api(test_context) -> None:
    client, fake = test_context
    await upload(client, student="a")
    await predict(client, "a")
    services = fake.application.state.services
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        lease.last_activity_at = utcnow() - timedelta(seconds=10)
        database.commit()
    object.__setattr__(services.settings, "lease_idle_timeout_seconds", 1)
    await services.lease_manager.reap_once()
    status = (await client.get("/students/a/status", headers=password_headers())).json()
    assert status["lease_state"] == "unassigned"
    assert all(row["state"] == "idle" for row in (await client.get("/workers")).json())


async def test_predict_failure_allows_future_reallocation(test_context) -> None:
    client, fake = test_context
    await upload(client, student="a")
    first = await predict(client, "a")
    lease_id = next(row["lease_id"] for row in (await client.get("/workers")).json()
                    if row["student_id"] == "a")
    fake.fail_predict_for.add(lease_id)
    failed = await predict(client, "a", 2)
    assert failed.json()["status"] == "failed"
    fake.fail_predict_for.clear()
    future = await predict(client, "a", 3)
    assert future.json()["status"] == "completed"
    new_lease = next(row["lease_id"] for row in (await client.get("/workers")).json()
                     if row["student_id"] == "a")
    assert new_lease != lease_id


async def test_workers_api_uses_lease_fields(test_context) -> None:
    client, _ = test_context
    await upload(client, student="a")
    await predict(client, "a")
    owned = next(row for row in (await client.get("/workers")).json() if row["state"] == "ready")
    assert owned["lease_id"].startswith("lease_") and owned["student_id"] == "a"
    assert "session_id" not in owned


async def test_restart_marks_running_failed_and_preserves_queued(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="a")
    services = fake.application.state.services
    with services.database.sessions() as database:
        running = PredictRequestRecord(
            id="req_running", student_id="a", artifact_id=artifact["artifact_id"],
            artifact_version="v1", status=RequestStatus.RUNNING.value,
            payload={"value": 1}, created_at=utcnow(),
        )
        queued = PredictRequestRecord(
            id="req_queued", student_id="a", artifact_id=artifact["artifact_id"],
            artifact_version="v1", status=RequestStatus.QUEUED.value,
            payload={"value": 2}, created_at=utcnow(),
        )
        database.add_all([running, queued])
        database.commit()
    await services.lease_manager.recover_requests()
    with services.database.sessions() as database:
        assert database.get(PredictRequestRecord, "req_running").status == RequestStatus.FAILED.value
        assert database.get(PredictRequestRecord, "req_queued").status == RequestStatus.QUEUED.value


async def test_manual_release_success(test_context) -> None:
    client, fake = test_context
    configure_admin(fake)
    owned = await ready_worker(client)

    response = await client.post(
        f"/workers/{owned['board']}/release", headers=admin_headers()
    )

    assert response.status_code == 200
    assert response.json() == {
        "released": True, "board": owned["board"], "student_id": "a"
    }
    services = fake.application.state.services
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        worker = database.get(Worker, owned["board"])
        assert lease.state == LeaseStatus.UNASSIGNED.value
        assert lease.worker_id is None and lease.lease_id is None
        assert lease.current_artifact_id is None and lease.released_at is not None
        assert worker.state == WorkerState.IDLE.value
        assert worker.lease_id is None and worker.current_artifact_id is None
        assert not worker.fpga_ready
    assert fake.states[owned["board"]]["lease_id"] is None


async def test_manual_release_wakes_allocator_for_oldest_queued_student(
    test_context,
) -> None:
    client, fake = test_context
    configure_admin(fake)
    services = fake.application.state.services
    with services.database.sessions() as database:
        database.get(Worker, "mock-kv2602").state = WorkerState.OFFLINE.value
        database.get(Worker, "mock-kv2603").state = WorkerState.OFFLINE.value
        database.commit()
    owned = await ready_worker(client, "a")
    await upload(client, student="b")
    queued = await predict(client, "b")
    assert queued.status_code == 202 and queued.json()["status"] == "queued"

    released = await client.post(
        f"/workers/{owned['board']}/release", headers=admin_headers()
    )
    assert released.status_code == 200

    request_id = queued.json()["request_id"]
    for _ in range(100):
        result = await client.get(
            f"/requests/{request_id}", headers=password_headers()
        )
        if result.json()["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert result.json()["status"] == "completed"
    reassigned = next(
        row for row in (await client.get("/workers")).json()
        if row["student_id"] == "b"
    )
    assert reassigned["board"] == owned["board"]


async def test_busy_worker_cannot_be_manually_released(test_context) -> None:
    client, fake = test_context
    configure_admin(fake)
    owned = await ready_worker(client)
    entered = asyncio.Event()
    resume = asyncio.Event()
    original_predict = fake.predict

    async def blocked_predict(worker, lease_id, payload):
        entered.set()
        await resume.wait()
        return await original_predict(worker, lease_id, payload)

    fake.predict = blocked_predict
    operation = asyncio.create_task(predict(client, "a", 2))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        response = await client.post(
            f"/workers/{owned['board']}/release", headers=admin_headers()
        )
        assert response.status_code == 409
        services = fake.application.state.services
        with services.database.sessions() as database:
            lease = database.get(StudentLease, "a")
            worker = database.get(Worker, owned["board"])
            running = database.scalar(
                select(PredictRequestRecord).where(
                    PredictRequestRecord.student_id == "a",
                    PredictRequestRecord.status == RequestStatus.RUNNING.value,
                )
            )
            assert running is not None
            assert lease.state == LeaseStatus.BUSY.value
            assert lease.lease_id == owned["lease_id"]
            assert worker.state == WorkerState.BUSY.value
            assert worker.lease_id == owned["lease_id"]
    finally:
        resume.set()
        assert (await operation).json()["status"] == "completed"


@pytest.mark.parametrize(
    ("lease_state", "worker_state"),
    [
        (LeaseStatus.DEPLOYING.value, WorkerState.DEPLOYING.value),
        (LeaseStatus.RESERVED.value, WorkerState.RESERVED.value),
        (LeaseStatus.RELEASING.value, WorkerState.READY.value),
        (LeaseStatus.READY.value, WorkerState.OFFLINE.value),
        (LeaseStatus.ERROR.value, WorkerState.ERROR.value),
    ],
)
async def test_non_ready_worker_cannot_be_manually_released(
    test_context, lease_state: str, worker_state: str
) -> None:
    client, fake = test_context
    configure_admin(fake)
    owned = await ready_worker(client)
    services = fake.application.state.services
    with services.database.sessions() as database:
        database.get(StudentLease, "a").state = lease_state
        database.get(Worker, owned["board"]).state = worker_state
        database.commit()

    response = await client.post(
        f"/workers/{owned['board']}/release", headers=admin_headers()
    )
    assert response.status_code == 409


async def test_ready_worker_with_queued_request_cannot_be_manually_released(
    test_context,
) -> None:
    client, fake = test_context
    configure_admin(fake)
    owned = await ready_worker(client)
    services = fake.application.state.services
    with services.database.sessions() as database:
        database.add(PredictRequestRecord(
            id="req_manual_release_queued",
            student_id="a",
            artifact_id=owned["artifact_id"],
            artifact_version="v1",
            status=RequestStatus.QUEUED.value,
            payload={"value": "queued"},
            created_at=utcnow(),
        ))
        database.commit()

    response = await client.post(
        f"/workers/{owned['board']}/release", headers=admin_headers()
    )
    assert response.status_code == 409
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        worker = database.get(Worker, owned["board"])
        request = database.get(PredictRequestRecord, "req_manual_release_queued")
        assert lease.state == LeaseStatus.READY.value
        assert lease.lease_id == owned["lease_id"]
        assert worker.state == WorkerState.READY.value
        assert worker.lease_id == owned["lease_id"]
        assert request.status == RequestStatus.QUEUED.value


async def test_idle_and_unknown_worker_release_have_stable_errors(test_context) -> None:
    client, fake = test_context
    configure_admin(fake)

    idle = await client.post(
        "/workers/mock-kv2601/release", headers=admin_headers()
    )
    missing = await client.post(
        "/workers/does-not-exist/release", headers=admin_headers()
    )

    assert idle.status_code == 409
    assert idle.json()["detail"] == "worker is not safely releasable"
    assert missing.status_code == 404


async def test_manual_release_requires_configured_valid_admin_token(
    test_context,
) -> None:
    client, fake = test_context
    unconfigured = await client.post("/workers/mock-kv2601/release")
    assert unconfigured.status_code == 503
    assert unconfigured.json()["detail"] == "manual admin actions are not configured"

    configure_admin(fake)
    missing = await client.post("/workers/mock-kv2601/release")
    wrong = await client.post(
        "/workers/mock-kv2601/release", headers=admin_headers("wrong-token")
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401


async def test_worker_release_failure_preserves_ownership_and_sets_error(
    test_context,
) -> None:
    client, fake = test_context
    configure_admin(fake)
    owned = await ready_worker(client)

    async def failed_release(worker, lease_id):
        raise WorkerClientError("simulated release failure")

    fake.release = failed_release
    response = await client.post(
        f"/workers/{owned['board']}/release", headers=admin_headers()
    )

    assert response.status_code == 502
    assert "simulated release failure" in response.json()["detail"]
    services = fake.application.state.services
    with services.database.sessions() as database:
        lease = database.get(StudentLease, "a")
        worker = database.get(Worker, owned["board"])
        assert lease.state == LeaseStatus.ERROR.value
        assert lease.lease_id == owned["lease_id"]
        assert worker.state == WorkerState.ERROR.value
        assert worker.lease_id == owned["lease_id"]


async def test_concurrent_manual_release_only_releases_once(test_context) -> None:
    client, fake = test_context
    configure_admin(fake)
    owned = await ready_worker(client)

    responses = await asyncio.gather(*(
        client.post(f"/workers/{owned['board']}/release", headers=admin_headers())
        for _ in range(2)
    ))

    assert sorted(response.status_code for response in responses) == [200, 409]
