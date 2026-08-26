from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx


def passed(message: str) -> None:
    print(f"[PASS] {message}", flush=True)


async def wait_for(client: httpx.AsyncClient, url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await client.get(url)).is_success:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}")


async def upload(client: httpx.AsyncClient, base: str, student: str,
                 marker: str) -> dict[str, Any]:
    bit = f"fake-bit:{student}:{marker}".encode()
    hwh = f'<SYSTEM NAME="{marker}"><MODULES/></SYSTEM>'.encode()
    response = await client.post(f"{base}/fpga/artifacts",
        data={"student_id": student}, files={
            "bit": ("design.bit", io.BytesIO(bit), "application/octet-stream"),
            "hwh": ("design.hwh", io.BytesIO(hwh), "application/xml"),
        })
    response.raise_for_status()
    return response.json()


async def predict(client: httpx.AsyncClient, base: str, student: str,
                  value: Any) -> httpx.Response:
    return await client.post(f"{base}/predict", json={
        "student_id": student, "payload": {"value": value},
    })


async def poll(client: httpx.AsyncClient, base: str, request_id: str,
               timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"{base}/requests/{request_id}")
        response.raise_for_status()
        body = response.json()
        if body["status"] in {"completed", "failed"}:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"request {request_id} did not finish")


async def run(base: str, workers_config: Path) -> None:
    worker_urls = {item["board"]: item["base_url"] for item in
                   json.loads(workers_config.read_text(encoding="utf-8"))}
    suffix = str(time.time_ns())
    students = [f"student-{name}-{suffix}" for name in "abcd"]
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        await wait_for(client, f"{base}/health")
        artifacts = [await upload(client, base, student, "v1") for student in students]
        assert all(item["version"] == "v1" for item in artifacts)
        passed("Artifacts uploaded with server-generated versions")

        first = await predict(client, base, students[0], 1)
        first.raise_for_status()
        assert first.status_code == 200 and first.json()["status"] == "completed"
        workers = (await client.get(f"{base}/workers")).json()
        board = next(row["board"] for row in workers if row["student_id"] == students[0])
        deploy_count = (await client.get(f"{worker_urls[board]}/status")).json()["deploy_count"]
        for value in range(2, 6):
            response = await predict(client, base, students[0], value)
            response.raise_for_status()
            assert response.json()["status"] == "completed"
        status = (await client.get(f"{worker_urls[board]}/status")).json()
        assert status["deploy_count"] == deploy_count
        assert status["max_concurrent_predicts"] == 1
        passed("Fixed Worker reused; Artifact deployed once")

        for student in students[1:3]:
            response = await predict(client, base, student, 1)
            response.raise_for_status()
        queued = await predict(client, base, students[3], 1)
        assert queued.status_code == 202 and queued.json()["status"] == "queued"
        passed("No IDLE Worker -> persistent request queued")

        completed = await poll(client, base, queued.json()["request_id"])
        assert completed["status"] == "completed"
        passed("LRU reclaim allocated Worker and completed queued request")

        assert (await client.post(f"{base}/sessions", json={})).status_code == 404
        health = (await client.get(f"{base}/health")).json()
        assert "leases" in health and "requests" in health and "sessions" not in health
        passed("Sessionless API and Lease/Request health report verified")
    print("\nALL TESTS PASSED", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Central lease/request HTTP smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workers-config", type=Path,
                        default=Path(os.getenv("WORKERS_CONFIG", "config/workers.mock.json")))
    args = parser.parse_args()
    asyncio.run(run(args.base_url.rstrip("/"), args.workers_config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
