from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol


class PredictAdapterNotConfigured(RuntimeError):
    pass


class FpgaBackendProtocol(Protocol):
    async def initialize(self) -> None: ...

    async def load_overlay(self, bit_path: Path) -> None: ...

    async def predict(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def release(self) -> None: ...


class PynqFpgaBackend:
    """Minimal PYNQ backend; application-specific predict remains pluggable."""

    def __init__(self) -> None:
        self.overlay: Any = None

    async def initialize(self) -> None:
        if not Path("/sys/class/fpga_manager/fpga0").exists():
            raise RuntimeError("FPGA Manager fpga0 is unavailable")

        import pyxrt
        from pynq import Device
        from pynq.ps import ON_TARGET

        if not ON_TARGET:
            raise RuntimeError(
                "PYNQ target marker missing: /proc/device-tree/chosen/pynq_board"
            )
        if type(Device.active_device).__name__ != "EmbeddedDevice":
            raise RuntimeError(
                "PYNQ active device is not EmbeddedDevice: "
                f"{type(Device.active_device).__name__}"
            )
        missing = [
            name for name in ("device", "bo", "kernel")
            if not hasattr(pyxrt, name)
        ]
        if missing:
            raise RuntimeError(f"pyxrt API missing: {', '.join(missing)}")

    async def load_overlay(self, bit_path: Path) -> None:
        self.overlay = await asyncio.to_thread(self._load_overlay, bit_path)

    @staticmethod
    def _load_overlay(bit_path: Path) -> Any:
        from pynq import Overlay

        return Overlay(str(bit_path))

    async def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        raise PredictAdapterNotConfigured("FPGA predict adapter not configured")

    async def release(self) -> None:
        # The configured overlay may remain in PL. A later deployment replaces it.
        return None
