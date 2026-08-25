from __future__ import annotations

import pytest

from .conftest import create, upload

pytestmark = pytest.mark.asyncio


async def test_ready_and_busy_workers_are_not_reallocated(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client)
    sessions = []
    for _ in range(3):
        response = await create(client, "student-a", artifact["artifact_id"])
        assert response.status_code == 201
        sessions.append(response.json())
    assert len({session["worker"] for session in sessions}) == 3
    workers = (await client.get("/workers")).json()
    assert all(worker["state"] == "ready" for worker in workers)
    queued = await create(client, "student-a", artifact["artifact_id"])
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
