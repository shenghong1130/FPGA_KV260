from __future__ import annotations

from datetime import timedelta
import pytest

from app.db_models import (
    PredictRequestRecord, RequestStatus, StudentLease, utcnow,
)
from .conftest import password_headers, predict, upload

pytestmark = pytest.mark.asyncio


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
