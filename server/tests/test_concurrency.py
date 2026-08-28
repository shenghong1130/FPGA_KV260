from __future__ import annotations

import asyncio
import pytest

from .conftest import password_headers, predict, upload

pytestmark = pytest.mark.asyncio


async def test_same_student_first_predicts_use_one_worker_serially(test_context) -> None:
    client, fake = test_context
    await upload(client, student="student-a")
    responses = await asyncio.gather(*(predict(client, "student-a", i) for i in range(10)))
    assert all(response.status_code == 200 for response in responses)
    assert len([row for row in (await client.get("/workers")).json()
                if row["student_id"] == "student-a"]) == 1
    assert len(fake.deploy_counts) == 1
    lease_id = next(iter(fake.deploy_counts))
    assert fake.max_active_predicts[lease_id] == 1
    assert sorted(r.json()["result"]["predict_index"] for r in responses) == list(range(1, 11))


async def test_different_students_get_distinct_workers(test_context) -> None:
    client, _ = test_context
    for student in ("a", "b", "c"):
        await upload(client, student=student)
    responses = await asyncio.gather(*(predict(client, student) for student in ("a", "b", "c")))
    workers = (await client.get("/workers")).json()
    assert len({row["board"] for row in workers if row["student_id"] in {"a", "b", "c"}}) == 3


async def test_student_status_hides_worker_identity(test_context) -> None:
    client, _ = test_context
    await upload(client, student="student-a")
    await predict(client, "student-a")
    status = (await client.get(
        "/students/student-a/status", headers=password_headers()
    )).json()
    assert status["lease_state"] == "ready" and status["worker_assigned"] is True
    assert "worker" not in status and "lease_id" not in status
