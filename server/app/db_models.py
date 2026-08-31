from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactStatus(str, enum.Enum):
    READY = "READY"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class SessionStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    DEPLOYING = "DEPLOYING"
    READY = "READY"
    BUSY = "BUSY"
    RELEASING = "RELEASING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    LOST = "LOST"


class LeaseStatus(str, enum.Enum):
    UNASSIGNED = "UNASSIGNED"
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    DEPLOYING = "DEPLOYING"
    READY = "READY"
    BUSY = "BUSY"
    RELEASING = "RELEASING"
    ERROR = "ERROR"
    LOST = "LOST"


class RequestStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WorkerState(str, enum.Enum):
    IDLE = "IDLE"
    RESERVED = "RESERVED"
    DEPLOYING = "DEPLOYING"
    READY = "READY"
    BUSY = "BUSY"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"


class Base(DeclarativeBase):
    pass


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("student_id", "version", name="uq_artifacts_student_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), index=True)
    # Legacy database compatibility only. Existing V1 SQLite databases have a
    # NOT NULL project_name column, so new rows fill it internally while the
    # public API and Artifact business logic no longer expose or use it.
    legacy_project_name: Mapped[str] = mapped_column(
        "project_name", String(256), default="legacy"
    )
    version: Mapped[str] = mapped_column(String(128))
    bit_path: Mapped[str] = mapped_column(Text)
    hwh_path: Mapped[str] = mapped_column(Text)
    bit_sha256: Mapped[str] = mapped_column(String(64))
    hwh_sha256: Mapped[str] = mapped_column(String(64))
    bit_size: Mapped[int] = mapped_column(BigInteger)
    hwh_size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default=ArtifactStatus.READY.value)


class StudentCredential(Base):
    __tablename__ = "student_credentials"

    student_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    password_salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    password_hash: Mapped[bytes] = mapped_column(LargeBinary(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Worker(Base):
    __tablename__ = "workers"

    board: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(512), unique=True)
    state: Mapped[str] = mapped_column(String(32), default=WorkerState.OFFLINE.value)
    # Legacy SQLite column name; current code treats it only as a lease ID.
    lease_id: Mapped[str | None] = mapped_column(
        "session_id", String(64), nullable=True, index=True
    )
    current_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fpga_ready: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class StudentLease(Base):
    __tablename__ = "student_leases"

    student_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    lease_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    current_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PredictRequestRecord(Base):
    __tablename__ = "predict_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    artifact_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    student_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    board: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
