from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


STUDENTS: tuple[tuple[str, str], ...] = tuple(
    (f"persist{index:02d}", f"persistpass{index:02d}")
    for index in range(1, 6)
)
STATE_PATH = Path(__file__).with_name("multi_student_test_state.json")
TERMINAL_STATUSES = {"completed", "failed"}
EXPECTED_REQUEST_EVENTS = {
    "REQUEST_CREATED",
    "REQUEST_STARTED",
    "REQUEST_COMPLETED",
}


class TestFailure(RuntimeError):
    __test__ = False

    pass


@dataclass
class StudentRun:
    student_id: str
    password: str
    artifact_id: str
    version: str
    bit_sha256: str
    hwh_sha256: str
    request_id: str = ""
    initial_status: str = ""
    worker: str | None = None
    final: dict[str, Any] | None = None


def banner(title: str, width: int = 64) -> None:
    print(f"\n{'=' * width}\n{title}\n{'=' * width}", flush=True)


def response_problem(student_id: str, operation: str, response: httpx.Response) -> str:
    body = response.text.strip() or "<empty response body>"
    return (
        f"[{operation}] {student_id} HTTP {response.status_code}\n"
        f"  response: {body}"
    )


def require_fields(body: dict[str, Any], fields: set[str], context: str) -> None:
    missing = sorted(fields.difference(body))
    if missing:
        raise TestFailure(f"{context}: response missing fields: {', '.join(missing)}")


async def phase0_health(client: httpx.AsyncClient, server: str) -> None:
    banner("KV260 Multi-Student Persistence Test", 60)
    print(
        f"\nCentral Server : {server}\n"
        f"Students       : {len(STUDENTS)}\n"
        "Artifact       : SAME bit/hwh for all students\n"
        "Image          : SAME jpg for all students\n",
        flush=True,
    )
    try:
        response = await client.get(f"{server}/health")
    except httpx.HTTPError as exc:
        raise TestFailure(f"Central Server OFFLINE: GET {server}/health failed: {exc}") from exc
    if response.status_code != 200:
        raise TestFailure(response_problem("central", "HEALTH", response))
    print("Central Server ONLINE", flush=True)


async def upload_one(
    client: httpx.AsyncClient,
    server: str,
    student_id: str,
    password: str,
    bit_bytes: bytes,
    hwh_bytes: bytes,
) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{server}/fpga/artifacts",
            data={"student_id": student_id, "password": password},
            files={
                "bit": ("design.bit", bit_bytes, "application/octet-stream"),
                "hwh": ("design.hwh", hwh_bytes, "application/xml"),
            },
        )
    except httpx.HTTPError as exc:
        raise TestFailure(f"[UPLOAD] {student_id} HTTP request failed: {exc}") from exc
    if response.status_code != 201:
        raise TestFailure(response_problem(student_id, "UPLOAD", response))
    try:
        body = response.json()
    except ValueError as exc:
        raise TestFailure(response_problem(student_id, "UPLOAD", response)) from exc
    require_fields(
        body,
        {"artifact_id", "student_id", "version", "bit_sha256", "hwh_sha256"},
        f"[UPLOAD] {student_id}",
    )
    return body


def newest_artifacts(
    artifacts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        student_id = artifact.get("student_id")
        if student_id not in dict(STUDENTS) or artifact.get("status") != "ready":
            continue
        current = selected.get(student_id)
        version = str(artifact.get("version", ""))
        try:
            version_number = int(version.removeprefix("v"))
            current_number = int(str(current["version"]).removeprefix("v")) if current else -1
        except ValueError:
            version_number = current_number = -1
        if current is None or version_number > current_number or (
            version_number == current_number
            and str(artifact.get("created_at", "")) > str(current.get("created_at", ""))
        ):
            selected[student_id] = artifact
    return selected


async def phase1_artifacts(
    client: httpx.AsyncClient,
    server: str,
    bit_bytes: bytes,
    hwh_bytes: bytes,
    skip_upload: bool,
) -> list[StudentRun]:
    banner("PHASE 1 - SAME Artifact -> 5 Students")
    if skip_upload:
        response = await client.get(f"{server}/fpga/artifacts")
        if response.status_code != 200:
            raise TestFailure(response_problem("all", "ARTIFACT LIST", response))
        try:
            selected = newest_artifacts(response.json())
        except (TypeError, ValueError) as exc:
            raise TestFailure("[ARTIFACT LIST] invalid JSON response") from exc
        missing = [student_id for student_id, _ in STUDENTS if student_id not in selected]
        if missing:
            raise TestFailure(
                "--skip-upload requested, but no ready Artifact exists for: "
                + ", ".join(missing)
            )
        bodies = [selected[student_id] for student_id, _ in STUDENTS]
        print("Using each student's latest existing ready Artifact.", flush=True)
    else:
        results = await asyncio.gather(
            *(
                upload_one(
                    client, server, student_id, password, bit_bytes, hwh_bytes
                )
                for student_id, password in STUDENTS
            ),
            return_exceptions=True,
        )
        errors = [str(result) for result in results if isinstance(result, Exception)]
        if errors:
            raise TestFailure("Concurrent Artifact upload failed:\n" + "\n".join(errors))
        bodies = [result for result in results if isinstance(result, dict)]

    runs: list[StudentRun] = []
    for (student_id, password), body in zip(STUDENTS, bodies, strict=True):
        if body.get("student_id") != student_id:
            raise TestFailure(
                f"[UPLOAD] {student_id}: response student_id={body.get('student_id')!r}"
            )
        run = StudentRun(
            student_id=student_id,
            password=password,
            artifact_id=str(body["artifact_id"]),
            version=str(body["version"]),
            bit_sha256=str(body["bit_sha256"]),
            hwh_sha256=str(body["hwh_sha256"]),
        )
        runs.append(run)
        print(
            f"[UPLOAD] {student_id:<10} {run.version:<5} {run.artifact_id}",
            flush=True,
        )

    artifact_ids = {run.artifact_id for run in runs}
    bit_hashes = {run.bit_sha256 for run in runs}
    hwh_hashes = {run.hwh_sha256 for run in runs}
    expected_bit_hash = hashlib.sha256(bit_bytes).hexdigest()
    expected_hwh_hash = hashlib.sha256(hwh_bytes).hexdigest()
    if len(artifact_ids) != len(runs):
        raise TestFailure("Artifact ID validation failed: IDs are not unique")
    if bit_hashes != {expected_bit_hash}:
        raise TestFailure(f"BIT SHA256 mismatch: server values={sorted(bit_hashes)}")
    if hwh_hashes != {expected_hwh_hash}:
        raise TestFailure(f"HWH SHA256 mismatch: server values={sorted(hwh_hashes)}")
    print("\nBIT SHA256 : SAME OK", flush=True)
    print("HWH SHA256 : SAME OK", flush=True)
    print("Artifact ID: UNIQUE OK", flush=True)
    return runs


async def submit_one(
    client: httpx.AsyncClient,
    server: str,
    run: StudentRun,
    image_bytes: bytes,
    start_event: asyncio.Event,
) -> tuple[int, dict[str, Any]]:
    await start_event.wait()
    try:
        response = await client.post(
            f"{server}/predict",
            headers={"X-Student-Password": run.password},
            data={"student_id": run.student_id},
            files={"image": ("flower.jpg", image_bytes, "image/jpeg")},
        )
    except httpx.HTTPError as exc:
        raise TestFailure(f"[SUBMIT] {run.student_id} HTTP request failed: {exc}") from exc
    if response.status_code not in {200, 202}:
        raise TestFailure(response_problem(run.student_id, "SUBMIT", response))
    try:
        body = response.json()
    except ValueError as exc:
        raise TestFailure(response_problem(run.student_id, "SUBMIT", response)) from exc
    require_fields(
        body,
        {"student_id", "request_id", "artifact_id", "version", "status", "worker"},
        f"[SUBMIT] {run.student_id}",
    )
    if body["status"] not in {"queued", "running", "completed", "failed"}:
        raise TestFailure(f"[SUBMIT] {run.student_id}: unexpected status {body['status']!r}")
    if response.status_code == 202 and body["status"] != "queued":
        raise TestFailure(f"[SUBMIT] {run.student_id}: HTTP 202 status is not queued")
    return response.status_code, body


def validate_request_identity(run: StudentRun, body: dict[str, Any]) -> None:
    expected = {
        "student_id": run.student_id,
        "request_id": run.request_id,
        "artifact_id": run.artifact_id,
        "version": run.version,
    }
    wrong = [
        f"{key}: expected {value!r}, got {body.get(key)!r}"
        for key, value in expected.items()
        if body.get(key) != value
    ]
    if wrong:
        raise TestFailure(f"request ownership mismatch for {run.student_id}: " + "; ".join(wrong))


def save_state(runs: list[StudentRun]) -> None:
    state = [
        {
            "student_id": run.student_id,
            "request_id": run.request_id,
            "artifact_id": run.artifact_id,
            "version": run.version,
            "initial_status": run.initial_status,
            "worker": run.worker,
            "bit_sha256": run.bit_sha256,
            "hwh_sha256": run.hwh_sha256,
        }
        for run in runs
    ]
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nSaved original request IDs: {STATE_PATH}", flush=True)


async def phase2_submit(
    client: httpx.AsyncClient,
    server: str,
    runs: list[StudentRun],
    image_bytes: bytes,
) -> None:
    banner("PHASE 2 - 5 Simultaneous Predict Requests")
    start_event = asyncio.Event()
    tasks = [
        asyncio.create_task(submit_one(client, server, run, image_bytes, start_event))
        for run in runs
    ]
    print("\nFive tasks are waiting at the start barrier...", flush=True)
    await asyncio.sleep(0.5)
    print("Releasing 5 HTTP requests simultaneously...\n", flush=True)
    start_event.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [str(result) for result in results if isinstance(result, Exception)]
    if errors:
        raise TestFailure("Simultaneous Predict submission failed:\n" + "\n".join(errors))

    for run, result in zip(runs, results, strict=True):
        assert isinstance(result, tuple)
        status_code, body = result
        run.request_id = str(body["request_id"])
        run.initial_status = str(body["status"])
        run.worker = body.get("worker")
        validate_request_identity(run, body)
        worker = run.worker or "-"
        print(
            f"[SUBMIT] {run.student_id:<10} HTTP {status_code:<3} "
            f"{run.initial_status:<10} {worker:<12} {run.request_id}",
            flush=True,
        )
    if len({run.request_id for run in runs}) != len(runs):
        raise TestFailure("Predict request IDs are not unique")
    save_state(runs)


async def get_request(
    client: httpx.AsyncClient, server: str, run: StudentRun
) -> dict[str, Any]:
    try:
        response = await client.get(
            f"{server}/requests/{run.request_id}",
            headers={"X-Student-Password": run.password},
        )
    except httpx.HTTPError as exc:
        raise TestFailure(f"[QUERY] {run.student_id} {run.request_id}: {exc}") from exc
    if response.status_code != 200:
        raise TestFailure(response_problem(run.student_id, "QUERY", response))
    try:
        body = response.json()
    except ValueError as exc:
        raise TestFailure(response_problem(run.student_id, "QUERY", response)) from exc
    validate_request_identity(run, body)
    return body


async def poll_one(
    client: httpx.AsyncClient,
    server: str,
    run: StudentRun,
    deadline: float,
) -> dict[str, Any]:
    previous = run.initial_status
    if previous in TERMINAL_STATUSES:
        body = await get_request(client, server, run)
        run.worker = body.get("worker") or run.worker
        return body
    while time.monotonic() < deadline:
        body = await get_request(client, server, run)
        status = str(body.get("status"))
        if status != previous:
            print(
                f"[STATE] {run.student_id} {run.request_id} "
                f"{previous:<9} -> {status:<9} worker={body.get('worker') or '-'}",
                flush=True,
            )
            previous = status
        run.worker = body.get("worker") or run.worker
        if status in TERMINAL_STATUSES:
            return body
        await asyncio.sleep(1)
    raise TestFailure(
        f"[TIMEOUT] {run.student_id} {run.request_id} remained {previous!r}"
    )


def validate_worker_snapshot(workers: Any) -> None:
    if not isinstance(workers, list):
        raise TestFailure("[WORKERS] response is not a JSON list")
    active = [row for row in workers if row.get("student_id")]
    students = [str(row["student_id"]) for row in active]
    leases = [str(row["lease_id"]) for row in active if row.get("lease_id")]
    if len(students) != len(set(students)):
        raise TestFailure("Worker ownership conflict: one student owns multiple Workers")
    if len(leases) != len(set(leases)):
        raise TestFailure("Worker ownership conflict: one lease appears on multiple Workers")


async def monitor_worker_ownership(
    client: httpx.AsyncClient, server: str, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        response = await client.get(f"{server}/workers")
        if response.status_code != 200:
            raise TestFailure(response_problem("all", "WORKERS", response))
        try:
            validate_worker_snapshot(response.json())
        except ValueError as exc:
            raise TestFailure("[WORKERS] invalid JSON response") from exc
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except TimeoutError:
            pass


async def phase3_poll(
    client: httpx.AsyncClient,
    server: str,
    runs: list[StudentRun],
    timeout: float,
) -> None:
    banner("PHASE 3 - Request Processing")
    deadline = time.monotonic() + timeout
    stop_event = asyncio.Event()
    monitor = asyncio.create_task(monitor_worker_ownership(client, server, stop_event))
    poll_tasks = [
        asyncio.create_task(poll_one(client, server, run, deadline)) for run in runs
    ]
    results = await asyncio.gather(*poll_tasks, return_exceptions=True)
    stop_event.set()
    monitor_result = await asyncio.gather(monitor, return_exceptions=True)
    errors = [str(result) for result in results if isinstance(result, Exception)]
    errors.extend(str(result) for result in monitor_result if isinstance(result, Exception))
    if errors:
        raise TestFailure("Request processing failed:\n" + "\n".join(errors))
    for run, result in zip(runs, results, strict=True):
        assert isinstance(result, dict)
        run.final = result
        run.worker = result.get("worker") or run.worker
    failed = [run.student_id for run in runs if run.final and run.final.get("status") != "completed"]
    if failed:
        details = [
            f"{run.student_id}: status={run.final.get('status')} error={run.final.get('error')}"
            for run in runs
            if run.student_id in failed and run.final
        ]
        raise TestFailure("Requests did not complete:\n" + "\n".join(details))
    print("\nWorker ownership snapshots: NO CONFLICT OK", flush=True)


async def event_types_for(
    client: httpx.AsyncClient,
    server: str,
    *,
    student_id: str,
    artifact_id: str | None = None,
    request_id: str | None = None,
) -> set[str]:
    params: dict[str, str | int] = {"limit": 1000, "student_id": student_id}
    if artifact_id:
        params["artifact_id"] = artifact_id
    if request_id:
        params["request_id"] = request_id
    response = await client.get(f"{server}/events", params=params)
    if response.status_code != 200:
        raise TestFailure(response_problem(student_id, "EVENTS", response))
    try:
        events = response.json()
        return {
            str(event["event_type"])
            for event in events
            if event.get("student_id") == student_id
            and (not artifact_id or event.get("artifact_id") == artifact_id)
            and (not request_id or event.get("request_id") == request_id)
        }
    except (TypeError, KeyError, ValueError) as exc:
        raise TestFailure(f"[EVENTS] {student_id}: invalid JSON response") from exc


async def check_audit(
    client: httpx.AsyncClient,
    server: str,
    runs: list[StudentRun],
    *,
    persistence: bool,
) -> None:
    title = "PHASE 5C - AuditEvent Persistence" if persistence else "PHASE 4 - Audit Events"
    banner(title)
    errors: list[str] = []
    for run in runs:
        artifact_events, request_events = await asyncio.gather(
            event_types_for(
                client,
                server,
                student_id=run.student_id,
                artifact_id=run.artifact_id,
            ),
            event_types_for(
                client,
                server,
                student_id=run.student_id,
                request_id=run.request_id,
            ),
        )
        missing = EXPECTED_REQUEST_EVENTS.difference(request_events)
        if "ARTIFACT_UPLOADED" not in artifact_events:
            missing.add("ARTIFACT_UPLOADED")
        if missing:
            errors.append(f"{run.student_id}: missing {', '.join(sorted(missing))}")
            print(f"[AUDIT] {run.student_id} FAIL missing={','.join(sorted(missing))}", flush=True)
        else:
            label = "PERSIST" if persistence else "PASS"
            print(f"[AUDIT] {run.student_id} {label}", flush=True)
    if errors:
        raise TestFailure("Audit validation failed:\n" + "\n".join(errors))


async def wait_for_restart(client: httpx.AsyncClient, server: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{server}/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise TestFailure(f"Central did not become healthy within {timeout:g} seconds")


async def phase5_restart(
    client: httpx.AsyncClient,
    server: str,
    runs: list[StudentRun],
    timeout: float,
) -> None:
    banner("CENTRAL RESTART PERSISTENCE CHECK", 60)
    print(
        "\n现在请重启 Central Server。\n\n"
        "例如：\n\n"
        "Ctrl+C\n\n"
        "重新运行：\n\n"
        "uvicorn app.main:app --host 0.0.0.0 --port 8000\n\n"
        "或者：\n\n"
        "sudo systemctl restart kv260-central\n\n"
        "重启完成后按 Enter。",
        flush=True,
    )
    await asyncio.to_thread(input)
    await wait_for_restart(client, server, timeout)

    banner("PHASE 5A - Original Request ID Persistence")
    results = await asyncio.gather(
        *(get_request(client, server, run) for run in runs), return_exceptions=True
    )
    errors: list[str] = []
    for run, result in zip(runs, results, strict=True):
        if isinstance(result, Exception):
            errors.append(str(result))
            print(f"[PERSIST] {run.student_id} FAIL {run.request_id} query-error", flush=True)
            continue
        if result.get("status") != "completed":
            errors.append(f"{run.student_id}: old request status={result.get('status')}")
            print(
                f"[PERSIST] {run.student_id} FAIL {run.request_id} {result.get('status')}",
                flush=True,
            )
            continue
        print(
            f"[PERSIST] {run.student_id} PASS {run.request_id} completed", flush=True
        )
    if errors:
        raise TestFailure("PredictRequest persistence failed:\n" + "\n".join(errors))

    banner("PHASE 5B - Artifact Metadata Persistence")
    response = await client.get(f"{server}/fpga/artifacts")
    if response.status_code != 200:
        raise TestFailure(response_problem("all", "ARTIFACT PERSIST", response))
    try:
        by_id = {str(item["artifact_id"]): item for item in response.json()}
    except (TypeError, KeyError, ValueError) as exc:
        raise TestFailure("[ARTIFACT PERSIST] invalid JSON response") from exc
    for run in runs:
        artifact = by_id.get(run.artifact_id)
        expected = {
            "student_id": run.student_id,
            "version": run.version,
            "bit_sha256": run.bit_sha256,
            "hwh_sha256": run.hwh_sha256,
        }
        if artifact is None or any(artifact.get(key) != value for key, value in expected.items()):
            raise TestFailure(
                f"[ARTIFACT PERSIST] {run.student_id}: metadata missing or changed for "
                f"{run.artifact_id}"
            )
        print(f"[ARTIFACT PERSIST] {run.student_id} PASS {run.artifact_id} {run.version}", flush=True)

    await check_audit(client, server, runs, persistence=True)


def print_result(
    runs: list[StudentRun],
    passed: bool,
    error: str | None = None,
    *,
    restart_checked: bool = False,
    failure_students: list[str] | None = None,
) -> None:
    banner("RESULT")
    print(
        f"{'Student':<13}{'Artifact':<39}{'Request':<39}{'Worker':<15}{'Status'}",
        flush=True,
    )
    for run in runs:
        status = str(run.final.get("status")) if run.final else (run.initial_status or "not-run")
        print(
            f"{run.student_id:<13}{run.artifact_id:<39}{run.request_id or '-':<39}"
            f"{run.worker or '-':<15}{status}",
            flush=True,
        )
    completed = sum(bool(run.final and run.final.get("status") == "completed") for run in runs)
    failed_students = {
        run.student_id
        for run in runs
        if not run.final or run.final.get("status") != "completed"
    }
    failed_students.update(failure_students or [])
    print(f"\nCompleted : {completed}\nFailed    : {len(failed_students)}", flush=True)
    if failed_students:
        print("Failed Students: " + ", ".join(sorted(failed_students)), flush=True)
    if error:
        print(f"\nERROR: {error}", flush=True)
    print(
        f"\nMULTI-STUDENT HTTP TEST: {'PASS' if passed else 'FAIL'}",
        flush=True,
    )
    if passed and restart_checked:
        print(
            "Persistence means the old request_id survived a Central process restart via "
            "SQLite central.db; asyncio.Lock, background tasks, and Python objects were "
            "re-created and are not the persistence mechanism.",
            flush=True,
        )


async def run(args: argparse.Namespace) -> int:
    server = args.server.rstrip("/")
    bit_bytes = args.bit.read_bytes()
    hwh_bytes = args.hwh.read_bytes()
    image_bytes = args.image.read_bytes()
    if not bit_bytes or not hwh_bytes or not image_bytes:
        raise TestFailure("--bit, --hwh, and --image must all be non-empty files")
    timeout = httpx.Timeout(args.timeout)
    runs: list[StudentRun] = []
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        try:
            await phase0_health(client, server)
            runs = await phase1_artifacts(
                client, server, bit_bytes, hwh_bytes, args.skip_upload
            )
            await phase2_submit(client, server, runs, image_bytes)
            await phase3_poll(client, server, runs, args.timeout)
            await check_audit(client, server, runs, persistence=False)
            if args.restart_check:
                await phase5_restart(client, server, runs, args.timeout)
        except (TestFailure, httpx.HTTPError, OSError) as exc:
            error = str(exc)
            failure_students = [
                student_id for student_id, _ in STUDENTS if student_id in error
            ]
            print_result(
                runs,
                False,
                error,
                restart_checked=args.restart_check,
                failure_students=failure_students,
            )
            return 1
    print_result(runs, True, restart_checked=args.restart_check)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Real HTTP test for five students and Central SQLite persistence"
    )
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--bit", type=Path, required=True)
    parser.add_argument("--hwh", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="use each test student's latest ready Artifact instead of uploading",
    )
    parser.add_argument(
        "--restart-check",
        action="store_true",
        help="pause for a Central restart, then query the original persistent IDs",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    for option in ("bit", "hwh", "image"):
        path = getattr(args, option)
        if not path.is_file():
            parser.error(f"--{option} is not a file: {path}")
    return args


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
