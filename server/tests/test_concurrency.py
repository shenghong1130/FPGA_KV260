from __future__ import annotations

import asyncio

import pytest

from .conftest import create, upload

pytestmark = pytest.mark.asyncio


async def test_concurrent_session_creation_has_no_double_allocation(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client)
    responses = await asyncio.gather(
        *(create(client, "student-a", artifact["artifact_id"]) for _ in range(4))
    )
    ready = [response.json() for response in responses if response.status_code == 201]
    queued = [response.json() for response in responses if response.status_code == 202]
    assert len(ready) == 3
    assert len({session["worker"] for session in ready}) == 3
    assert len(queued) == 1 and queued[0]["status"] == "queued"


async def test_same_session_predicts_are_serialized(test_context) -> None:
    client, fake = test_context
    artifact = await upload(client)
    session = (await create(client, "student-a", artifact["artifact_id"])).json()
    responses = await asyncio.gather(
        *(
            client.post(
                f"/sessions/{session['session_id']}/predict",
                json={"payload": {"index": index}},
            )
            for index in range(10)
        )
    )
    assert all(response.status_code == 200 for response in responses)
    indexes = sorted(response.json()["predict_index"] for response in responses)
    assert indexes == list(range(1, 11))
    assert fake.max_active_predicts[session["session_id"]] == 1
