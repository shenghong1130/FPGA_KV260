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
