from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import AuditEvent, utcnow

LOGGER = logging.getLogger(__name__)
SENSITIVE_DETAIL_KEYS = {
    "password", "password_hash", "password_salt", "old_password", "new_password",
    "admin_token", "x-admin-token", "x-student-password", "lease_secret",
    "worker_secret", "session_secret", "image_base64", "payload",
}
AUDIT_EVENT_TYPES: tuple[tuple[str, str], ...] = (
    ("ARTIFACT_UPLOADED", "Artifact 上传"),
    ("AUTH_FAILED", "学生认证失败"),
    ("STUDENT_PASSWORD_CHANGED", "学生密码修改"),
    ("REQUEST_CREATED", "Request 创建"),
    ("REQUEST_STARTED", "Request 开始"),
    ("REQUEST_COMPLETED", "Request 完成"),
    ("REQUEST_FAILED", "Request 失败"),
    ("WORKER_ASSIGNED", "Worker 分配"),
    ("WORKER_ONLINE", "Worker 上线"),
    ("WORKER_OFFLINE", "Worker 离线"),
    ("WORKER_RELEASED", "Worker 释放"),
    ("ADMIN_WORKER_RELEASE", "管理员释放 Worker"),
    ("FPGA_DEPLOYED", "FPGA 部署成功"),
    ("FPGA_DEPLOY_FAILED", "FPGA 部署失败"),
    ("ARTIFACT_ARCHIVED", "Artifact 已归档"),
    ("ARTIFACT_CLEANUP_FAILED", "Artifact 清理失败"),
    ("ADMIN_ARTIFACT_CLEANUP", "管理员 Artifact 清理"),
    ("ARTIFACT_ADMIN_DELETED", "管理员删除 Artifact"),
    ("ARTIFACT_ADMIN_DELETE_FAILED", "管理员删除 Artifact 失败"),
)


class AuditLogger:
    """Best-effort persistent audit trail, isolated from business transactions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @classmethod
    def _sanitize_details(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._sanitize_details(item)
                for key, item in value.items()
                if str(key).lower() not in SENSITIVE_DETAIL_KEYS
            }
        if isinstance(value, list):
            return [cls._sanitize_details(item) for item in value]
        return value

    def record(
        self,
        event_type: str,
        *,
        level: str = "INFO",
        actor_type: str | None = None,
        actor_id: str | None = None,
        student_id: str | None = None,
        board: str | None = None,
        artifact_id: str | None = None,
        request_id: str | None = None,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            with self.sessions() as database:
                database.add(
                    AuditEvent(
                        id=f"evt_{uuid.uuid4().hex}",
                        event_type=event_type.upper(),
                        level=level.upper(),
                        actor_type=actor_type,
                        actor_id=actor_id,
                        student_id=student_id,
                        board=board,
                        artifact_id=artifact_id,
                        request_id=request_id,
                        message=message,
                        details=self._sanitize_details(details) if details else details,
                        created_at=utcnow(),
                    )
                )
                database.commit()
        except Exception:
            LOGGER.exception("failed to persist audit event: event_type=%s", event_type)

    def list_events(
        self,
        *,
        limit: int,
        event_type: str | None = None,
        level: str | None = None,
        student_id: str | None = None,
        board: str | None = None,
        artifact_id: str | None = None,
        request_id: str | None = None,
    ) -> list[AuditEvent]:
        with self.sessions() as database:
            query = select(AuditEvent)
            filters = {
                AuditEvent.event_type: event_type.upper() if event_type else None,
                AuditEvent.level: level.upper() if level else None,
                AuditEvent.student_id: student_id,
                AuditEvent.board: board,
                AuditEvent.artifact_id: artifact_id,
                AuditEvent.request_id: request_id,
            }
            for column, value in filters.items():
                if value is not None:
                    query = query.where(column == value)
            query = query.order_by(
                AuditEvent.created_at.desc(), AuditEvent.id.desc()
            ).limit(limit)
            return list(database.scalars(query).all())
