from __future__ import annotations

from pathlib import Path

import pytest

from app.db_models import (
    Artifact,
    ArtifactStatus,
    PredictRequestRecord,
    RequestStatus,
    StudentLease,
    Worker,
    utcnow,
)
from .conftest import predict, upload

pytestmark = pytest.mark.asyncio
ADMIN_TOKEN = "cleanup-admin-token"


def enable_admin(fake) -> dict[str, str]:
    object.__setattr__(
        fake.application.state.services.settings, "admin_action_token", ADMIN_TOKEN
    )
    return {"X-Admin-Token": ADMIN_TOKEN}


async def upload_versions(client, count: int = 3) -> list[dict]:
    return [await upload(client, student="cleanup-student", bit=f"bit-{i}".encode()) for i in range(count)]


async def test_cleanup_preview_execute_archives_old_versions_only(test_context) -> None:
    client, fake = test_context
    headers = enable_admin(fake)
    versions = await upload_versions(client)

    preview = await client.get("/admin/artifacts/cleanup-preview", headers=headers)
    assert preview.status_code == 200
    body = preview.json()
    assert body["candidates"] == 2
    assert [item["version"] for item in body["artifacts"]] == ["v1", "v2"]
    assert versions[2]["artifact_id"] not in {item["artifact_id"] for item in body["artifacts"]}

    result = await client.post("/admin/artifacts/cleanup", headers=headers)
    assert result.status_code == 200
    assert result.json()["archived_count"] == 2
    services = fake.application.state.services
    with services.database.sessions() as database:
        assert database.get(Artifact, versions[0]["artifact_id"]).status == ArtifactStatus.ARCHIVED.value
        assert database.get(Artifact, versions[1]["artifact_id"]).status == ArtifactStatus.ARCHIVED.value
        assert database.get(Artifact, versions[2]["artifact_id"]).status == ArtifactStatus.READY.value
    assert not (services.settings.artifact_root / versions[0]["artifact_id"]).exists()
    assert not (services.settings.artifact_root / versions[1]["artifact_id"]).exists()
    assert (services.settings.artifact_root / versions[2]["artifact_id"]).exists()
    next_version = await upload(client, student="cleanup-student", bit=b"bit-next")
    assert next_version["version"] == "v4"
    prediction = await predict(client, "cleanup-student")
    assert prediction.json()["artifact_id"] == next_version["artifact_id"]
    listed = (await client.get("/fpga/artifacts")).json()
    archived = {item["artifact_id"] for item in listed if item["status"] == "archived"}
    assert archived == {versions[0]["artifact_id"], versions[1]["artifact_id"]}

    events = (await client.get("/events?event_type=ARTIFACT_ARCHIVED")).json()
    assert len(events) == 2
    summary = (await client.get("/events?event_type=ADMIN_ARTIFACT_CLEANUP")).json()
    assert summary[0]["details"]["archived_count"] == 2


async def test_active_worker_and_pending_requests_are_protected(test_context) -> None:
    client, fake = test_context
    headers = enable_admin(fake)
    versions = await upload_versions(client, 6)
    services = fake.application.state.services
    with services.database.sessions() as database:
        worker = database.get(Worker, "mock-kv2601")
        worker.current_artifact_id = versions[2]["artifact_id"]
        database.add(StudentLease(
            student_id="lease-protection", state="READY",
            current_artifact_id=versions[3]["artifact_id"],
            created_at=utcnow(), last_activity_at=utcnow(),
        ))
        database.add_all([
            PredictRequestRecord(
                id="req_cleanup_queued", student_id="cleanup-student",
                artifact_id=versions[0]["artifact_id"], artifact_version="v1",
                status=RequestStatus.QUEUED.value, payload={}, created_at=utcnow(),
            ),
            PredictRequestRecord(
                id="req_cleanup_running", student_id="cleanup-student",
                artifact_id=versions[1]["artifact_id"], artifact_version="v2",
                status=RequestStatus.RUNNING.value, payload={}, created_at=utcnow(),
            ),
        ])
        database.commit()

    preview = (await client.get("/admin/artifacts/cleanup-preview", headers=headers)).json()
    assert preview["candidates"] == 1
    assert preview["artifacts"][0]["artifact_id"] == versions[4]["artifact_id"]
    assert (await client.post("/admin/artifacts/cleanup", headers=headers)).json()["archived_count"] == 1
    assert all(
        (services.settings.artifact_root / item["artifact_id"]).exists()
        for index, item in enumerate(versions) if index != 4
    )


async def test_completed_request_metadata_survives_cleanup(test_context) -> None:
    client, fake = test_context
    headers = enable_admin(fake)
    versions = await upload_versions(client)
    services = fake.application.state.services
    with services.database.sessions() as database:
        database.add(PredictRequestRecord(
            id="req_completed_history", student_id="cleanup-student",
            artifact_id=versions[0]["artifact_id"], artifact_version="v1",
            status=RequestStatus.COMPLETED.value, payload={}, result={"ok": True},
            created_at=utcnow(), completed_at=utcnow(),
        ))
        database.commit()

    result = await client.post("/admin/artifacts/cleanup", headers=headers)
    assert result.json()["archived_count"] == 2
    with services.database.sessions() as database:
        artifact = database.get(Artifact, versions[0]["artifact_id"])
        request = database.get(PredictRequestRecord, "req_completed_history")
        assert artifact is not None and artifact.status == ArtifactStatus.ARCHIVED.value
        assert request is not None and request.artifact_id == artifact.id


async def test_execute_recomputes_protection_after_preview(test_context) -> None:
    client, fake = test_context
    headers = enable_admin(fake)
    versions = await upload_versions(client)
    preview = (await client.get("/admin/artifacts/cleanup-preview", headers=headers)).json()
    assert {item["artifact_id"] for item in preview["artifacts"]} == {
        versions[0]["artifact_id"], versions[1]["artifact_id"]
    }

    services = fake.application.state.services
    with services.database.sessions() as database:
        database.get(Worker, "mock-kv2601").current_artifact_id = versions[1]["artifact_id"]
        database.commit()
    result = (await client.post("/admin/artifacts/cleanup", headers=headers)).json()
    assert result["archived_count"] == 1
    with services.database.sessions() as database:
        assert database.get(Artifact, versions[1]["artifact_id"]).status == ArtifactStatus.READY.value


async def test_cleanup_auth_and_missing_directory_are_safe(test_context) -> None:
    client, fake = test_context
    assert (await client.get("/admin/artifacts/cleanup-preview")).status_code == 503
    headers = enable_admin(fake)
    assert (await client.get("/admin/artifacts/cleanup-preview")).status_code == 401
    assert (await client.get(
        "/admin/artifacts/cleanup-preview", headers={"X-Admin-Token": "wrong"}
    )).status_code == 401

    versions = await upload_versions(client, 2)
    directory = fake.application.state.services.settings.artifact_root / versions[0]["artifact_id"]
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()
    result = (await client.post("/admin/artifacts/cleanup", headers=headers)).json()
    assert result == {
        "archived_count": 1, "failed_count": 0, "freed_bytes": 0, "failed": []
    }
    with fake.application.state.services.database.sessions() as database:
        assert database.get(Artifact, versions[0]["artifact_id"]).status == ArtifactStatus.ARCHIVED.value


async def test_one_file_failure_does_not_stop_other_candidates(
    test_context, monkeypatch
) -> None:
    client, fake = test_context
    headers = enable_admin(fake)
    versions = await upload_versions(client)
    service = fake.application.state.services.artifact_cleanup
    original = service._remove_directory

    async def fail_one(artifact_id: str) -> bool:
        if artifact_id == versions[0]["artifact_id"]:
            raise OSError("simulated cleanup failure")
        return await original(artifact_id)

    monkeypatch.setattr(service, "_remove_directory", fail_one)
    result = (await client.post("/admin/artifacts/cleanup", headers=headers)).json()
    assert result["archived_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed"][0]["artifact_id"] == versions[0]["artifact_id"]
    with fake.application.state.services.database.sessions() as database:
        assert database.get(Artifact, versions[0]["artifact_id"]).status == ArtifactStatus.READY.value
        assert database.get(Artifact, versions[1]["artifact_id"]).status == ArtifactStatus.ARCHIVED.value
