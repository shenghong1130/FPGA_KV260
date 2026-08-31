from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db_models import PredictRequestRecord, RequestStatus
from .conftest import password_headers, predict, upload

pytestmark = pytest.mark.asyncio


async def test_request_list_is_empty_by_default(test_context) -> None:
    client, _ = test_context

    response = await client.get("/requests")

    assert response.status_code == 200
    assert response.json() == []


async def test_request_list_orders_limits_and_exposes_all_statuses(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="request-list-student")
    sessions = fake.application.state.services.database.sessions
    created = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    records = [
        PredictRequestRecord(
            id="req_queued",
            student_id="request-list-student",
            artifact_id=artifact["artifact_id"],
            artifact_version=artifact["version"],
            status=RequestStatus.QUEUED.value,
            payload={"value": 1},
            created_at=created,
        ),
        PredictRequestRecord(
            id="req_running",
            student_id="request-list-student",
            artifact_id=artifact["artifact_id"],
            artifact_version=artifact["version"],
            status=RequestStatus.RUNNING.value,
            payload={"value": 2},
            created_at=created + timedelta(seconds=1),
            started_at=created + timedelta(seconds=2),
        ),
        PredictRequestRecord(
            id="req_completed",
            student_id="request-list-student",
            artifact_id=artifact["artifact_id"],
            artifact_version=artifact["version"],
            status=RequestStatus.COMPLETED.value,
            payload={"value": 3},
            result={"flower_cn": "桃花", "flower": "taohua", "confidence": 2.95},
            created_at=created + timedelta(seconds=3),
            started_at=created + timedelta(seconds=4),
            completed_at=created + timedelta(seconds=5),
        ),
        PredictRequestRecord(
            id="req_failed",
            student_id="request-list-student",
            artifact_id=artifact["artifact_id"],
            artifact_version=artifact["version"],
            status=RequestStatus.FAILED.value,
            payload={"value": 4},
            error="FPGA execution failed",
            created_at=created + timedelta(seconds=6),
            started_at=created + timedelta(seconds=7),
            completed_at=created + timedelta(seconds=8),
        ),
    ]
    with sessions() as database:
        database.add_all(records)
        database.commit()

    response = await client.get("/requests")
    assert response.status_code == 200
    items = response.json()
    assert [item["request_id"] for item in items] == [
        "req_failed", "req_completed", "req_running", "req_queued"
    ]
    by_id = {item["request_id"]: item for item in items}
    assert by_id["req_queued"]["status"] == "queued"
    assert by_id["req_running"]["status"] == "running"
    assert by_id["req_completed"]["result"]["flower"] == "taohua"
    assert by_id["req_failed"]["error"] == "FPGA execution failed"
    assert all(item["worker"] is None for item in items)
    assert "payload" not in by_id["req_completed"]

    limited = await client.get("/requests", params={"limit": 2})
    assert limited.status_code == 200
    assert [item["request_id"] for item in limited.json()] == [
        "req_failed", "req_completed"
    ]

    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["requests"] == {
        "completed": 1, "failed": 1, "queued": 1, "running": 1
    }


async def test_request_list_limit_validation(test_context) -> None:
    client, _ = test_context

    assert (await client.get("/requests", params={"limit": 0})).status_code == 422
    assert (await client.get("/requests", params={"limit": 501})).status_code == 422


async def test_request_list_filters_by_student_before_applying_limit(
    test_context,
) -> None:
    client, _ = test_context
    await upload(client, student="student-filter-a")
    await upload(client, student="student-filter-b")
    first_a = (await predict(client, "student-filter-a", value="first-a")).json()
    await predict(client, "student-filter-b", value="only-b")
    latest_a = (await predict(client, "student-filter-a", value="latest-a")).json()

    response = await client.get(
        "/requests", params={"student_id": "student-filter-a", "limit": 100}
    )
    assert response.status_code == 200
    items = response.json()
    assert [item["request_id"] for item in items] == [
        latest_a["request_id"], first_a["request_id"]
    ]
    assert {item["student_id"] for item in items} == {"student-filter-a"}

    limited = await client.get(
        "/requests", params={"student_id": "student-filter-a", "limit": 1}
    )
    assert limited.status_code == 200
    assert [item["request_id"] for item in limited.json()] == [
        latest_a["request_id"]
    ]

    missing = await client.get(
        "/requests", params={"student_id": "student-with-no-requests"}
    )
    assert missing.status_code == 200
    assert missing.json() == []


async def test_predict_and_owned_request_auth_contracts_remain_unchanged(
    test_context,
) -> None:
    client, _ = test_context
    await upload(client, student="auth-student", password="student-password")

    response = await predict(
        client, "auth-student", value=7, password="student-password"
    )
    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "completed"
    assert item["result"]["input"] == {"value": 7}

    request_path = f"/requests/{item['request_id']}"
    assert (await client.get(
        request_path, headers=password_headers("student-password")
    )).status_code == 200
    assert (await client.get(
        request_path, headers=password_headers("wrong-password")
    )).status_code == 401
    assert (await client.get(request_path)).status_code == 401
