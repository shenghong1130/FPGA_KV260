# KV260 Central Scheduler V1

This directory contains the first Central Server implementation. It is isolated from the existing KV260 Runtime and does not implement a real PYNQ Worker.

## Architecture

The scheduling unit is a **Session/Lease**, not an individual Job:

```text
Artifact Store
      ↓
POST /sessions
      ↓
random IDLE Worker (atomic reservation)
      ↓
deploy design.bit + design.hwh once
      ↓
Session READY on one fixed Worker
      ↓
predict many times (serialized per Session)
      ↓
DELETE /sessions/{id}
      ↓
Worker returns to IDLE
```

Artifacts are stored independently of Workers under `data/artifacts/art_<uuid>/`. SQLite persists Artifact, Session and Worker metadata. A global `asyncio.Lock` protects `IDLE → RESERVED`; a per-Session lock serializes predict and release. When every Worker is leased, Sessions remain `QUEUED` in FIFO order until release wakes the allocator.

An active Session is never silently migrated. A serious Worker failure changes the Worker to `ERROR`/`OFFLINE` and the Session to `FAILED`/`LOST`. `READY` means leased and initialized; it never means generally available. Only `IDLE` Workers are allocatable.

`SESSION_IDLE_TIMEOUT_SECONDS` is present for future TTL support and defaults to `0` (disabled). Authentication and authorization are TODO for V2; V1 accepts `student_id` explicitly.

## API

```text
POST   /fpga/artifacts
GET    /fpga/artifacts
GET    /fpga/artifacts/{artifact_id}

POST   /sessions
GET    /sessions/{session_id}
POST   /sessions/{session_id}/predict
DELETE /sessions/{session_id}

GET    /workers
GET    /health
```

Artifact upload uses `multipart/form-data` fields `student_id`, `project_name`, `version`, `bit`, and `hwh`. User filenames are never used as storage paths. Uploads are size checked, hashed, HWH XML parsed, staged in a temporary directory, then atomically renamed.

Central expects each Worker to implement:

```text
GET  /health
GET  /status
POST /internal/deploy   (multipart design.bit/design.hwh + identity and hashes)
POST /predict           (session_id + payload)
POST /internal/release  (session_id)
```

The Mock Worker implements this contract without importing PYNQ or accessing FPGA hardware.

## Installation

Python 3.12 is required.

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

## Start Central Server

Real Worker configuration defaults to `config/workers.json`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For local Mock Workers:

```bash
WORKERS_CONFIG=config/workers.mock.json \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

FastAPI documentation is available at `http://127.0.0.1:8000/docs`.

## Run pytest

```bash
cd server
source .venv/bin/activate
pytest -v
```

The tests cover upload validation and SHA-256, atomic allocation, READY Worker exclusion, queue/release behavior, 100 fixed-Worker predictions with one deployment, concurrent Session creation, and per-Session predict serialization.

## Run Mock Cluster

Terminal 1:

```bash
cd server
source .venv/bin/activate
python -m testbed.run_mock_cluster --workers 3
```

Use `--workers 20` to expose ports `18081` through `18100`. Stop with Ctrl+C; the launcher terminates its child Uvicorn processes.

Terminal 2:

```bash
cd server
source .venv/bin/activate
WORKERS_CONFIG=config/workers.mock.json \
DATABASE_URL=sqlite:///data/smoke.db \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Run Smoke Test

Terminal 3:

```bash
cd server
source .venv/bin/activate
python -m testbed.smoke_test
```

The smoke test uses generated fake bytes and minimal valid XML—no real bitstream is committed. It verifies fixed-Worker routing, deploy-once behavior, a different Worker for a second Session, serialized concurrent predictions, release to IDLE, QUEUED behavior, and automatic FIFO allocation after release.

## Configuration

Environment variables override these defaults:

| Variable | Default |
| --- | --- |
| `SERVER_HOST` | `127.0.0.1` |
| `SERVER_PORT` | `8000` |
| `DATABASE_URL` | `sqlite:///server/data/central.db` (absolute resolved path internally) |
| `ARTIFACT_ROOT` | `server/data/artifacts` |
| `WORKERS_CONFIG` | `server/config/workers.json` |
| `WORKER_CONNECT_TIMEOUT` | `2.0` seconds |
| `WORKER_REQUEST_TIMEOUT` | `30.0` seconds |
| `WORKER_DEPLOY_TIMEOUT` | `120.0` seconds |
| `HEALTH_INTERVAL_SECONDS` | `5.0` seconds |
| `HEALTH_FAILURE_THRESHOLD` | `3` |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `0` (disabled; V1 placeholder) |
| `MAX_BIT_SIZE` | `134217728` bytes |
| `MAX_HWH_SIZE` | `16777216` bytes |

`config/workers.json` contains the 20 real KV260 endpoints on port 8080. `config/workers.mock.json` contains 20 loopback endpoints; only the number requested from `run_mock_cluster` will be healthy and allocatable.

## V1 boundaries

V1 intentionally does not contain authentication, a Web frontend, Redis, Celery, containers, HA Scheduler logic, transparent Session migration, or a real FPGA Worker. Real PYNQ Overlay, AXI DMA/MMIO, `allocate()`, Worker systemd integration, TLS and production authentication remain future work.
