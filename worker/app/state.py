from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import shutil
import tempfile
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .hardware_validation import HardwareValidationError, validate_design
from .fpga import FpgaBackendProtocol, FpgaExecutionError, PredictPayloadError

LOGGER = logging.getLogger(__name__)


class WorkerConflictError(RuntimeError):
    pass


class WorkerValidationError(ValueError):
    pass


class WorkerState:
    _SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    _SHA256 = re.compile(r"^[0-9a-f]{64}$")

    def __init__(
        self,
        board: str,
        artifact_root: Path,
        backend: FpgaBackendProtocol,
    ) -> None:
        self.board = board
        self.artifact_root = artifact_root
        self.backend = backend
        self.current_lease_id: str | None = None
        self.current_artifact_id: str | None = None
        self.fpga_ready = False
        self.recovery_required = False
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with self._journal() as journal:
            journal.executescript("""
                CREATE TABLE IF NOT EXISTS retired_leases (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS retired_designs (
                    lease TEXT, artifact TEXT, PRIMARY KEY (lease, artifact));
                CREATE TABLE IF NOT EXISTS owner (singleton INTEGER PRIMARY KEY,
                    lease TEXT, artifact TEXT);
            """)
            row = journal.execute("SELECT lease, artifact FROM owner WHERE singleton=1").fetchone()
            if row:
                self.current_lease_id, self.current_artifact_id = row
                self.recovery_required = True
        # A restarted process retains ownership, never assumes hardware readiness.
        await self.backend.initialize()

    @contextmanager
    def _journal(self):
        journal = sqlite3.connect(self.artifact_root / "fencing.sqlite")
        try:
            with journal:
                yield journal
        finally:
            journal.close()

    async def _backend_call(self, operation):
        # asyncio.to_thread cancellation does not stop a DMA thread. Keep the
        # Worker lock until hardware work actually ends, even on cancellation.
        task = asyncio.create_task(operation)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _clean_cache(self, keep: str | None = None) -> None:
        # Only Central-generated cache directories; never remove the journal.
        for path in self.artifact_root.glob("art_*"):
            if path.name != keep and path.is_dir() and not path.is_symlink():
                try:
                    shutil.rmtree(path)
                except OSError:
                    LOGGER.exception("cannot remove obsolete Worker cache: %s", path)

    @classmethod
    def _validate_id(cls, field: str, value: str) -> None:
        if not cls._SAFE_ID.fullmatch(value):
            raise WorkerValidationError(f"invalid {field}")

    @classmethod
    def _validate_digest(cls, field: str, expected: str, content: bytes) -> None:
        if not cls._SHA256.fullmatch(expected):
            raise WorkerValidationError(f"invalid {field} format")
        actual = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise WorkerValidationError(f"{field} mismatch")

    async def deploy(
        self,
        lease_id: str,
        artifact_id: str,
        bit_sha256: str,
        hwh_sha256: str,
        bit_data: bytes,
        hwh_data: bytes,
    ) -> dict[str, Any]:
        self._validate_id("lease_id", lease_id)
        self._validate_id("artifact_id", artifact_id)
        if not bit_data:
            raise WorkerValidationError("bit file is empty")
        if not hwh_data:
            raise WorkerValidationError("hwh file is empty")
        self._validate_digest("bit SHA-256", bit_sha256, bit_data)
        self._validate_digest("hwh SHA-256", hwh_sha256, hwh_data)
        try:
            validate_design(bit_data, hwh_data)
        except HardwareValidationError as exc:
            raise WorkerValidationError(str(exc)) from exc

        async with self.lock:
            if self.recovery_required:
                raise WorkerConflictError("Worker restarted with retained ownership; confirmed hardware recovery is required")
            if self.current_lease_id not in (None, lease_id):
                raise WorkerConflictError("worker already leased")

            with self._journal() as journal:
                if journal.execute("SELECT 1 FROM retired_leases WHERE id=?", (lease_id,)).fetchone():
                    raise WorkerConflictError("lease has been retired")
                if journal.execute("SELECT 1 FROM retired_designs WHERE lease=? AND artifact=?", (lease_id, artifact_id)).fetchone():
                    raise WorkerConflictError("deployment has been superseded")
                if self.current_artifact_id and self.current_artifact_id != artifact_id:
                    journal.execute("INSERT OR IGNORE INTO retired_designs VALUES (?, ?)", (lease_id, self.current_artifact_id))
                journal.execute("INSERT OR REPLACE INTO owner VALUES (1, ?, ?)", (lease_id, artifact_id))
            self.current_lease_id = lease_id
            self.current_artifact_id = artifact_id
            self.fpga_ready = False
            staging = Path(
                tempfile.mkdtemp(prefix="deploy-", dir=self.artifact_root)
            )
            final = self.artifact_root / artifact_id
            try:
                LOGGER.info(
                    "artifact deployment start: board=%s lease_id=%s artifact_id=%s",
                    self.board, lease_id, artifact_id,
                )
                bit_path = staging / "design.bit"
                hwh_path = staging / "design.hwh"
                bit_path.write_bytes(bit_data)
                hwh_path.write_bytes(hwh_data)
                self.fpga_ready = False
                if final.exists():
                    shutil.rmtree(final)
                os.replace(staging, final)
                await self._backend_call(self.backend.load_overlay(final / "design.bit"))
            except Exception:
                self.fpga_ready = False
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            self.current_lease_id = lease_id
            self.current_artifact_id = artifact_id
            self.fpga_ready = True
            self._clean_cache(keep=artifact_id)
            LOGGER.info(
                "overlay and DMA ready: board=%s lease_id=%s artifact_id=%s",
                self.board, lease_id, artifact_id,
            )
            return {
                "ok": True,
                "fpga_ready": True,
                "lease_id": lease_id,
                "artifact_id": artifact_id,
            }

    async def predict(
        self, lease_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.lock:
            if lease_id != self.current_lease_id or not self.fpga_ready:
                raise WorkerConflictError("lease does not own a ready worker")
            try:
                LOGGER.info("predict start: board=%s lease_id=%s", self.board, lease_id)
                result = await self._backend_call(self.backend.predict(payload))
                LOGGER.info("predict complete: board=%s lease_id=%s", self.board, lease_id)
                return result
            except PredictPayloadError:
                # A bad image does not invalidate the loaded Overlay.
                raise
            except FpgaExecutionError:
                self.fpga_ready = False
                LOGGER.exception(
                    "FPGA predict failed: board=%s lease_id=%s", self.board, lease_id
                )
                raise
            except Exception:
                self.fpga_ready = False
                raise

    async def release(self, lease_id: str) -> dict[str, Any]:
        async with self.lock:
            self._validate_id("lease_id", lease_id)
            if self.recovery_required:
                raise WorkerConflictError("Worker restarted with retained ownership; confirmed hardware recovery is required")
            if self.current_lease_id not in (None, lease_id):
                raise WorkerConflictError("lease does not own worker")
            self.fpga_ready = False
            # Fence before cleanup: a failed/lost release response must never
            # permit a delayed deploy to resurrect this lease, including reboot.
            with self._journal() as journal:
                journal.execute("INSERT OR IGNORE INTO retired_leases VALUES (?)", (lease_id,))
            await self._backend_call(self.backend.release())
            with self._journal() as journal:
                journal.execute("DELETE FROM owner WHERE singleton=1")
            self.current_lease_id = None
            self.current_artifact_id = None
            self.fpga_ready = False
            self._clean_cache()
            return {"ok": True, "lease_id": lease_id}

    def status(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "fpga_ready": self.fpga_ready,
            "lease_id": self.current_lease_id,
            "artifact_id": self.current_artifact_id,
        }
