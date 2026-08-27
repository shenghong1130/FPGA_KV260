from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest_asyncio

from app.config import Settings
from app.db_models import Artifact, Worker
from app.main import create_app
from app.worker_client import WorkerClientError


class FakeWorkerClient:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.deploy_counts: dict[str, int] = {}
        self.active_predicts: dict[str, int] = {}
        self.max_active_predicts: dict[str, int] = {}
        self.fail_predict_for: set[str] = set()
        self.predict_payloads: list[dict[str, Any]] = []
        self.application: Any = None

    async def close(self) -> None:
        return None

    def _state(self, board: str) -> dict[str, Any]:
        return self.states.setdefault(
            board, {"lease_id": None, "artifact_id": None, "predict_count": 0}
        )

    async def health(self, worker: Worker) -> dict[str, Any]:
        return {"ok": True}

    async def status(self, worker: Worker) -> dict[str, Any]:
        state = self._state(worker.board)
        return {
            "board": worker.board,
            "fpga_ready": state["lease_id"] is not None,
            **state,
        }

    async def deploy(
        self, worker: Worker, lease_id: str, artifact: Artifact
    ) -> dict[str, Any]:
        state = self._state(worker.board)
        if state["lease_id"] not in (None, lease_id):
            raise WorkerClientError("already leased")
        state.update(
            lease_id=lease_id, artifact_id=artifact.id, predict_count=0
        )
        self.deploy_counts[lease_id] = self.deploy_counts.get(lease_id, 0) + 1
        await asyncio.sleep(0.005)
        return {
            "ok": True,
            "fpga_ready": True,
            "lease_id": lease_id,
            "artifact_id": artifact.id,
        }

    async def predict(
        self, worker: Worker, lease_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.predict_payloads.append(payload)
        state = self._state(worker.board)
        if state["lease_id"] != lease_id:
            raise WorkerClientError("wrong lease")
        if lease_id in self.fail_predict_for:
            raise WorkerClientError("simulated worker failure")
        self.active_predicts[lease_id] = self.active_predicts.get(lease_id, 0) + 1
        self.max_active_predicts[lease_id] = max(
            self.max_active_predicts.get(lease_id, 0),
            self.active_predicts[lease_id],
        )
        try:
            await asyncio.sleep(0.02)
            state["predict_count"] += 1
            if "image_base64" in payload:
                return {
                    "ok": True,
                    "status": "success",
                    "predicted_class": "bailianhua",
                    "flower": "bailianhua",
                    "flower_api": "bailianhua",
                    "flower_cn": "白莲花",
                    "raw_class": "白莲花",
                    "class_index": 0,
                    "confidence": 0.75,
                }
            return {
                "ok": True,
                "board": worker.board,
                "lease_id": lease_id,
                "artifact_id": state["artifact_id"],
                "predict_index": state["predict_count"],
                "input": payload,
            }
        finally:
            self.active_predicts[lease_id] -= 1

    async def release(self, worker: Worker, lease_id: str) -> dict[str, Any]:
        state = self._state(worker.board)
        if state["lease_id"] != lease_id:
            raise WorkerClientError("wrong lease")
        state["lease_id"] = None
        state["artifact_id"] = None
        return {"ok": True, "lease_id": lease_id}


@pytest_asyncio.fixture
async def test_context(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, FakeWorkerClient]]:
    config = tmp_path / "workers.json"
    config.write_text(
        json.dumps(
            [
                {"board": f"mock-kv260{index}", "base_url": f"http://mock{index}"}
                for index in range(1, 4)
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        base_dir=tmp_path,
        server_host="127.0.0.1",
        server_port=8000,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        artifact_root=tmp_path / "artifacts",
        workers_config=config,
        worker_connect_timeout=0.1,
        worker_request_timeout=1,
        worker_deploy_timeout=1,
        health_interval_seconds=3600,
        health_failure_threshold=3,
        lease_idle_timeout_seconds=3600,
        lease_reclaim_grace_seconds=3600,
        lease_reaper_interval_seconds=3600,
        max_bit_size=1024 * 1024,
        max_hwh_size=1024 * 1024,
        max_predict_image_size=64,
    )
    fake = FakeWorkerClient()
    application = create_app(settings, fake)  # type: ignore[arg-type]
    fake.application = application
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, fake


async def upload(
    client: httpx.AsyncClient,
    student: str = "student-a",
    bit: bytes = b"fake-bit",
    hwh: bytes = b"<SYSTEM><MODULES/></SYSTEM>",
    bit_name: str = "design.bit",
    hwh_name: str = "design.hwh",
) -> dict[str, Any]:
    response = await client.post(
        "/fpga/artifacts",
        data={"student_id": student},
        files={"bit": (bit_name, bit), "hwh": (hwh_name, hwh)},
    )
    response.raise_for_status()
    result = response.json()
    if response.status_code == 201:
        assert result["bit_sha256"] == hashlib.sha256(bit).hexdigest()
        assert result["hwh_sha256"] == hashlib.sha256(hwh).hexdigest()
    return result


async def predict(client: httpx.AsyncClient, student: str,
                  value: Any = 1) -> httpx.Response:
    return await client.post(
        "/predict", json={"student_id": student, "payload": {"value": value}}
    )
