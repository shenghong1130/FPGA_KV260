from __future__ import annotations

import sqlite3

from sqlalchemy import inspect, text

from app.database import Database


def test_sqlite_worker_id_upgrade_and_audit_backfill_are_idempotent(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE artifacts (
            id VARCHAR(64) PRIMARY KEY,
            student_id VARCHAR(128) NOT NULL,
            project_name VARCHAR(256) NOT NULL,
            version VARCHAR(128) NOT NULL,
            bit_path TEXT NOT NULL,
            hwh_path TEXT NOT NULL,
            bit_sha256 VARCHAR(64) NOT NULL,
            hwh_sha256 VARCHAR(64) NOT NULL,
            bit_size BIGINT NOT NULL,
            hwh_size BIGINT NOT NULL,
            created_at DATETIME NOT NULL,
            status VARCHAR(32) NOT NULL
        );
        CREATE TABLE student_credentials (
            student_id VARCHAR(128) PRIMARY KEY,
            password_salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE workers (
            board VARCHAR(64) PRIMARY KEY,
            base_url VARCHAR(512) NOT NULL,
            state VARCHAR(32) NOT NULL,
            session_id VARCHAR(64),
            current_artifact_id VARCHAR(64),
            fpga_ready INTEGER NOT NULL,
            last_seen DATETIME,
            last_error TEXT
        );
        CREATE TABLE student_leases (
            student_id VARCHAR(128) PRIMARY KEY,
            lease_id VARCHAR(64),
            worker_id VARCHAR(64),
            current_artifact_id VARCHAR(64),
            state VARCHAR(32) NOT NULL,
            created_at DATETIME NOT NULL,
            queued_at DATETIME,
            activated_at DATETIME,
            last_activity_at DATETIME NOT NULL,
            released_at DATETIME,
            request_count INTEGER NOT NULL,
            error TEXT
        );
        CREATE TABLE predict_requests (
            id VARCHAR(64) PRIMARY KEY,
            student_id VARCHAR(128) NOT NULL,
            artifact_id VARCHAR(64) NOT NULL,
            artifact_version VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL,
            payload JSON NOT NULL,
            result JSON,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            completed_at DATETIME,
            error TEXT
        );
        CREATE TABLE audit_events (
            id VARCHAR(64) PRIMARY KEY,
            event_type VARCHAR(64) NOT NULL,
            level VARCHAR(16) NOT NULL,
            actor_type VARCHAR(32),
            actor_id VARCHAR(128),
            student_id VARCHAR(128),
            board VARCHAR(64),
            artifact_id VARCHAR(64),
            request_id VARCHAR(64),
            message TEXT NOT NULL,
            details JSON,
            created_at DATETIME NOT NULL
        );
        INSERT INTO artifacts VALUES (
            'art_old', 'student01', 'legacy', 'v1', '/bit', '/hwh',
            'aaaaaaaa', 'bbbbbbbb', 10, 20, '2026-08-31 01:00:00', 'READY'
        );
        INSERT INTO student_credentials VALUES (
            'student01', X'0102', X'0304',
            '2026-08-31 01:00:00', '2026-08-31 01:00:00'
        );
        INSERT INTO workers VALUES (
            'kv2607', 'http://kv2607', 'IDLE', NULL, NULL, 0, NULL, NULL
        );
        INSERT INTO student_leases VALUES (
            'student01', NULL, NULL, NULL, 'UNASSIGNED',
            '2026-08-31 01:00:00', NULL, NULL,
            '2026-08-31 01:00:00', NULL, 1, NULL
        );
        INSERT INTO predict_requests VALUES (
            'req_old', 'student01', 'art_old', 'v1', 'COMPLETED', '{}', '{}',
            '2026-08-31 01:00:00', '2026-08-31 01:00:01',
            '2026-08-31 01:00:02', NULL
        );
        INSERT INTO predict_requests VALUES (
            'req_unknown', 'student01', 'art_old', 'v1', 'COMPLETED', '{}', '{}',
            '2026-08-31 02:00:00', '2026-08-31 02:00:01',
            '2026-08-31 02:00:02', NULL
        );
        INSERT INTO audit_events VALUES (
            'evt_old', 'REQUEST_COMPLETED', 'INFO', NULL, NULL, 'student01',
            'kv2607', 'art_old', 'req_old', 'completed', NULL,
            '2026-08-31 01:00:02'
        );
    """)
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{path}")
    database.initialize()
    database.initialize()

    columns = {item["name"] for item in inspect(database.engine).get_columns(
        "predict_requests"
    )}
    indexes = {item["name"] for item in inspect(database.engine).get_indexes(
        "predict_requests"
    )}
    assert "worker_id" in columns
    assert "ix_predict_requests_worker_id" in indexes
    with database.engine.connect() as upgraded:
        assert upgraded.execute(text(
            "SELECT worker_id FROM predict_requests WHERE id='req_old'"
        )).scalar_one() == "kv2607"
        assert upgraded.execute(text(
            "SELECT worker_id FROM predict_requests WHERE id='req_unknown'"
        )).scalar_one() is None
        for table in (
            "artifacts", "student_credentials", "workers", "student_leases",
            "predict_requests", "audit_events",
        ):
            assert upgraded.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    database.close()
