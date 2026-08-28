from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from app.database import Database
from app.db_models import (
    Artifact,
    PredictRequestRecord,
    StudentCredential,
    StudentLease,
)
from .conftest import TEST_PASSWORD, password_headers, predict, upload

pytestmark = pytest.mark.asyncio


async def test_first_upload_registers_hashed_credential(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="student01")

    assert artifact["version"] == "v1"
    with fake.application.state.services.database.sessions() as database:
        credential = database.get(StudentCredential, "student01")
        assert credential is not None
        assert len(credential.password_salt) == 32
        assert len(credential.password_hash) == 64
        assert TEST_PASSWORD.encode() not in credential.password_salt
        assert TEST_PASSWORD.encode() not in credential.password_hash
        assert database.scalar(select(func.count()).select_from(Artifact)) == 1
    database_path = Path(
        fake.application.state.services.database.engine.url.database
    )
    assert TEST_PASSWORD.encode() not in database_path.read_bytes()


async def test_repeat_upload_authenticates_and_wrong_password_has_no_side_effect(
    test_context,
) -> None:
    client, fake = test_context
    await upload(client, student="student01")
    second = await upload(client, student="student01")
    assert second["version"] == "v2"

    before_paths = set(fake.application.state.services.settings.artifact_root.iterdir())
    response = await client.post(
        "/fpga/artifacts",
        data={"student_id": "student01", "password": "wrongpassword"},
        files={
            "bit": ("design.bit", b"must-not-save"),
            "hwh": ("design.hwh", b"<SYSTEM/>")
        },
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid student credentials"}
    with fake.application.state.services.database.sessions() as database:
        artifacts = list(database.scalars(
            select(Artifact).where(Artifact.student_id == "student01")
        ))
        assert len(artifacts) == 2
        assert {item.version for item in artifacts} == {"v1", "v2"}
    assert set(fake.application.state.services.settings.artifact_root.iterdir()) == before_paths


async def test_upload_without_password_is_unauthorized(test_context) -> None:
    client, fake = test_context
    response = await client.post(
        "/fpga/artifacts",
        data={"student_id": "student01"},
        files={
            "bit": ("design.bit", b"bit"),
            "hwh": ("design.hwh", b"<SYSTEM/>")
        },
    )
    assert response.status_code == 401
    with fake.application.state.services.database.sessions() as database:
        assert database.get(StudentCredential, "student01") is None
        assert database.scalar(select(func.count()).select_from(Artifact)) == 0


async def test_predict_authentication_precedes_all_lease_side_effects(test_context) -> None:
    client, fake = test_context
    await upload(client, student="student01")

    wrong = await predict(client, "student01", password="wrongpassword")
    missing = await predict(client, "unregistered")
    assert wrong.status_code == missing.status_code == 401
    with fake.application.state.services.database.sessions() as database:
        assert database.scalar(
            select(func.count()).select_from(PredictRequestRecord)
        ) == 0
        assert database.scalar(select(func.count()).select_from(StudentLease)) == 0
    assert fake.deploy_counts == {}
    assert fake.predict_payloads == []

    correct = await predict(client, "student01", value=123)
    assert correct.status_code == 200
    assert correct.json()["status"] == "completed"
    assert fake.predict_payloads[-1] == {"value": 123}
    with fake.application.state.services.database.sessions() as database:
        lease = database.get(StudentLease, "student01")
        before = (lease.request_count, lease.last_activity_at)
        request_count = database.scalar(
            select(func.count()).select_from(PredictRequestRecord)
        )
    rejected = await predict(client, "student01", password="wrongpassword")
    assert rejected.status_code == 401
    with fake.application.state.services.database.sessions() as database:
        lease = database.get(StudentLease, "student01")
        assert (lease.request_count, lease.last_activity_at) == before
        assert database.scalar(
            select(func.count()).select_from(PredictRequestRecord)
        ) == request_count


async def test_student_cannot_impersonate_another_student(test_context) -> None:
    client, _ = test_context
    await upload(client, student="student-a", password="password-a")
    await upload(client, student="student-b", password="password-b")
    response = await predict(client, "student-a", password="password-b")
    assert response.status_code == 401


async def test_request_and_status_require_owning_student_password(test_context) -> None:
    client, _ = test_context
    await upload(client, student="student-a", password="password-a")
    request = await predict(client, "student-a", password="password-a")
    request_id = request.json()["request_id"]

    assert (await client.get(
        f"/requests/{request_id}", headers=password_headers("password-a")
    )).status_code == 200
    assert (await client.get(
        f"/requests/{request_id}", headers=password_headers("password-b")
    )).status_code == 401
    assert (await client.get(
        "/students/student-a/status", headers=password_headers("password-a")
    )).status_code == 200
    assert (await client.get(
        "/students/student-a/status", headers=password_headers("password-b")
    )).status_code == 401


async def test_change_password_invalidates_old_password_immediately(test_context) -> None:
    client, _ = test_context
    await upload(client, student="student01")
    response = await client.post(
        "/students/student01/password",
        headers=password_headers(),
        json={"new_password": "new-password-123"},
    )
    assert response.status_code == 200
    assert response.json() == {"student_id": "student01", "password_changed": True}
    assert (await predict(client, "student01")).status_code == 401
    assert (await predict(
        client, "student01", password="new-password-123"
    )).status_code == 200


@pytest.mark.parametrize("password", ["short", "x" * 129])
async def test_new_password_length_policy(test_context, password: str) -> None:
    client, _ = test_context
    response = await client.post(
        "/fpga/artifacts",
        data={"student_id": "student01", "password": password},
        files={
            "bit": ("design.bit", b"bit"),
            "hwh": ("design.hwh", b"<SYSTEM/>")
        },
    )
    assert response.status_code == 422


async def test_concurrent_first_registration_has_one_authoritative_password(
    test_context,
) -> None:
    client, fake = test_context

    async def first_upload(password: str, content: bytes):
        return await client.post(
            "/fpga/artifacts",
            data={"student_id": "racing-student", "password": password},
            files={
                "bit": ("design.bit", content),
                "hwh": ("design.hwh", b"<SYSTEM/>")
            },
        )

    responses = await asyncio.gather(
        first_upload("first-password", b"first"),
        first_upload("second-password", b"second"),
    )
    assert sorted(response.status_code for response in responses) == [201, 401]
    winner = "first-password" if responses[0].status_code == 201 else "second-password"
    loser = "second-password" if winner == "first-password" else "first-password"
    with fake.application.state.services.database.sessions() as database:
        assert database.scalar(
            select(func.count()).select_from(StudentCredential).where(
                StudentCredential.student_id == "racing-student"
            )
        ) == 1
        assert database.scalar(
            select(func.count()).select_from(Artifact).where(
                Artifact.student_id == "racing-student"
            )
        ) == 1
    assert (await predict(client, "racing-student", password=loser)).status_code == 401
    assert (await predict(client, "racing-student", password=winner)).status_code == 200


async def test_initialize_adds_credentials_table_without_removing_legacy_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE artifacts (
            id VARCHAR(64) PRIMARY KEY,
            student_id VARCHAR(128) NOT NULL,
            project_name VARCHAR(256) NOT NULL,
            version VARCHAR(128) NOT NULL,
            bit_path TEXT NOT NULL,
            hwh_path TEXT NOT NULL,
            bit_sha256 VARCHAR(64) NOT NULL,
            hwh_sha256 VARCHAR(64) NOT NULL,
            bit_size BIGINT NOT NULL,
            hwh_size BIGINT NOT NULL,
            created_at DATETIME NOT NULL,
            status VARCHAR(32) NOT NULL,
            CONSTRAINT uq_artifacts_student_version UNIQUE (student_id, version)
        )
    """)
    connection.execute("""
        INSERT INTO artifacts VALUES (
            'art_old', 'student01', 'legacy', 'v1', '/old/design.bit',
            '/old/design.hwh', ?, ?, 1, 1, '2026-01-01 00:00:00', 'READY'
        )
    """, ("a" * 64, "b" * 64))
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{path}")
    database.initialize()
    assert "student_credentials" in inspect(database.engine).get_table_names()
    with database.sessions() as session:
        assert session.get(Artifact, "art_old").student_id == "student01"
        assert session.get(StudentCredential, "student01") is None
    database.close()
