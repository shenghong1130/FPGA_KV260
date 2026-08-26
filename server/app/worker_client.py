from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .db_models import Artifact, Worker


class WorkerClientError(RuntimeError):
    pass


class WorkerClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(
                settings.worker_request_timeout,
                connect=settings.worker_connect_timeout,
            )
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def health(self, worker: Worker) -> dict[str, Any]:
        return await self._json("GET", f"{worker.base_url}/health")

    async def status(self, worker: Worker) -> dict[str, Any]:
        return await self._json("GET", f"{worker.base_url}/status")

    async def deploy(
        self, worker: Worker, lease_id: str, artifact: Artifact
    ) -> dict[str, Any]:
        try:
            with Path(artifact.bit_path).open("rb") as bit_file, Path(
                artifact.hwh_path
            ).open("rb") as hwh_file:
                response = await self.http.post(
                    f"{worker.base_url}/internal/deploy",
                    data={
                        "lease_id": lease_id,
                        "artifact_id": artifact.id,
                        "bit_sha256": artifact.bit_sha256,
                        "hwh_sha256": artifact.hwh_sha256,
                    },
                    files={
                        "bit": ("design.bit", bit_file, "application/octet-stream"),
                        "hwh": ("design.hwh", hwh_file, "application/xml"),
                    },
                    timeout=httpx.Timeout(
                        self.settings.worker_deploy_timeout,
                        connect=self.settings.worker_connect_timeout,
                    ),
                )
            response.raise_for_status()
            payload = response.json()
        except (OSError, httpx.HTTPError, ValueError) as exc:
            raise WorkerClientError(f"worker deploy failed: {exc}") from exc
        if not payload.get("ok") or not payload.get("fpga_ready"):
            raise WorkerClientError(f"worker rejected deployment: {payload}")
        if payload.get("lease_id") != lease_id or payload.get("artifact_id") != artifact.id:
            raise WorkerClientError("worker deployment identity mismatch")
        return payload

    async def predict(
        self, worker: Worker, lease_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"{worker.base_url}/predict",
            json={"lease_id": lease_id, "payload": payload},
        )

    async def release(self, worker: Worker, lease_id: str) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"{worker.base_url}/internal/release",
            json={"lease_id": lease_id},
        )

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.http.request(method, url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkerClientError(f"{method} {url} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise WorkerClientError(f"{method} {url} returned a non-object response")
        return payload
