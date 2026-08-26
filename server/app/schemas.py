from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ArtifactResponse(BaseModel):
    artifact_id: str
    student_id: str
    version: str
    status: str
    bit_sha256: str
    hwh_sha256: str
    bit_size: int
    hwh_size: int
    created_at: datetime


class SessionCreate(BaseModel):
    student_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=64)


class SessionResponse(BaseModel):
    session_id: str
    student_id: str
    artifact_id: str
    status: str
    worker: str | None
    request_count: int
    error: str | None = None


class PredictRequest(BaseModel):
    payload: dict[str, Any]


class WorkerResponse(BaseModel):
    board: str
    state: str
    session_id: str | None
    artifact_id: str | None
    fpga_ready: bool
    last_seen: datetime | None
    last_error: str | None
