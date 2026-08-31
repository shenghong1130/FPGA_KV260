from __future__ import annotations

import pytest

from app.worker_client import WorkerClientError
from .conftest import password_headers, predict, upload

pytestmark = pytest.mark.asyncio


async def test_artifact_and_request_lifecycle_events_are_persisted(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="audit-student")
    response = await predict(client, "audit-student", value=7)
    request_id = response.json()["request_id"]

    events = (await client.get("/events?student_id=audit-student&limit=100")).json()
    types = {item["event_type"] for item in events}
    assert {
        "ARTIFACT_UPLOADED",
        "REQUEST_CREATED",
        "WORKER_ASSIGNED",
        "FPGA_DEPLOYED",
        "REQUEST_STARTED",
        "REQUEST_COMPLETED",
    } <= types
    completed = next(item for item in events if item["event_type"] == "REQUEST_COMPLETED")
    assert completed["request_id"] == request_id
    assert completed["artifact_id"] == artifact["artifact_id"]
    serialized = str(events)
    assert "image_base64" not in serialized
    assert "correct123" not in serialized
    limited = (await client.get("/events?limit=2")).json()
    assert len(limited) == 2
    assert limited[0]["created_at"] >= limited[1]["created_at"]


async def test_request_failure_and_event_filters(test_context) -> None:
    client, fake = test_context
    await upload(client, student="failed-student")
    first = await predict(client, "failed-student")
    workers = (await client.get("/workers")).json()
    lease_id = next(row["lease_id"] for row in workers if row["student_id"] == "failed-student")
    fake.fail_predict_for.add(lease_id)
    failed = await predict(client, "failed-student", value=2)
    assert failed.json()["status"] == "failed"

    events = (await client.get(
        "/events?event_type=REQUEST_FAILED&level=ERROR&student_id=failed-student"
    )).json()
    assert len(events) == 1
    assert events[0]["request_id"] == failed.json()["request_id"]
    assert events[0]["board"]
    assert first.json()["request_id"] != events[0]["request_id"]
    by_board = (await client.get(
        f"/events?board={events[0]['board']}&request_id={events[0]['request_id']}"
        f"&artifact_id={events[0]['artifact_id']}"
    )).json()
    assert any(item["event_type"] == "REQUEST_FAILED" for item in by_board)


async def test_worker_offline_is_not_duplicated_and_recovery_is_recorded(
    test_context,
) -> None:
    client, fake = test_context
    services = fake.application.state.services
    board = "mock-kv2601"
    original_health = fake.health

    async def unavailable(worker):
        raise WorkerClientError("unreachable")

    fake.health = unavailable
    await services.worker_registry._check_worker(board, recovery=True)
    await services.worker_registry._check_worker(board, recovery=True)
    offline = (await client.get(f"/events?event_type=WORKER_OFFLINE&board={board}")).json()
    assert len(offline) == 1

    fake.health = original_health
    await services.worker_registry._check_worker(board, recovery=True)
    online = (await client.get(f"/events?event_type=WORKER_ONLINE&board={board}")).json()
    # One startup transition plus the explicit recovery transition.
    assert len(online) == 2


async def test_auth_failure_password_change_and_admin_release_events(test_context) -> None:
    client, fake = test_context
    await upload(client, student="secure-student")
    bad = await client.get(
        "/students/secure-student/status",
        headers=password_headers("not-the-password"),
    )
    assert bad.status_code == 401
    changed = await client.post(
        "/students/secure-student/password",
        headers=password_headers(),
        json={"new_password": "new-correct-456"},
    )
    assert changed.status_code == 200

    await predict(client, "secure-student", password="new-correct-456")
    worker = next(
        row for row in (await client.get("/workers")).json()
        if row["student_id"] == "secure-student"
    )
    object.__setattr__(fake.application.state.services.settings,
                       "admin_action_token", "audit-admin-token")
    released = await client.post(
        f"/workers/{worker['board']}/release",
        headers={"X-Admin-Token": "audit-admin-token"},
    )
    assert released.status_code == 200

    events = (await client.get("/events?student_id=secure-student")).json()
    types = {item["event_type"] for item in events}
    assert {"AUTH_FAILED", "STUDENT_PASSWORD_CHANGED", "ADMIN_WORKER_RELEASE"} <= types
    assert "not-the-password" not in str(events)
    assert "audit-admin-token" not in str(events)
    assert "new-correct-456" not in str(events)


async def test_audit_failure_does_not_break_artifact_upload(test_context, monkeypatch) -> None:
    client, fake = test_context

    # Callers rely on AuditLogger.record being best effort; emulate its public
    # behavior by replacing its session factory rather than business services.
    class BrokenSession:
        def __call__(self):
            raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(fake.application.state.services.audit, "sessions", BrokenSession())
    response = await upload(client, student="audit-does-not-block")
    assert response["status"] == "ready"


async def test_audit_details_drop_credentials_and_image_payload(test_context) -> None:
    client, fake = test_context
    fake.application.state.services.audit.record(
        "SANITIZE_TEST",
        message="Sanitize details",
        details={
            "password": "secret-password",
            "admin_token": "secret-token",
            "image_base64": "large-image-data",
            "payload": {"value": 1},
            "safe": {"predicted_class": "taohua"},
        },
    )
    event = (await client.get("/events?event_type=SANITIZE_TEST")).json()[0]
    assert event["details"] == {"safe": {"predicted_class": "taohua"}}
