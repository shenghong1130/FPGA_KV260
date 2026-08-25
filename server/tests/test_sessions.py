from __future__ import annotations

import pytest

from .conftest import create, upload

pytestmark = pytest.mark.asyncio


async def test_session_fixed_worker_and_deploy_once(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client)
    response = await create(client, "student-a", artifact["artifact_id"])
    assert response.status_code == 201
    session = response.json()
    board = session["worker"]
    for value in range(100):
        prediction = await client.post(
            f"/sessions/{session['session_id']}/predict",
            json={"payload": {"value": value}},
        )
        assert prediction.status_code == 200
        body = prediction.json()
        assert body["board"] == board
        assert body["session_id"] == session["session_id"]
        assert body["artifact_id"] == artifact["artifact_id"]
    assert fake.deploy_counts[session["session_id"]] == 1
    detail = (await client.get(f"/sessions/{session['session_id']}")).json()
    assert detail["request_count"] == 100


async def test_artifact_ownership(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client, student="owner")
    response = await create(client, "not-owner", artifact["artifact_id"])
    assert response.status_code == 403


async def test_predict_failure_does_not_migrate_session(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client)
    session = (await create(client, "student-a", artifact["artifact_id"])).json()
    original_worker = session["worker"]
    fake.fail_predict_for.add(session["session_id"])

    response = await client.post(
        f"/sessions/{session['session_id']}/predict", json={"payload": {"value": 1}}
    )
    assert response.status_code == 502
    detail = (await client.get(f"/sessions/{session['session_id']}")).json()
    assert detail["status"] == "lost"
    assert detail["worker"] == original_worker
    workers = (await client.get("/workers")).json()
    failed_worker = next(worker for worker in workers if worker["board"] == original_worker)
    assert failed_worker["state"] == "error"
