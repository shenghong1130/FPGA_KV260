from __future__ import annotations

from pathlib import Path

import pytest

from app.db_models import (
    Artifact,
    ArtifactStatus,
    PredictRequestRecord,
    RequestStatus,
    SessionRecord,
    SessionStatus,
    StudentCredential,
    StudentLease,
    Worker,
    utcnow,
)
from .conftest import password_headers, upload

pytestmark = pytest.mark.asyncio
ADMIN_TOKEN = "artifact-delete-admin-token"


def enable_admin(fake) -> dict[str, str]:
    object.__setattr__(
        fake.application.state.services.settings,
        "admin_action_token",
        ADMIN_TOKEN,
    )
    return {"X-Admin-Token": ADMIN_TOKEN}


def artifact_directory(fake, artifact_id: str) -> Path:
    return fake.application.state.services.settings.artifact_root / artifact_id


async def test_delete_artifact_requires_configured_valid_admin_token(
    test_context,
) -> None:
    client, fake = test_context
    artifact = await upload(client, student="delete-auth")
    path = f"/admin/artifacts/{artifact['artifact_id']}"

    assert (await client.delete(path)).status_code == 503
    enable_admin(fake)
    assert (await client.delete(path)).status_code == 401
    assert (
        await client.delete(path, headers={"X-Admin-Token": "wrong-token"})
    ).status_code == 401


async def test_delete_artifact_not_found(test_context) -> None:
    client, fake = test_context
    response = await client.delete(
        "/admin/artifacts/art_00000000000000000000000000000000",
        headers=enable_admin(fake),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "artifact not found"


async def test_admin_deletes_ready_artifact_but_keeps_metadata_and_student(
    test_context,
) -> None:
    client, fake = test_context
    artifact = await upload(client, student="delete-ready", bit=b"delete-me")
    directory = artifact_directory(fake, artifact["artifact_id"])
    assert {item.name for item in directory.iterdir()} == {
        "design.bit",
        "design.hwh",
        "manifest.json",
    }

    response = await client.delete(
        f"/admin/artifacts/{artifact['artifact_id']}",
        headers=enable_admin(fake),
    )
    assert response.status_code == 200
    assert response.json() == {
        "artifact_id": artifact["artifact_id"],
        "student_id": "delete-ready",
        "version": "v1",
        "archived": True,
        "files_deleted": True,
        "freed_bytes": artifact["bit_size"] + artifact["hwh_size"],
    }
    assert not directory.exists()
    services = fake.application.state.services
    with services.database.sessions() as database:
        stored = database.get(Artifact, artifact["artifact_id"])
        credential = database.get(StudentCredential, "delete-ready")
        assert stored is not None and stored.status == ArtifactStatus.ARCHIVED.value
        assert credential is not None

    events = (
        await client.get(
            f"/events?event_type=ARTIFACT_ADMIN_DELETED&artifact_id={artifact['artifact_id']}"
        )
    ).json()
    assert len(events) == 1
    assert events[0]["level"] == "WARNING"
    assert events[0]["actor_type"] == "admin"
    assert events[0]["student_id"] == "delete-ready"
    assert events[0]["details"] == {
        "version": "v1",
        "freed_bytes": artifact["bit_size"] + artifact["hwh_size"],
        "files_missing": False,
    }


async def test_admin_can_delete_latest_and_versions_remain_monotonic(
    test_context,
) -> None:
    client, fake = test_context
    versions = [
        await upload(client, student="delete-latest", bit=f"bit-{index}".encode())
        for index in range(3)
    ]
    response = await client.delete(
        f"/admin/artifacts/{versions[2]['artifact_id']}",
        headers=enable_admin(fake),
    )
    assert response.status_code == 200

    status = await client.get(
        "/students/delete-latest/status",
        headers=password_headers(),
    )
    assert status.json()["latest_artifact_id"] == versions[1]["artifact_id"]
    assert status.json()["latest_version"] == "v2"
    next_artifact = await upload(client, student="delete-latest", bit=b"next")
    assert next_artifact["version"] == "v4"


@pytest.mark.parametrize("protection", ["lease", "worker", "queued", "running"])
async def test_in_use_artifact_cannot_be_deleted(
    test_context, protection: str
) -> None:
    client, fake = test_context
    artifact = await upload(client, student=f"delete-{protection}")
    artifact_id = artifact["artifact_id"]
    services = fake.application.state.services
    with services.database.sessions() as database:
        if protection == "lease":
            database.add(
                StudentLease(
                    student_id=f"delete-{protection}",
                    state="READY",
                    current_artifact_id=artifact_id,
                    created_at=utcnow(),
                    last_activity_at=utcnow(),
                )
            )
        elif protection == "worker":
            database.get(Worker, "mock-kv2601").current_artifact_id = artifact_id
        else:
            database.add(
                PredictRequestRecord(
                    id=f"req_delete_{protection}",
                    student_id=f"delete-{protection}",
                    artifact_id=artifact_id,
                    artifact_version="v1",
                    status=(
                        RequestStatus.QUEUED.value
                        if protection == "queued"
                        else RequestStatus.RUNNING.value
                    ),
                    payload={},
                    created_at=utcnow(),
                )
            )
        database.commit()

    response = await client.delete(
        f"/admin/artifacts/{artifact_id}", headers=enable_admin(fake)
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Artifact is currently in use"
    assert artifact_directory(fake, artifact_id).exists()
    with services.database.sessions() as database:
        assert database.get(Artifact, artifact_id).status == ArtifactStatus.READY.value


async def test_active_legacy_session_protects_artifact(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="delete-active-session")
    services = fake.application.state.services
    with services.database.sessions() as database:
        database.add(
            SessionRecord(
                id="ses_delete_active",
                student_id="delete-active-session",
                artifact_id=artifact["artifact_id"],
                status=SessionStatus.BUSY.value,
                created_at=utcnow(),
                last_activity_at=utcnow(),
            )
        )
        database.commit()

    response = await client.delete(
        f"/admin/artifacts/{artifact['artifact_id']}",
        headers=enable_admin(fake),
    )
    assert response.status_code == 409
    assert artifact_directory(fake, artifact["artifact_id"]).exists()


async def test_completed_request_survives_manual_delete(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="delete-completed")
    services = fake.application.state.services
    with services.database.sessions() as database:
        database.add(
            PredictRequestRecord(
                id="req_delete_completed",
                student_id="delete-completed",
                artifact_id=artifact["artifact_id"],
                artifact_version="v1",
                status=RequestStatus.COMPLETED.value,
                payload={},
                result={"ok": True},
                created_at=utcnow(),
                completed_at=utcnow(),
            )
        )
        database.commit()

    response = await client.delete(
        f"/admin/artifacts/{artifact['artifact_id']}",
        headers=enable_admin(fake),
    )
    assert response.status_code == 200
    with services.database.sessions() as database:
        request = database.get(PredictRequestRecord, "req_delete_completed")
        assert request is not None
        assert request.artifact_id == artifact["artifact_id"]
        assert request.result == {"ok": True}


async def test_manual_delete_file_failure_keeps_ready_and_records_audit(
    test_context, monkeypatch
) -> None:
    client, fake = test_context
    artifact = await upload(client, student="delete-failure")
    service = fake.application.state.services.artifact_cleanup

    async def fail_delete(_artifact_id: str) -> bool:
        raise OSError("simulated manual delete failure")

    monkeypatch.setattr(service, "_remove_directory", fail_delete)
    response = await client.delete(
        f"/admin/artifacts/{artifact['artifact_id']}",
        headers=enable_admin(fake),
    )
    assert response.status_code == 500
    assert "simulated manual delete failure" in response.json()["detail"]
    assert artifact_directory(fake, artifact["artifact_id"]).exists()
    with fake.application.state.services.database.sessions() as database:
        assert (
            database.get(Artifact, artifact["artifact_id"]).status
            == ArtifactStatus.READY.value
        )

    events = (
        await client.get(
            "/events?event_type=ARTIFACT_ADMIN_DELETE_FAILED"
            f"&artifact_id={artifact['artifact_id']}"
        )
    ).json()
    assert len(events) == 1
    assert events[0]["level"] == "ERROR"
    assert events[0]["actor_type"] == "admin"
    assert events[0]["details"]["error"] == "simulated manual delete failure"


async def test_manual_delete_missing_files_archives_with_zero_freed_bytes(
    test_context,
) -> None:
    client, fake = test_context
    artifact = await upload(client, student="delete-missing-files")
    directory = artifact_directory(fake, artifact["artifact_id"])
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()

    response = await client.delete(
        f"/admin/artifacts/{artifact['artifact_id']}",
        headers=enable_admin(fake),
    )
    assert response.status_code == 200
    assert response.json()["files_deleted"] is False
    assert response.json()["freed_bytes"] == 0
