from __future__ import annotations

import asyncio
import hashlib
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel


@dataclass(slots=True)
class MockState:
    board: str
    current_artifact_id: str | None = None
    current_lease_id: str | None = None
    fpga_ready: bool = False
    predict_count: int = 0
    deploy_count: int = 0
    active_predicts: int = 0
    max_concurrent_predicts: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PredictBody(BaseModel):
    lease_id: str
    payload: dict[str, Any]


class ReleaseBody(BaseModel):
    lease_id: str


state = MockState(board=os.getenv("MOCK_BOARD", "mock-kv2601"))
app = FastAPI(title=f"Mock Worker {state.board}")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/status")
async def status() -> dict[str, Any]:
    return {
        "board": state.board,
        "fpga_ready": state.fpga_ready,
        "lease_id": state.current_lease_id,
        "artifact_id": state.current_artifact_id,
        "predict_count": state.predict_count,
        "deploy_count": state.deploy_count,
        "max_concurrent_predicts": state.max_concurrent_predicts,
    }


@app.post("/internal/deploy")
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
    if not bit_data or hashlib.sha256(bit_data).hexdigest() != bit_sha256:
        raise HTTPException(status_code=422, detail="bit SHA-256 mismatch")
    if not hwh_data or hashlib.sha256(hwh_data).hexdigest() != hwh_sha256:
        raise HTTPException(status_code=422, detail="hwh SHA-256 mismatch")
    try:
        ET.fromstring(hwh_data)
    except ET.ParseError as exc:
        raise HTTPException(status_code=422, detail=f"invalid HWH XML: {exc}") from exc
    async with state.lock:
        if state.current_lease_id not in (None, lease_id):
            raise HTTPException(status_code=409, detail="worker already leased")
        await asyncio.sleep(float(os.getenv("MOCK_DEPLOY_DELAY", "0.05")))
        state.current_artifact_id = artifact_id
        state.current_lease_id = lease_id
        state.fpga_ready = True
        state.predict_count = 0
        state.deploy_count += 1
    return {
        "ok": True,
        "fpga_ready": True,
        "lease_id": lease_id,
        "artifact_id": artifact_id,
    }


@app.post("/predict")
async def predict(body: PredictBody) -> dict[str, Any]:
    async with state.lock:
        if body.lease_id != state.current_lease_id or not state.fpga_ready:
            raise HTTPException(status_code=409, detail="lease does not own worker")
        state.active_predicts += 1
        state.max_concurrent_predicts = max(
            state.max_concurrent_predicts, state.active_predicts
        )
        state.predict_count += 1
        predict_index = state.predict_count
        artifact_id = state.current_artifact_id
        try:
            await asyncio.sleep(float(os.getenv("MOCK_PREDICT_DELAY", "0.15")))
        finally:
            state.active_predicts -= 1
    if "image_base64" in body.payload:
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
        "board": state.board,
        "lease_id": body.lease_id,
        "artifact_id": artifact_id,
        "predict_index": predict_index,
        "input": body.payload,
    }


@app.post("/internal/release")
async def release(body: ReleaseBody) -> dict[str, Any]:
    async with state.lock:
        if state.current_lease_id not in (None, body.lease_id):
            raise HTTPException(status_code=409, detail="lease does not own worker")
        state.current_lease_id = None
        state.fpga_ready = False
    return {"ok": True, "lease_id": body.lease_id}
