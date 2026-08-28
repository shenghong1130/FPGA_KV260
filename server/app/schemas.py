from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .datetime_utils import ensure_utc


class UtcResponseModel(BaseModel):
    """Normalize every datetime exposed by a public API response to UTC."""

    @field_validator("*", mode="before")
    @classmethod
    def normalize_datetime(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return ensure_utc(value)
        return value


class ArtifactResponse(UtcResponseModel):
    artifact_id: str
    student_id: str
    version: str
    status: str
    bit_sha256: str
    hwh_sha256: str
    bit_size: int
    hwh_size: int
    created_at: datetime


class PublicPredictRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]


class PredictResponse(UtcResponseModel):
    request_id: str
    student_id: str
    artifact_id: str
    version: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class StudentStatusResponse(UtcResponseModel):
    student_id: str
    latest_artifact_id: str | None
    latest_version: str | None
    lease_state: str
    worker_assigned: bool
    queued_requests: int
    running_requests: int
    last_activity_at: datetime | None


class PasswordChangeRequest(BaseModel):
    new_password: str


class PasswordChangeResponse(BaseModel):
    student_id: str
    password_changed: bool


class WorkerResponse(UtcResponseModel):
    board: str
    state: str
    lease_id: str | None
    student_id: str | None
    artifact_id: str | None
    fpga_ready: bool
    last_seen: datetime | None
    last_error: str | None
