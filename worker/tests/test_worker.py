from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio

from worker.app.fpga import FpgaExecutionError, PredictPayloadError
from worker.app.main import create_app, validate_board_hostname


class FakeBackend:
    def __init__(self) -> None:
        self.initialized = False
        self.loaded: list[Path] = []
        self.active_predicts = 0
        self.max_active_predicts = 0
        self.release_count = 0

    async def initialize(self) -> None:
        self.initialized = True

    async def load_overlay(self, bit_path: Path) -> None:
        assert bit_path.name == "design.bit"
        assert bit_path.with_suffix(".hwh").is_file()
        self.loaded.append(bit_path)

    async def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("invalid"):
            raise PredictPayloadError("invalid test image")
        if payload.get("hardware_error"):
            raise FpgaExecutionError("DMA failed")
        self.active_predicts += 1
        self.max_active_predicts = max(self.max_active_predicts, self.active_predicts)
        try:
            await asyncio.sleep(0.02)
            return {"ok": True, "result": payload}
        finally:
            self.active_predicts -= 1

    async def release(self) -> None:
        self.release_count += 1


@pytest_asyncio.fixture
async def worker_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeBackend]]:
    backend = FakeBackend()
    application = create_app(
        board="kv2603", artifact_root=tmp_path / "artifacts", backend=backend
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://worker"
        ) as client:
            yield client, backend


async def deploy(
    client: httpx.AsyncClient,
    lease_id: str = "lease_1",
    artifact_id: str = "art_1",
    bit_data: bytes = b"bitstream",
    hwh_data: bytes = b"<SYSTEM><MODULES/></SYSTEM>",
    bit_sha256: str | None = None,
    hwh_sha256: str | None = None,
) -> httpx.Response:
    return await client.post(
        "/internal/deploy",
        data={
            "lease_id": lease_id,
            "artifact_id": artifact_id,
            "bit_sha256": bit_sha256 or hashlib.sha256(bit_data).hexdigest(),
            "hwh_sha256": hwh_sha256 or hashlib.sha256(hwh_data).hexdigest(),
        },
        files={
            "bit": ("design.bit", bit_data),
            "hwh": ("design.hwh", hwh_data),
        },
    )


@pytest.mark.asyncio
async def test_initial_health_and_status(worker_context) -> None:
    client, backend = worker_context
    assert backend.initialized
    health = await client.get("/health")
    status = await client.get("/status")
    assert health.status_code == 200
    assert health.json() == {"ok": True, "board": "kv2603"}
    assert status.json() == {
        "board": "kv2603",
        "fpga_ready": False,
        "lease_id": None,
        "artifact_id": None,
    }


def test_board_hostname_mapping() -> None:
    assert validate_board_hostname("kv2601") == "kv2601"
    assert validate_board_hostname("kv26020") == "kv26020"
    with pytest.raises(RuntimeError, match="invalid KV260 hostname"):
        validate_board_hostname("kv26021")
    with pytest.raises(RuntimeError, match="invalid KV260 hostname"):
        validate_board_hostname("mock-kv2601")


@pytest.mark.asyncio
async def test_deploy_rejects_sha256_mismatch(worker_context) -> None:
    client, _ = worker_context
    response = await deploy(client, bit_sha256="0" * 64)
    assert response.status_code == 422
    assert "SHA-256 mismatch" in response.json()["detail"]


@pytest.mark.asyncio
async def test_deploy_rejects_invalid_hwh(worker_context) -> None:
    client, _ = worker_context
    response = await deploy(client, hwh_data=b"not-xml")
    assert response.status_code == 422
    assert "invalid HWH XML" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_id", "artifact_id"),
    [("../lease", "art_1"), ("lease_1", "../artifact"), ("lease/1", "art_1")],
)
async def test_deploy_rejects_path_traversal(
    worker_context, lease_id: str, artifact_id: str
) -> None:
    client, _ = worker_context
    response = await deploy(client, lease_id=lease_id, artifact_id=artifact_id)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_conflicting_lease_and_release_ownership(worker_context) -> None:
    client, backend = worker_context
    first_deploy = await deploy(client)
    assert first_deploy.status_code == 200
    assert first_deploy.json() == {
        "ok": True,
        "fpga_ready": True,
        "lease_id": "lease_1",
        "artifact_id": "art_1",
    }
    assert len(backend.loaded) == 1
    conflict = await deploy(client, lease_id="lease_2", artifact_id="art_2")
    assert conflict.status_code == 409
    wrong_release = await client.post(
        "/internal/release", json={"lease_id": "lease_2"}
    )
    assert wrong_release.status_code == 409
    release = await client.post(
        "/internal/release", json={"lease_id": "lease_1"}
    )
    assert release.status_code == 200
    assert release.json() == {"ok": True, "lease_id": "lease_1"}
    assert backend.release_count == 1
    assert (await client.get("/status")).json() == {
        "board": "kv2603",
        "fpga_ready": False,
        "lease_id": None,
        "artifact_id": None,
    }


@pytest.mark.asyncio
async def test_predict_requires_ready_owned_lease(worker_context) -> None:
    client, _ = worker_context
    no_lease = await client.post(
        "/predict", json={"lease_id": "lease_1", "payload": {}}
    )
    assert no_lease.status_code == 409
    assert (await deploy(client)).status_code == 200
    wrong_lease = await client.post(
        "/predict", json={"lease_id": "lease_2", "payload": {}}
    )
    assert wrong_lease.status_code == 409


@pytest.mark.asyncio
async def test_predict_rejects_owned_but_not_ready_worker(tmp_path: Path) -> None:
    backend = FakeBackend()
    application = create_app(
        board="kv2602", artifact_root=tmp_path / "artifacts", backend=backend
    )
    async with application.router.lifespan_context(application):
        application.state.worker.current_lease_id = "lease_1"
        application.state.worker.fpga_ready = False
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://worker"
        ) as client:
            response = await client.post(
                "/predict", json={"lease_id": "lease_1", "payload": {}}
            )
            assert response.status_code == 409


@pytest.mark.asyncio
async def test_bad_predict_payload_returns_422_without_losing_overlay(tmp_path: Path) -> None:
    backend = FakeBackend()
    application = create_app(
        board="kv2602", artifact_root=tmp_path / "artifacts", backend=backend
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://worker"
        ) as client:
            assert (await deploy(client)).status_code == 200
            response = await client.post(
                "/predict", json={"lease_id": "lease_1", "payload": {"invalid": True}}
            )
            assert response.status_code == 422
            assert response.json()["detail"] == "invalid test image"
            assert (await client.get("/status")).json()["fpga_ready"] is True


@pytest.mark.asyncio
async def test_predict_calls_are_serialized(worker_context) -> None:
    client, backend = worker_context
    assert (await deploy(client)).status_code == 200
    first, second = await asyncio.gather(
        client.post("/predict", json={"lease_id": "lease_1", "payload": {"n": 1}}),
        client.post("/predict", json={"lease_id": "lease_1", "payload": {"n": 2}}),
    )
    assert first.status_code == second.status_code == 200
    assert backend.max_active_predicts == 1


@pytest.mark.asyncio
async def test_hardware_predict_failure_returns_500_and_clears_ready(worker_context) -> None:
    client, _ = worker_context
    assert (await deploy(client)).status_code == 200
    response = await client.post(
        "/predict",
        json={"lease_id": "lease_1", "payload": {"hardware_error": True}},
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "DMA failed"
    assert (await client.get("/status")).json()["fpga_ready"] is False
