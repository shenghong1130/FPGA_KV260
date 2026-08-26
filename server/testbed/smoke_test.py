from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import tempfile
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
            response = await client.get(url)
            if response.is_success:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}")


async def upload_artifact(
    client: httpx.AsyncClient, base_url: str, student: str, project: str
) -> dict[str, Any]:
    bit = (f"fake-bitstream:{student}:{project}").encode()
    hwh = f'<SYSTEM NAME="{project}"><MODULES/></SYSTEM>'.encode()
    response = await client.post(
        f"{base_url}/fpga/artifacts",
        data={"student_id": student},
        files={
            "bit": ("design.bit", io.BytesIO(bit), "application/octet-stream"),
            "hwh": ("design.hwh", io.BytesIO(hwh), "application/xml"),
        },
    )
    response.raise_for_status()
    return response.json()


async def create_session(
    client: httpx.AsyncClient, base_url: str, student: str, artifact_id: str
) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        f"{base_url}/sessions",
        json={"student_id": student, "artifact_id": artifact_id},
    )
    response.raise_for_status()
    return response.status_code, response.json()


async def poll_session(
    client: httpx.AsyncClient, base_url: str, session_id: str, wanted: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = await client.get(f"{base_url}/sessions/{session_id}")
        response.raise_for_status()
        body = response.json()
        if body["status"] == wanted:
            return body
        if body["status"] in {"failed", "lost", "closed"} and wanted != body["status"]:
            raise AssertionError(f"session entered {body['status']}: {body}")
        await asyncio.sleep(0.1)
    raise AssertionError(f"session {session_id} did not reach {wanted}")


async def run(base_url: str, workers_config: Path) -> None:
    workers = json.loads(workers_config.read_text(encoding="utf-8"))
    worker_urls = {item["board"]: item["base_url"] for item in workers}
    unique = str(time.time_ns())
    student_a = f"student-a-{unique}"
    student_b = f"student-b-{unique}"
    created_sessions: list[str] = []

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        await wait_for(client, f"{base_url}/health")
        artifact_a = await upload_artifact(client, base_url, student_a, "flower-a")
        artifact_b = await upload_artifact(client, base_url, student_b, "flower-b")
        assert artifact_a["artifact_id"] != artifact_b["artifact_id"]
        assert artifact_a["status"] == artifact_b["status"] == "ready"
        assert artifact_a["bit_sha256"] and artifact_a["hwh_sha256"]
        passed("Artifact A uploaded")
        passed("Artifact B uploaded")

        code, session_a = await create_session(
            client, base_url, student_a, artifact_a["artifact_id"]
        )
        assert code == 201 and session_a["status"] == "ready"
        created_sessions.append(session_a["session_id"])
        board_a = session_a["worker"]
        assert board_a in worker_urls
        passed(f"Session A allocated to {board_a}")
        mock_status = (await client.get(f"{worker_urls[board_a]}/status")).json()
        deploy_count = mock_status["deploy_count"]

        prediction_boards: list[str] = []
        for index in range(1, 6):
            response = await client.post(
                f"{base_url}/sessions/{session_a['session_id']}/predict",
                json={"payload": {"value": index}},
            )
            response.raise_for_status()
            result = response.json()
            assert result["board"] == board_a
            assert result["session_id"] == session_a["session_id"]
            assert result["artifact_id"] == artifact_a["artifact_id"]
            prediction_boards.append(result["board"])
            passed(f"Predict #{index} -> {result['board']}")
        assert len(set(prediction_boards)) == 1

        concurrent = await asyncio.gather(
            *(
                client.post(
                    f"{base_url}/sessions/{session_a['session_id']}/predict",
                    json={"payload": {"concurrent": index}},
                )
                for index in range(5)
            )
        )
        results = []
        for response in concurrent:
            response.raise_for_status()
            results.append(response.json())
        indexes = sorted(result["predict_index"] for result in results)
        assert len(indexes) == len(set(indexes)) == 5
        mock_status = (await client.get(f"{worker_urls[board_a]}/status")).json()
        assert mock_status["max_concurrent_predicts"] == 1
        assert mock_status["deploy_count"] == deploy_count
        passed("Concurrent predict requests serialized")
        passed("Artifact deployed exactly once for Session A")

        code, session_b = await create_session(
            client, base_url, student_b, artifact_b["artifact_id"]
        )
        assert code == 201 and session_b["status"] == "ready"
        assert session_b["worker"] != board_a
        created_sessions.append(session_b["session_id"])
        passed("Session B allocated to different worker")

        worker_rows = (await client.get(f"{base_url}/workers")).json()
        idle_count = sum(worker["state"] == "idle" for worker in worker_rows)
        filler_sessions: list[dict[str, Any]] = []
        for _ in range(idle_count):
            code, filler = await create_session(
                client, base_url, student_a, artifact_a["artifact_id"]
            )
            assert code == 201 and filler["status"] == "ready"
            filler_sessions.append(filler)
            created_sessions.append(filler["session_id"])
        code, queued = await create_session(
            client, base_url, student_a, artifact_a["artifact_id"]
        )
        assert code == 202 and queued["status"] == "queued"
        created_sessions.append(queued["session_id"])
        passed("All workers occupied -> new Session QUEUED")

        release_target = filler_sessions[0] if filler_sessions else session_b
        response = await client.delete(
            f"{base_url}/sessions/{release_target['session_id']}"
        )
        response.raise_for_status()
        assert response.json()["status"] == "closed"
        queued_ready = await poll_session(
            client, base_url, queued["session_id"], "ready"
        )
        assert queued_ready["worker"] is not None
        passed("Released worker automatically assigned to FIFO queued Session")

        response = await client.delete(f"{base_url}/sessions/{session_a['session_id']}")
        response.raise_for_status()
        assert response.json()["status"] == "closed"
        worker_rows = (await client.get(f"{base_url}/workers")).json()
        released = next(worker for worker in worker_rows if worker["board"] == board_a)
        assert released["state"] == "idle" and released["session_id"] is None
        passed(f"{board_a} returned to IDLE")

        for session_id in created_sessions:
            response = await client.get(f"{base_url}/sessions/{session_id}")
            if response.is_success and response.json()["status"] != "closed":
                await client.delete(f"{base_url}/sessions/{session_id}")

    print("\nALL TESTS PASSED", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Central Scheduler HTTP smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--workers-config",
        type=Path,
        default=Path(os.getenv("WORKERS_CONFIG", "config/workers.mock.json")),
    )
    args = parser.parse_args()
    asyncio.run(run(args.base_url.rstrip("/"), args.workers_config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
