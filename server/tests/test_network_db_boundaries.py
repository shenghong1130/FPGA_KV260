from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event

from .conftest import predict, upload

pytestmark = pytest.mark.asyncio


class TransactionCounter:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.active = 0

    def begin(self, *_args) -> None:
        self.active += 1

    def end(self, *_args) -> None:
        self.active -= 1

    def __enter__(self):
        event.listen(self.engine, "begin", self.begin)
        event.listen(self.engine, "commit", self.end)
        event.listen(self.engine, "rollback", self.end)
        return self

    def __exit__(self, *_args) -> None:
        event.remove(self.engine, "begin", self.begin)
        event.remove(self.engine, "commit", self.end)
        event.remove(self.engine, "rollback", self.end)


async def test_deploy_network_wait_has_no_open_database_transaction(test_context) -> None:
    client, fake = test_context
    await upload(client, student="deploy-wait")
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = fake.deploy

    async def blocked_deploy(worker, lease_id, artifact):
        entered.set()
        await resume.wait()
        return await original(worker, lease_id, artifact)

    fake.deploy = blocked_deploy
    engine = fake.application.state.services.database.engine
    with TransactionCounter(engine) as transactions:
        operation = asyncio.create_task(predict(client, "deploy-wait"))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        assert transactions.active == 0
        assert (await asyncio.wait_for(client.get("/health"), timeout=0.25)).status_code == 200
        assert transactions.active == 0
        resume.set()
        assert (await operation).status_code == 200


async def test_predict_network_wait_has_no_open_database_transaction(test_context) -> None:
    client, fake = test_context
    await upload(client, student="predict-wait")
    assert (await predict(client, "predict-wait", 1)).status_code == 200
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = fake.predict

    async def blocked_predict(worker, lease_id, payload):
        entered.set()
        await resume.wait()
        return await original(worker, lease_id, payload)

    fake.predict = blocked_predict
    engine = fake.application.state.services.database.engine
    with TransactionCounter(engine) as transactions:
        operation = asyncio.create_task(predict(client, "predict-wait", 2))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        assert transactions.active == 0
        workers = await asyncio.wait_for(client.get("/workers"), timeout=0.25)
        assert workers.status_code == 200
        assert transactions.active == 0
        resume.set()
        assert (await operation).status_code == 200
