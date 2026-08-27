from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
from pathlib import Path
from typing import Any, Callable, Protocol


CLASS_NAMES = [
    "白莲花", "雏菊", "荷花", "菊花", "腊梅", "兰花", "玫瑰花", "水仙花",
    "桃花", "樱花", "鸢尾花", "紫荆花",
]
API_FLOWER_NAMES = [
    "bailianhua", "chuju", "hehua", "juhua", "lamei", "lanhua",
    "meiguihua", "shuixianhua", "taohua", "yinghua", "yuanweihua",
    "zijinghua",
]
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png"}


class PredictPayloadError(ValueError):
    """The request image does not match the public prediction contract."""


class FpgaExecutionError(RuntimeError):
    """The configured FPGA data path failed and must be redeployed."""


class FpgaBackendProtocol(Protocol):
    async def initialize(self) -> None: ...

    async def load_overlay(self, bit_path: Path) -> None: ...

    async def predict(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def release(self) -> None: ...


class PynqFpgaBackend:
    """PYNQ flower classifier using the verified AXI DMA hardware ABI."""

    def __init__(
        self,
        *,
        overlay_factory: Callable[[str], Any] | None = None,
        allocate_factory: Callable[..., Any] | None = None,
        max_image_size: int | None = None,
    ) -> None:
        self.overlay_factory = overlay_factory
        self.allocate_factory = allocate_factory
        self.max_image_size = (
            max_image_size if max_image_size is not None else int(
                os.getenv("KV260_WORKER_MAX_IMAGE_SIZE", str(8 * 1024 * 1024))
            )
        )
        self.overlay: Any = None
        self.dma: Any = None
        self.x_buffer: Any = None
        self.y_buffer: Any = None

    async def initialize(self) -> None:
        if self.overlay_factory is not None and self.allocate_factory is not None:
            return
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
            name for name in ("device", "bo", "kernel") if not hasattr(pyxrt, name)
        ]
        if missing:
            raise RuntimeError(f"pyxrt API missing: {', '.join(missing)}")

    async def load_overlay(self, bit_path: Path) -> None:
        if self.overlay_factory is not None and self.allocate_factory is not None:
            self._load_overlay_sync(bit_path)
        else:
            await asyncio.to_thread(self._load_overlay_sync, bit_path)

    @staticmethod
    def _free_buffer(buffer: Any) -> None:
        if buffer is not None and hasattr(buffer, "freebuffer"):
            buffer.freebuffer()

    def _clear_runtime(self) -> None:
        buffers = (self.x_buffer, self.y_buffer)
        self.x_buffer = None
        self.y_buffer = None
        self.dma = None
        self.overlay = None
        first_error: Exception | None = None
        for buffer in buffers:
            try:
                self._free_buffer(buffer)
            except Exception as exc:  # cleanup both CMA buffers before reporting
                first_error = first_error or exc
        if first_error is not None:
            raise RuntimeError(f"failed to release a PYNQ DMA buffer: {first_error}") from first_error

    def _load_overlay_sync(self, bit_path: Path) -> None:
        import numpy as np

        hwh_path = bit_path.with_suffix(".hwh")
        if not hwh_path.is_file():
            raise RuntimeError(f"matching HWH file is unavailable: {hwh_path}")

        overlay_factory = self.overlay_factory
        allocate_factory = self.allocate_factory
        if overlay_factory is None or allocate_factory is None:
            from pynq import Overlay, allocate

            overlay_factory = Overlay
            allocate_factory = allocate

        self._clear_runtime()
        x_buffer: Any = None
        y_buffer: Any = None
        try:
            overlay = overlay_factory(str(bit_path))
            ip_dict = getattr(overlay, "ip_dict", None)
            if not isinstance(ip_dict, dict) or "axi_dma_0" not in ip_dict:
                raise RuntimeError("HWH does not expose required IP axi_dma_0")
            dma = getattr(overlay, "axi_dma_0", None)
            if dma is None:
                raise RuntimeError("Overlay does not expose axi_dma_0")
            if bool(getattr(dma, "_sg", False)):
                raise RuntimeError("axi_dma_0 must use Simple DMA, not Scatter Gather")
            if not hasattr(dma, "sendchannel") or not hasattr(dma, "recvchannel"):
                raise RuntimeError("axi_dma_0 send/receive channels are unavailable")
            x_buffer = allocate_factory(shape=(3, 28, 28), dtype=np.float32)
            y_buffer = allocate_factory(shape=(12,), dtype=np.float32)
        except Exception:
            self._free_buffer(x_buffer)
            self._free_buffer(y_buffer)
            self._clear_runtime()
            raise

        self.overlay = overlay
        self.dma = dma
        self.x_buffer = x_buffer
        self.y_buffer = y_buffer

    async def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.overlay_factory is not None and self.allocate_factory is not None:
            return self._predict_sync(payload)
        return await asyncio.to_thread(self._predict_sync, payload)

    def _decode_image(self, payload: dict[str, Any]) -> Any:
        from PIL import Image, UnidentifiedImageError

        encoded = payload.get("image_base64")
        content_type = payload.get("content_type")
        if not isinstance(encoded, str) or not encoded:
            raise PredictPayloadError("payload.image_base64 is required")
        if content_type not in SUPPORTED_IMAGE_TYPES:
            raise PredictPayloadError("content_type must be image/jpeg or image/png")
        if len(encoded) > ((self.max_image_size + 2) // 3 * 4 + 4):
            raise PredictPayloadError(
                f"decoded image exceeds {self.max_image_size} bytes"
            )
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PredictPayloadError("image_base64 is not valid strict base64") from exc
        if not image_bytes:
            raise PredictPayloadError("decoded image is empty")
        if len(image_bytes) > self.max_image_size:
            raise PredictPayloadError(
                f"decoded image exceeds {self.max_image_size} bytes"
            )
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
            expected_format = "JPEG" if content_type == "image/jpeg" else "PNG"
            if image.format != expected_format:
                raise PredictPayloadError(
                    f"image bytes do not match declared {content_type}"
                )
            return image.convert("RGB")
        except PredictPayloadError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise PredictPayloadError("image payload is not a valid JPEG or PNG") from exc

    @staticmethod
    def _preprocess(image: Any) -> Any:
        import numpy as np

        resized = image.resize((28, 28))
        values = np.asarray(resized, dtype=np.float32).transpose(2, 0, 1)
        for channel in range(values.shape[0]):
            std = float(np.std(values[channel]))
            if std > 0:
                values[channel] = (
                    values[channel] - np.mean(values[channel])
                ) / std
            else:
                values[channel] = 0
        return values

    def _predict_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        import numpy as np

        image = self._decode_image(payload)
        values = self._preprocess(image)
        if self.dma is None or self.x_buffer is None or self.y_buffer is None:
            raise FpgaExecutionError("FPGA overlay and DMA buffers are not ready")
        try:
            np.copyto(self.x_buffer, values)
            if hasattr(self.x_buffer, "flush"):
                self.x_buffer.flush()
            # Verified order: arm receive before starting transmit.
            self.dma.recvchannel.transfer(self.y_buffer)
            self.dma.sendchannel.transfer(self.x_buffer)
            self.dma.sendchannel.wait()
            self.dma.recvchannel.wait()
            if hasattr(self.y_buffer, "invalidate"):
                self.y_buffer.invalidate()
            class_index = int(np.argmax(self.y_buffer))
            confidence = float(self.y_buffer[class_index])
        except Exception as exc:
            try:
                self._clear_runtime()
            except RuntimeError as cleanup_exc:
                raise FpgaExecutionError(
                    f"FPGA DMA execution failed: {exc}; cleanup failed: {cleanup_exc}"
                ) from exc
            raise FpgaExecutionError(f"FPGA DMA execution failed: {exc}") from exc

        flower_cn = CLASS_NAMES[class_index]
        flower_api = API_FLOWER_NAMES[class_index]
        return {
            "ok": True,
            "status": "success",
            "predicted_class": flower_api,
            "flower": flower_api,
            "flower_api": flower_api,
            "flower_cn": flower_cn,
            "raw_class": flower_cn,
            "class_index": class_index,
            "confidence": confidence,
        }

    async def release(self) -> None:
        # PL may retain its bitstream; process-owned CMA buffers are released.
        if self.overlay_factory is not None and self.allocate_factory is not None:
            self._clear_runtime()
        else:
            await asyncio.to_thread(self._clear_runtime)
