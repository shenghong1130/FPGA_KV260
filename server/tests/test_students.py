from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db_models import (
    LeaseStatus,
    PredictRequestRecord,
    RequestStatus,
    StudentCredential,
    StudentLease,
)
from .conftest import password_headers, upload

pytestmark = pytest.mark.asyncio


async def test_student_list_unions_persistent_sources_and_deduplicates(
    test_context,
) -> None:
    client, fake = test_context
    artifact = await upload(client, student="artifact-only")
    sessions = fake.application.state.services.database.sessions
    now = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
    with sessions() as database:
        database.add(StudentCredential(
            student_id="credential-only",
            password_salt=b"s" * 32,
            password_hash=b"h" * 64,
            created_at=now,
            updated_at=now,
        ))
        database.add(StudentLease(
            student_id="lease-only",
            state=LeaseStatus.READY.value,
            worker_id="kv2603",
            lease_id="lease_student_list",
            created_at=now,
            last_activity_at=now,
        ))
        database.add(PredictRequestRecord(
            id="req_history_only",
            student_id="request-only",
            artifact_id=artifact["artifact_id"],
            artifact_version=artifact["version"],
            status=RequestStatus.COMPLETED.value,
            payload={"value": 1},
            result={"value": 1},
            created_at=now,
            completed_at=now,
        ))
        database.commit()

    response = await client.get("/students")
    assert response.status_code == 200
    items = response.json()
    by_student = {item["student_id"]: item for item in items}
    assert {
        "artifact-only", "credential-only", "lease-only", "request-only"
    } <= set(by_student)
    assert len(items) == len(by_student)
    assert by_student["artifact-only"]["latest_artifact_id"] == artifact["artifact_id"]
    assert by_student["artifact-only"]["worker_id"] is None
    assert by_student["lease-only"]["worker_id"] == "kv2603"
    assert by_student["request-only"]["total_requests"] == 1


async def test_student_list_request_counters_and_safe_fields(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="counter-student")
    sessions = fake.application.state.services.database.sessions
    created = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
    statuses = (
        [RequestStatus.QUEUED.value] * 2
        + [RequestStatus.RUNNING.value]
        + [RequestStatus.COMPLETED.value] * 5
        + [RequestStatus.FAILED.value] * 2
    )
    with sessions() as database:
        database.add_all([
            PredictRequestRecord(
                id=f"req_counter_{index}",
                student_id="counter-student",
                artifact_id=artifact["artifact_id"],
                artifact_version=artifact["version"],
                status=status,
                payload={"value": index},
                created_at=created + timedelta(seconds=index),
            )
            for index, status in enumerate(statuses)
        ])
        database.commit()

    response = await client.get("/students")
    assert response.status_code == 200
    item = next(
        row for row in response.json() if row["student_id"] == "counter-student"
    )
    assert item["queued_requests"] == 2
    assert item["running_requests"] == 1
    assert item["completed_requests"] == 5
    assert item["failed_requests"] == 2
    assert item["total_requests"] == 10
    assert set(item) == {
        "student_id", "latest_artifact_id", "latest_version", "lease_state",
        "worker_id", "queued_requests", "running_requests",
        "completed_requests", "failed_requests", "total_requests",
        "last_activity_at",
    }
    serialized = response.text.lower()
    for secret_name in (
        "password", "password_hash", "password_salt", "admin_token", "salt"
    ):
        assert secret_name not in serialized


async def test_owned_student_status_still_requires_password_and_has_totals(
    test_context,
) -> None:
    client, _ = test_context
    await upload(client, student="private-status", password="private-password")
    path = "/students/private-status/status"

    assert (await client.get(path)).status_code == 401
    assert (await client.get(
        path, headers=password_headers("wrong-password")
    )).status_code == 401
    response = await client.get(
        path, headers=password_headers("private-password")
    )
    assert response.status_code == 200
    item = response.json()
    assert item["total_requests"] == 0
    assert item["completed_requests"] == 0
    assert item["failed_requests"] == 0
    assert "worker_id" not in item
