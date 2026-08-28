from __future__ import annotations

import pytest

from .conftest import predict, upload

pytestmark = pytest.mark.asyncio


async def test_first_predict_allocates_and_deploys(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client, student="student-a")
    response = await predict(client, "student-a", 1)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "completed"
    assert body["artifact_id"] == artifact["artifact_id"] and body["version"] == "v1"
    assert "board" not in body["result"] and "lease_id" not in body["result"]
    assert len(fake.deploy_counts) == 1


async def test_same_student_fixed_worker_and_deploy_once(test_context) -> None:
    client, fake = test_context
    await upload(client, student="student-a")
    for value in range(100):
        assert (await predict(client, "student-a", value)).status_code == 200
    owned = [row for row in (await client.get("/workers")).json()
             if row["student_id"] == "student-a"]
    assert len(owned) == 1
    assert list(fake.deploy_counts.values()) == [1]


async def test_new_artifact_redeploys_on_same_worker(test_context) -> None:
    client, fake = test_context
    first = await upload(client, student="student-a", bit=b"v1")
    result1 = (await predict(client, "student-a", 1)).json()
    second = await upload(client, student="student-a", bit=b"v2")
    result2 = (await predict(client, "student-a", 2)).json()
    assert first["version"] == "v1" and second["version"] == "v2"
    assert result2["artifact_id"] == second["artifact_id"]
    assert list(fake.deploy_counts.values()) == [2]


async def test_unregistered_student_is_unauthorized_and_missing_request_is_404(
    test_context,
) -> None:
    client, _ = test_context
    assert (await predict(client, "missing")).status_code == 401
    assert (await client.get("/requests/req_missing")).status_code == 404


async def test_old_session_api_removed(test_context) -> None:
    client, _ = test_context
    assert (await client.post("/sessions", json={})).status_code == 404
    assert (await client.get("/sessions/old")).status_code == 404
