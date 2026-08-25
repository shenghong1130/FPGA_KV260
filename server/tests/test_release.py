from __future__ import annotations

import asyncio

import pytest

from .conftest import create, upload

pytestmark = pytest.mark.asyncio


async def test_release_and_queued_allocation(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client)
    ready = []
    for _ in range(3):
        response = await create(client, "student-a", artifact["artifact_id"])
        assert response.status_code == 201
        ready.append(response.json())
    queued_response = await create(client, "student-a", artifact["artifact_id"])
    assert queued_response.status_code == 202
    queued = queued_response.json()
    assert queued["status"] == "queued"

    release = await client.delete(f"/sessions/{ready[1]['session_id']}")
    assert release.status_code == 200 and release.json()["status"] == "closed"
    for _ in range(100):
        detail = (await client.get(f"/sessions/{queued['session_id']}")).json()
        if detail["status"] == "ready":
            break
        await asyncio.sleep(0.01)
    assert detail["status"] == "ready"
    assert detail["worker"] == ready[1]["worker"]


async def test_release_queued_session(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client)
    for _ in range(3):
        assert (await create(client, "student-a", artifact["artifact_id"])).status_code == 201
    queued = (await create(client, "student-a", artifact["artifact_id"])).json()
    release = await client.delete(f"/sessions/{queued['session_id']}")
    assert release.status_code == 200
    assert release.json()["status"] == "closed"
