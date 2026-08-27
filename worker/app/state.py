from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

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
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        await self.backend.initialize()

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
            ET.fromstring(hwh_data)
        except ET.ParseError as exc:
            raise WorkerValidationError(f"invalid HWH XML: {exc}") from exc

        async with self.lock:
            if self.current_lease_id not in (None, lease_id):
                raise WorkerConflictError("worker already leased")

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
                await self.backend.load_overlay(final / "design.bit")
            except Exception:
                self.fpga_ready = False
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            self.current_lease_id = lease_id
            self.current_artifact_id = artifact_id
            self.fpga_ready = True
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
                result = await self.backend.predict(payload)
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
            if lease_id != self.current_lease_id:
                raise WorkerConflictError("lease does not own worker")
            await self.backend.release()
            self.current_lease_id = None
            self.current_artifact_id = None
            self.fpga_ready = False
            return {"ok": True, "lease_id": lease_id}

    def status(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "fpga_ready": self.fpga_ready,
            "lease_id": self.current_lease_id,
            "artifact_id": self.current_artifact_id,
        }
