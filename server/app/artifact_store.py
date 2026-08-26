from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Artifact, ArtifactStatus, utcnow


class ArtifactValidationError(ValueError):
    pass


class ArtifactStore:
    _VERSION_PATTERN = re.compile(r"^v([1-9][0-9]*)$")

    def __init__(
        self,
        root: Path,
        sessions: sessionmaker[Session],
        max_bit_size: int,
        max_hwh_size: int,
    ) -> None:
        self.root = root
        self.tmp_root = root.parent / "tmp"
        self.sessions = sessions
        self.max_bit_size = max_bit_size
        self.max_hwh_size = max_hwh_size
        # V1 runs one Central Server process. This lock keeps version lookup,
        # allocation, and metadata insertion atomic within that process.
        self.version_allocation_lock = asyncio.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _next_version(cls, database: Session, student_id: str) -> str:
        versions = database.scalars(
            select(Artifact.version).where(Artifact.student_id == student_id)
        )
        highest = 0
        for version in versions:
            match = cls._VERSION_PATTERN.fullmatch(version)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"v{highest + 1}"

    @staticmethod
    async def _save_upload(
        upload: UploadFile, destination: Path, expected_suffix: str, max_size: int
    ) -> tuple[str, int]:
        filename = upload.filename or ""
        if Path(filename).suffix.lower() != expected_suffix:
            raise ArtifactValidationError(f"file extension must be {expected_suffix}")
        digest = hashlib.sha256()
        size = 0
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_size:
                    raise ArtifactValidationError(
                        f"{expected_suffix} file exceeds maximum size {max_size}"
                    )
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise ArtifactValidationError(f"{expected_suffix} file is empty")
        return digest.hexdigest(), size

    async def create(
        self,
        student_id: str,
        bit: UploadFile,
        hwh: UploadFile,
    ) -> Artifact:
        artifact_id = f"art_{uuid.uuid4().hex}"
        staging = Path(tempfile.mkdtemp(prefix="artifact-", dir=self.tmp_root))
        final = self.root / artifact_id
        moved_to_final = False
        try:
            bit_path = staging / "design.bit"
            hwh_path = staging / "design.hwh"
            bit_sha, bit_size = await self._save_upload(
                bit, bit_path, ".bit", self.max_bit_size
            )
            hwh_sha, hwh_size = await self._save_upload(
                hwh, hwh_path, ".hwh", self.max_hwh_size
            )
            try:
                ET.parse(hwh_path)
            except ET.ParseError as exc:
                raise ArtifactValidationError(f"invalid HWH XML: {exc}") from exc
            async with self.version_allocation_lock:
                with self.sessions() as database:
                    version = self._next_version(database, student_id)
                    manifest = {
                        "artifact_id": artifact_id,
                        "student_id": student_id,
                        "version": version,
                        "bit_sha256": bit_sha,
                        "hwh_sha256": hwh_sha,
                        "bit_size": bit_size,
                        "hwh_size": hwh_size,
                    }
                    (staging / "manifest.json").write_text(
                        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(staging, final)
                    moved_to_final = True
                    artifact = Artifact(
                        id=artifact_id,
                        student_id=student_id,
                        legacy_project_name="legacy",
                        version=version,
                        bit_path=str(final / "design.bit"),
                        hwh_path=str(final / "design.hwh"),
                        bit_sha256=bit_sha,
                        hwh_sha256=hwh_sha,
                        bit_size=bit_size,
                        hwh_size=hwh_size,
                        created_at=utcnow(),
                        status=ArtifactStatus.READY.value,
                    )
                    database.add(artifact)
                    database.commit()
                    return artifact
        except Exception:
            if moved_to_final:
                shutil.rmtree(final, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
