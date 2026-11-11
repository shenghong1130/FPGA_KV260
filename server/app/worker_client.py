from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .db_models import Artifact, Worker


class WorkerClientError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False,
                 retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retryable = retryable
        self.status_code = status_code


def check_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.reason_phrase)
    except (ValueError, AttributeError):
        detail = response.reason_phrase
    detail = str(detail).replace("\r", " ").replace("\n", " ")[:2048]
    # Legacy Workers returned 500 for their deterministic SG rejection.
    permanent = (response.status_code == 422 or "must use Simple DMA" in detail or
                 response.headers.get("X-KV260-Error-Code") == "FPGA_EXECUTION_FAILED")
    raise WorkerClientError(
        f"Worker HTTP {response.status_code}: {detail}", permanent=permanent,
        retryable=response.status_code in {502, 503, 504},
        status_code=response.status_code,
    )


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
            check_response(response)
            payload = response.json()
        except (OSError, httpx.HTTPError, ValueError) as exc:
            raise WorkerClientError(f"worker deploy failed: {exc}",
                                    retryable=isinstance(exc, httpx.TransportError)) from exc
        if not isinstance(payload, dict) or not payload.get("ok") or not payload.get("fpga_ready"):
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
        result = await self._json(
            "POST", f"{worker.base_url}/internal/release",
            json={"lease_id": lease_id},
        )
        if result.get("ok") is not True or result.get("lease_id") != lease_id:
            raise WorkerClientError("worker release acknowledgement identity mismatch")
        return result

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.http.request(method, url, **kwargs)
            check_response(response)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkerClientError(f"{method} {url} failed: {exc}",
                                    retryable=isinstance(exc, httpx.TransportError)) from exc
        if not isinstance(payload, dict):
            raise WorkerClientError(f"{method} {url} returned a non-object response")
        return payload
