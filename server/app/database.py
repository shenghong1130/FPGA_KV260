from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url)
        self.sessions = sessionmaker(self.engine, class_=Session, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            self._upgrade_sqlite()

    def _upgrade_sqlite(self) -> None:
        """Apply additive, idempotent compatibility upgrades to existing SQLite DBs."""
        with self.engine.begin() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("predict_requests")
            }
            if "worker_id" not in columns:
                connection.execute(text(
                    "ALTER TABLE predict_requests ADD COLUMN worker_id VARCHAR(64)"
                ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_predict_requests_worker_id "
                "ON predict_requests(worker_id)"
            ))
            connection.execute(text("""
                UPDATE predict_requests
                SET worker_id = (
                    SELECT audit_events.board
                    FROM audit_events
                    WHERE audit_events.request_id = predict_requests.id
                      AND audit_events.board IS NOT NULL
                      AND audit_events.event_type IN (
                          'REQUEST_STARTED', 'REQUEST_COMPLETED', 'REQUEST_FAILED'
                      )
                    ORDER BY CASE audit_events.event_type
                        WHEN 'REQUEST_STARTED' THEN 0
                        WHEN 'REQUEST_COMPLETED' THEN 1
                        ELSE 2
                    END, audit_events.created_at DESC, audit_events.id DESC
                    LIMIT 1
                )
                WHERE worker_id IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM audit_events
                    WHERE audit_events.request_id = predict_requests.id
                      AND audit_events.board IS NOT NULL
                      AND audit_events.event_type IN (
                          'REQUEST_STARTED', 'REQUEST_COMPLETED', 'REQUEST_FAILED'
                      )
                  )
            """))

    def close(self) -> None:
        self.engine.dispose()
