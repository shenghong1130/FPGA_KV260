from __future__ import annotations

import os
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .fpga import (
    FpgaExecutionError,
    FpgaBackendProtocol,
    PredictPayloadError,
    PynqFpgaBackend,
)
from .state import WorkerConflictError, WorkerState, WorkerValidationError

BOARD_PATTERN = re.compile(r"^kv260([1-9]|1[0-9]|20)$")


class PredictBody(BaseModel):
    lease_id: str
    payload: dict[str, Any]


class ReleaseBody(BaseModel):
    lease_id: str


def validate_board_hostname(hostname: str) -> str:
    board = hostname.strip()
    if not BOARD_PATTERN.fullmatch(board):
        raise RuntimeError(
            f"invalid KV260 hostname {board!r}; expected kv2601 ... kv26020"
        )
    return board


def create_app(
    *,
    board: str | None = None,
    artifact_root: Path | None = None,
    backend: FpgaBackendProtocol | None = None,
) -> FastAPI:
    selected_board = validate_board_hostname(board) if board else socket.gethostname()
    selected_root = artifact_root or Path(
        os.getenv("KV260_WORKER_ARTIFACT_ROOT", "/var/lib/kv260-worker/artifacts")
    )
    state = WorkerState(selected_board, selected_root, backend or PynqFpgaBackend())

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        state.board = validate_board_hostname(state.board)
        await state.initialize()
        application.state.worker = state
        yield

    application = FastAPI(title=f"KV260 FPGA Worker {selected_board}", lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "board": state.board}

    @application.get("/status")
    async def status() -> dict[str, Any]:
        return state.status()

    @application.post("/internal/deploy")
    async def deploy(
        lease_id: str = Form(),
        artifact_id: str = Form(),
        bit_sha256: str = Form(),
        hwh_sha256: str = Form(),
        bit: UploadFile = File(),
        hwh: UploadFile = File(),
    ) -> dict[str, Any]:
        bit_data = await bit.read()
        hwh_data = await hwh.read()
        await bit.close()
        await hwh.close()
        try:
            return await state.deploy(
                lease_id,
                artifact_id,
                bit_sha256,
                hwh_sha256,
                bit_data,
                hwh_data,
            )
        except WorkerValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except WorkerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"PYNQ Overlay deployment failed: {exc}"
            ) from exc

    @application.post("/predict")
    async def predict(body: PredictBody) -> dict[str, Any]:
        try:
            return await state.predict(body.lease_id, body.payload)
        except WorkerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PredictPayloadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FpgaExecutionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.post("/internal/release")
    async def release(body: ReleaseBody) -> dict[str, Any]:
        try:
            return await state.release(body.lease_id)
        except WorkerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return application


app = create_app()
