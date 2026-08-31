from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .audit import AuditLogger
from .db_models import (
    Artifact,
    ArtifactStatus,
    PredictRequestRecord,
    RequestStatus,
    StudentLease,
    Worker,
)
from .lease_manager import LeaseManager

SAFE_ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{32}$")


@dataclass(slots=True)
class CleanupCandidate:
    artifact_id: str
    student_id: str
    version: str
    size: int


class ArtifactCleanupService:
    def __init__(
        self,
        root: Path,
        sessions: sessionmaker[Session],
        lease_manager: LeaseManager,
        audit: AuditLogger,
    ) -> None:
        self.root = root.resolve()
        self.sessions = sessions
        self.lease_manager = lease_manager
        self.audit = audit
        self.cleanup_lock = asyncio.Lock()

    @staticmethod
    def _protected_ids(database: Session) -> set[str]:
        protected: set[str] = set()

        # The first READY row for each student is its latest deployable Artifact.
        seen_students: set[str] = set()
        ready = database.scalars(
            select(Artifact)
            .where(Artifact.status == ArtifactStatus.READY.value)
            .order_by(
                Artifact.student_id,
                Artifact.created_at.desc(),
                Artifact.id.desc(),
            )
        ).all()
        for artifact in ready:
            if artifact.student_id not in seen_students:
                protected.add(artifact.id)
                seen_students.add(artifact.student_id)

        protected.update(
            value
            for value in database.scalars(
                select(StudentLease.current_artifact_id).where(
                    StudentLease.current_artifact_id.is_not(None)
                )
            ).all()
            if value
        )
        protected.update(
            value
            for value in database.scalars(
                select(Worker.current_artifact_id).where(
                    Worker.current_artifact_id.is_not(None)
                )
            ).all()
            if value
        )
        protected.update(
            database.scalars(
                select(PredictRequestRecord.artifact_id).where(
                    PredictRequestRecord.status.in_(
                        [RequestStatus.QUEUED.value, RequestStatus.RUNNING.value]
                    )
                )
            ).all()
        )
        return protected

    @staticmethod
    def _candidate(artifact: Artifact) -> CleanupCandidate:
        return CleanupCandidate(
            artifact_id=artifact.id,
            student_id=artifact.student_id,
            version=artifact.version,
            size=int(artifact.bit_size or 0) + int(artifact.hwh_size or 0),
        )

    def preview(self) -> dict:
        with self.sessions() as database:
            protected = self._protected_ids(database)
            artifacts = database.scalars(
                select(Artifact)
                .where(Artifact.status == ArtifactStatus.READY.value)
                .order_by(Artifact.student_id, Artifact.created_at, Artifact.id)
            ).all()
            candidates = [
                self._candidate(artifact)
                for artifact in artifacts
                if artifact.id not in protected
            ]
        return {
            "candidates": len(candidates),
            "protected": len(protected),
            "reclaimable_bytes": sum(item.size for item in candidates),
            "artifacts": [asdict(item) for item in candidates],
        }

    def _artifact_directory(self, artifact_id: str) -> Path:
        if not SAFE_ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("unsafe artifact id")
        target = self.root / artifact_id
        if target.parent != self.root:
            raise ValueError("artifact directory escapes artifact root")
        if target.is_symlink():
            raise ValueError("artifact directory must not be a symlink")
        if target.exists() and target.resolve() != target:
            raise ValueError("artifact directory escapes artifact root")
        return target

    async def _remove_directory(self, artifact_id: str) -> bool:
        target = self._artifact_directory(artifact_id)
        if not target.exists():
            return False
        if not target.is_dir():
            raise ValueError("artifact path is not a directory")
        # Artifact directories contain only the small, fixed bit/hwh/manifest
        # set. Keep deletion inside the serialized cleanup operation.
        shutil.rmtree(target)
        return True

    async def execute(self) -> dict:
        archived_count = 0
        freed_bytes = 0
        failed: list[dict[str, str]] = []

        async with self.cleanup_lock:
            # This is intentionally a fresh snapshot, independent of any Preview.
            initial = self.preview()["artifacts"]
            for item in initial:
                student_id = item["student_id"]
                artifact_id = item["artifact_id"]
                async with self.lease_manager.student_locks[student_id]:
                    with self.sessions() as database:
                        artifact = database.get(Artifact, artifact_id)
                        protected = self._protected_ids(database)
                        if (
                            artifact is None
                            or artifact.status != ArtifactStatus.READY.value
                            or artifact.id in protected
                        ):
                            continue
                        size = int(artifact.bit_size or 0) + int(artifact.hwh_size or 0)
                        version = artifact.version

                    try:
                        files_existed = await self._remove_directory(artifact_id)
                    except Exception as exc:
                        error = str(exc) or type(exc).__name__
                        failed.append({"artifact_id": artifact_id, "error": error})
                        self.audit.record(
                            "ARTIFACT_CLEANUP_FAILED",
                            level="ERROR",
                            actor_type="admin",
                            student_id=student_id,
                            artifact_id=artifact_id,
                            message="Artifact cleanup failed",
                            details={"error": error},
                        )
                        continue

                    with self.sessions() as database:
                        artifact = database.get(Artifact, artifact_id)
                        if artifact is None:
                            continue
                        artifact.status = ArtifactStatus.ARCHIVED.value
                        database.commit()
                    archived_count += 1
                    if files_existed:
                        freed_bytes += size
                    self.audit.record(
                        "ARTIFACT_ARCHIVED",
                        student_id=student_id,
                        artifact_id=artifact_id,
                        message=f"Archived Artifact {version}",
                        details={
                            "version": version,
                            "freed_bytes": size if files_existed else 0,
                            "files_missing": not files_existed,
                        },
                    )

        result = {
            "archived_count": archived_count,
            "failed_count": len(failed),
            "freed_bytes": freed_bytes,
            "failed": failed,
        }
        self.audit.record(
            "ADMIN_ARTIFACT_CLEANUP",
            level="WARNING",
            actor_type="admin",
            message="Administrator completed Artifact cleanup",
            details={
                "archived_count": archived_count,
                "failed_count": len(failed),
                "freed_bytes": freed_bytes,
            },
        )
        return result
