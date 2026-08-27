from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from worker.app.fpga import (
    API_FLOWER_NAMES,
    CLASS_NAMES,
    FpgaExecutionError,
    PredictPayloadError,
    PynqFpgaBackend,
)


class FakeBuffer(np.ndarray):
    def __new__(cls, shape: tuple[int, ...], dtype: Any):
        instance = np.zeros(shape, dtype=dtype).view(cls)
        instance.flushed = False
        instance.invalidated = False
        instance.freed = False
        return instance

    def flush(self) -> None:
        self.flushed = True

    def invalidate(self) -> None:
        self.invalidated = True

    def freebuffer(self) -> None:
        self.freed = True


class FakeChannel:
    def __init__(self, name: str, events: list[str], fail_wait: bool = False) -> None:
        self.name = name
        self.events = events
        self.buffer: Any = None
        self.fail_wait = fail_wait

    def transfer(self, buffer: Any) -> None:
        self.buffer = buffer
        self.events.append(f"{self.name}.transfer")

    def wait(self) -> None:
        self.events.append(f"{self.name}.wait")
        if self.fail_wait:
            raise RuntimeError("DMA channel failed")


class FakeDma:
    def __init__(self, events: list[str], *, sg: bool = False,
                 fail_wait: bool = False) -> None:
        self._sg = sg
        self.sendchannel = FakeChannel("send", events, fail_wait=fail_wait)
        self.recvchannel = FakeChannel("recv", events)


class FakeOverlay:
    def __init__(self, dma: FakeDma, *, include_dma: bool = True) -> None:
        self.ip_dict = {"axi_dma_0": {}} if include_dma else {}
        self.axi_dma_0 = dma


def png_payload(*, constant: bool = False) -> dict[str, str]:
    if constant:
        pixels = np.full((36, 40, 3), 19, dtype=np.uint8)
    else:
        pixels = np.arange(36 * 40 * 3, dtype=np.uint8).reshape((36, 40, 3))
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return {
        "image_base64": base64.b64encode(output.getvalue()).decode("ascii"),
        "content_type": "image/png",
    }


def make_backend(tmp_path: Path, *, include_dma: bool = True,
                 sg: bool = False, fail_wait: bool = False):
    events: list[str] = []
    dma = FakeDma(events, sg=sg, fail_wait=fail_wait)
    overlay = FakeOverlay(dma, include_dma=include_dma)
    buffers: list[FakeBuffer] = []

    def allocate(*, shape, dtype):
        buffer = FakeBuffer(shape, dtype)
        buffers.append(buffer)
        return buffer

    backend = PynqFpgaBackend(
        overlay_factory=lambda _: overlay,
        allocate_factory=allocate,
    )
    bit = tmp_path / "design.bit"
    bit.write_bytes(b"bit")
    bit.with_suffix(".hwh").write_text("<SYSTEM/>", encoding="utf-8")
    return backend, dma, events, buffers, bit


@pytest.mark.asyncio
async def test_overlay_dma_buffers_preprocess_and_result(tmp_path: Path) -> None:
    backend, dma, events, buffers, bit = make_backend(tmp_path)
    await backend.initialize()
    await backend.load_overlay(bit)
    dma.recvchannel.wait = lambda: (
        events.append("recv.wait"),
        dma.recvchannel.buffer.__setitem__(slice(None), np.arange(12, dtype=np.float32)),
    )[-1]

    result = await backend.predict(png_payload())

    assert buffers[0].shape == (3, 28, 28)
    assert buffers[0].dtype == np.float32
    assert buffers[1].shape == (12,)
    assert buffers[0].flushed is True
    assert buffers[1].invalidated is True
    assert events == ["recv.transfer", "send.transfer", "send.wait", "recv.wait"]
    assert result == {
        "ok": True,
        "status": "success",
        "predicted_class": API_FLOWER_NAMES[11],
        "flower": API_FLOWER_NAMES[11],
        "flower_api": API_FLOWER_NAMES[11],
        "flower_cn": CLASS_NAMES[11],
        "raw_class": CLASS_NAMES[11],
        "class_index": 11,
        "confidence": 11.0,
    }
    assert "image_base64" not in result


@pytest.mark.asyncio
async def test_constant_channels_are_zero(tmp_path: Path) -> None:
    backend, _, _, buffers, bit = make_backend(tmp_path)
    await backend.load_overlay(bit)
    await backend.predict(png_payload(constant=True))
    assert np.all(buffers[0] == 0)


@pytest.mark.asyncio
async def test_rgb_chw_resize_and_channel_zscore(tmp_path: Path) -> None:
    backend, _, _, buffers, bit = make_backend(tmp_path)
    await backend.load_overlay(bit)
    payload = png_payload()
    image = backend._decode_image(payload)
    expected = backend._preprocess(image)
    await backend.predict(payload)
    assert expected.shape == (3, 28, 28)
    assert np.allclose(buffers[0], expected)
    assert np.allclose(np.mean(buffers[0], axis=(1, 2)), 0, atol=1e-5)
    assert np.allclose(np.std(buffers[0], axis=(1, 2)), 1, atol=1e-5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"image_base64": "%%%", "content_type": "image/png"},
        {"image_base64": base64.b64encode(b"bad").decode(), "content_type": "image/gif"},
        {"image_base64": base64.b64encode(b"bad").decode(), "content_type": "image/png"},
    ],
)
async def test_payload_validation(tmp_path: Path, payload: dict[str, str]) -> None:
    backend, _, _, _, bit = make_backend(tmp_path)
    await backend.load_overlay(bit)
    with pytest.raises(PredictPayloadError):
        await backend.predict(payload)


@pytest.mark.asyncio
async def test_rejects_missing_dma_and_scatter_gather(tmp_path: Path) -> None:
    backend, _, _, _, bit = make_backend(tmp_path, include_dma=False)
    with pytest.raises(RuntimeError, match="axi_dma_0"):
        await backend.load_overlay(bit)
    assert backend.overlay is backend.dma is None
    assert backend.x_buffer is backend.y_buffer is None
    backend, _, _, _, bit = make_backend(tmp_path, sg=True)
    with pytest.raises(RuntimeError, match="Simple DMA"):
        await backend.load_overlay(bit)


@pytest.mark.asyncio
async def test_dma_failure_invalidates_backend_and_frees_buffers(tmp_path: Path) -> None:
    backend, _, _, buffers, bit = make_backend(tmp_path, fail_wait=True)
    await backend.load_overlay(bit)
    with pytest.raises(FpgaExecutionError, match="DMA execution failed"):
        await backend.predict(png_payload())
    assert backend.dma is None
    assert all(buffer.freed for buffer in buffers)


@pytest.mark.asyncio
async def test_release_uses_freebuffer(tmp_path: Path) -> None:
    backend, _, _, buffers, bit = make_backend(tmp_path)
    await backend.load_overlay(bit)
    await backend.release()
    assert all(buffer.freed for buffer in buffers)
    assert backend.overlay is None


@pytest.mark.asyncio
async def test_predict_many_does_not_reload_and_new_overlay_frees_old_buffers(
    tmp_path: Path,
) -> None:
    backend, _, _, first_buffers, bit = make_backend(tmp_path)
    load_count = 0
    original_factory = backend.overlay_factory

    def counting_overlay(path: str):
        nonlocal load_count
        load_count += 1
        return original_factory(path)

    backend.overlay_factory = counting_overlay
    await backend.load_overlay(bit)
    await backend.predict(png_payload())
    await backend.predict(png_payload())
    await backend.predict(png_payload())
    assert load_count == 1
    await backend.load_overlay(bit)
    assert load_count == 2
    assert all(buffer.freed for buffer in first_buffers[:2])


@pytest.mark.asyncio
async def test_partial_buffer_allocation_failure_cleans_first_buffer(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    overlay = FakeOverlay(FakeDma(events))
    allocated: list[FakeBuffer] = []

    def partial_allocate(*, shape, dtype):
        if allocated:
            raise MemoryError("output allocation failed")
        buffer = FakeBuffer(shape, dtype)
        allocated.append(buffer)
        return buffer

    backend = PynqFpgaBackend(
        overlay_factory=lambda _: overlay,
        allocate_factory=partial_allocate,
    )
    bit = tmp_path / "design.bit"
    bit.write_bytes(b"bit")
    bit.with_suffix(".hwh").write_text("<SYSTEM/>", encoding="utf-8")
    with pytest.raises(MemoryError, match="output allocation failed"):
        await backend.load_overlay(bit)
    assert allocated[0].freed is True
    assert backend.overlay is backend.dma is None
    assert backend.x_buffer is backend.y_buffer is None
