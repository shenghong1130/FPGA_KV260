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
    worker: str | None = None
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class PredictRequestListItem(UtcResponseModel):
    request_id: str
    student_id: str
    artifact_id: str
    version: str
    status: str
    worker: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
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
    completed_requests: int
    failed_requests: int
    total_requests: int
    last_activity_at: datetime | None


class StudentSummaryResponse(UtcResponseModel):
    student_id: str
    latest_artifact_id: str | None
    latest_version: str | None
    lease_state: str
    worker_id: str | None
    queued_requests: int
    running_requests: int
    completed_requests: int
    failed_requests: int
    total_requests: int
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


class WorkerReleaseResponse(BaseModel):
    released: bool
    board: str
    student_id: str


class AuditEventResponse(UtcResponseModel):
    id: str
    event_type: str
    level: str
    actor_type: str | None = None
    actor_id: str | None = None
    student_id: str | None = None
    board: str | None = None
    artifact_id: str | None = None
    request_id: str | None = None
    message: str
    details: dict[str, Any] | None = None
    created_at: datetime


class AuditEventTypeResponse(BaseModel):
    value: str
    label: str


class ArtifactCleanupItem(BaseModel):
    artifact_id: str
    student_id: str
    version: str
    size: int


class ArtifactCleanupPreview(BaseModel):
    candidates: int
    protected: int
    reclaimable_bytes: int
    artifacts: list[ArtifactCleanupItem]


class ArtifactCleanupFailure(BaseModel):
    artifact_id: str
    error: str


class ArtifactCleanupResult(BaseModel):
    archived_count: int
    failed_count: int
    freed_bytes: int
    failed: list[ArtifactCleanupFailure]
