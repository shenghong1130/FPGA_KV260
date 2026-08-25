from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactStatus(str, enum.Enum):
    READY = "READY"
    FAILED = "FAILED"


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

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), index=True)
    project_name: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(128))
    bit_path: Mapped[str] = mapped_column(Text)
    hwh_path: Mapped[str] = mapped_column(Text)
    bit_sha256: Mapped[str] = mapped_column(String(64))
    hwh_sha256: Mapped[str] = mapped_column(String(64))
    bit_size: Mapped[int] = mapped_column(BigInteger)
    hwh_size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default=ArtifactStatus.READY.value)


class Worker(Base):
    __tablename__ = "workers"

    board: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(512), unique=True)
    state: Mapped[str] = mapped_column(String(32), default=WorkerState.OFFLINE.value)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
