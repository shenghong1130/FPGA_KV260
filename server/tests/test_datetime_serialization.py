from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.datetime_utils import ensure_utc
from app.db_models import Artifact, LeaseStatus, StudentLease, Worker
from app.schemas import ArtifactResponse, StudentStatusResponse, WorkerResponse
from .conftest import password_headers

NAIVE_UTC = datetime(2026, 8, 27, 9, 53, 7, 420198)


def assert_utc_iso(value: str, expected: datetime = NAIVE_UTC) -> None:
    assert value.endswith(("Z", "+00:00"))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed == expected.replace(tzinfo=timezone.utc)
    assert parsed.utcoffset() == timedelta(0)


def test_ensure_utc_handles_naive_aware_non_utc_and_none() -> None:
    assert ensure_utc(None) is None
    assert ensure_utc(NAIVE_UTC) == NAIVE_UTC.replace(tzinfo=timezone.utc)

    aware_utc = NAIVE_UTC.replace(tzinfo=timezone.utc)
    assert ensure_utc(aware_utc) == aware_utc

    utc_plus_eight = datetime(
        2026, 8, 27, 17, 53, 7, 420198,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert ensure_utc(utc_plus_eight) == aware_utc


@pytest.mark.parametrize(
    ("response", "field"),
    [
        (
            ArtifactResponse(
                artifact_id="art_utc",
                student_id="student-utc",
                version="v1",
                status="ready",
                bit_sha256="a" * 64,
                hwh_sha256="b" * 64,
                bit_size=1,
                hwh_size=1,
                created_at=NAIVE_UTC,
            ),
            "created_at",
        ),
        (
            WorkerResponse(
                board="kv2601",
                state="idle",
                lease_id=None,
                student_id=None,
                artifact_id=None,
                fpga_ready=False,
                last_seen=NAIVE_UTC,
                last_error=None,
            ),
            "last_seen",
        ),
        (
            StudentStatusResponse(
                student_id="student-utc",
                latest_artifact_id=None,
                latest_version=None,
                lease_state="unassigned",
                worker_assigned=False,
                queued_requests=0,
                running_requests=0,
                last_activity_at=NAIVE_UTC,
            ),
            "last_activity_at",
        ),
    ],
)
def test_public_response_datetime_json_is_explicit_utc(response, field: str) -> None:
    assert_utc_iso(response.model_dump(mode="json")[field])


def test_optional_response_datetimes_remain_null() -> None:
    worker = WorkerResponse(
        board="kv2601",
        state="offline",
        lease_id=None,
        student_id=None,
        artifact_id=None,
        fpga_ready=False,
        last_seen=None,
        last_error=None,
    )
    assert worker.model_dump(mode="json")["last_seen"] is None


@pytest.mark.parametrize(
    "value",
    [
        NAIVE_UTC.replace(tzinfo=timezone.utc),
        datetime(
            2026, 8, 27, 17, 53, 7, 420198,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    ],
)
def test_aware_response_datetimes_serialize_as_utc(value: datetime) -> None:
    response = WorkerResponse(
        board="kv2601",
        state="idle",
        lease_id=None,
        student_id=None,
        artifact_id=None,
        fpga_ready=False,
        last_seen=value,
        last_error=None,
    )
    assert_utc_iso(response.model_dump(mode="json")["last_seen"])


@pytest.mark.asyncio
async def test_sqlite_datetime_api_responses_are_explicit_utc(test_context) -> None:
    client, fake = test_context
    sessions = fake.application.state.services.database.sessions
    with sessions() as database:
        worker = database.get(Worker, "mock-kv2601")
        worker.last_seen = NAIVE_UTC
        database.add(
            Artifact(
                id="art_utc",
                student_id="student-utc",
                legacy_project_name="legacy",
                version="v1",
                bit_path="/tmp/design.bit",
                hwh_path="/tmp/design.hwh",
                bit_sha256="a" * 64,
                hwh_sha256="b" * 64,
                bit_size=1,
                hwh_size=1,
                created_at=NAIVE_UTC,
                status="READY",
            )
        )
        database.add(
            StudentLease(
                student_id="student-utc",
                state=LeaseStatus.UNASSIGNED.value,
                created_at=NAIVE_UTC,
                last_activity_at=NAIVE_UTC,
            )
        )
        database.commit()

    workers = (await client.get("/workers")).json()
    worker = next(item for item in workers if item["board"] == "mock-kv2601")
    assert_utc_iso(worker["last_seen"])

    artifact_response = await client.get("/fpga/artifacts/art_utc")
    assert artifact_response.status_code == 200
    assert_utc_iso(artifact_response.json()["created_at"])

    await fake.application.state.services.student_auth.authenticate_or_register(
        "student-utc", "correct123"
    )
    student_response = await client.get(
        "/students/student-utc/status", headers=password_headers()
    )
    assert student_response.status_code == 200
    assert_utc_iso(student_response.json()["last_activity_at"])
