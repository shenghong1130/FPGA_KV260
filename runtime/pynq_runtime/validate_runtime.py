#!/usr/bin/env python3
"""Functional validation for the KV260 Minimal PYNQ runtime."""

import argparse
from pathlib import Path

import numpy as np
import pynq
import pynqmetadata
import pynqutils
from pynq import MMIO, Overlay, allocate
from pynq.pl_server import Device
from pynq.ps import ON_TARGET


def package_version(module) -> str:
    return getattr(module, "__version__", "unknown")


parser = argparse.ArgumentParser()
parser.add_argument("--bit", type=Path)
args = parser.parse_args()

print(f"PYNQ version: {package_version(pynq)}")
print(f"pynqmetadata version: {package_version(pynqmetadata)}")
print(f"pynqutils version: {package_version(pynqutils)}")
print(f"PYNQ ON_TARGET: {ON_TARGET}")
print("Overlay import: OK")
print("MMIO import: OK")

if not ON_TARGET:
    raise RuntimeError(
        "PYNQ target marker missing: /proc/device-tree/chosen/pynq_board"
    )

overlay = None
if args.bit is not None:
    hwh = args.bit.with_suffix(".hwh")
    if not args.bit.is_file() or not hwh.is_file():
        raise FileNotFoundError(f"matching design files required: {args.bit} and {hwh}")
    overlay = Overlay(str(args.bit), download=True)
    print(f"Overlay Load: OK ({args.bit})")
    print(f"HWH Parse: OK ({len(overlay.ip_dict)} IP entries)")
    print(f"IP dictionary: {overlay.ip_dict}")

device = Device.active_device
device_type = type(device).__name__
print(f"Device: {device_type}")
if device_type != "EmbeddedDevice":
    raise RuntimeError(
        f"PYNQ active device must be EmbeddedDevice on KV260, got {device_type}"
    )
try:
    buffer = allocate(shape=(1024,), dtype=np.uint32)
    allocation_mode = "overlay/default memory"
except RuntimeError as error:
    if args.bit is not None or "Overlay is not downloaded" not in str(error):
        raise
    # Before an application overlay is available PYNQ has no HWH memory
    # topology.  Exercise the same public allocate() API and XRT BO backend
    # against the KV260 PS DDR bank explicitly; a real Overlay supplies this
    # target automatically from its matching HWH.
    ps_ddr = device.get_memory(
        {"idx": 0, "base_address": 0, "size": 0, "tag": "PSDDR"}
    )
    buffer = allocate(shape=(1024,), dtype=np.uint32, target=ps_ddr)
    allocation_mode = "bootstrap PS DDR target (design files unavailable)"

try:
    buffer[:] = np.arange(buffer.size, dtype=buffer.dtype)
    buffer.flush()
    buffer.invalidate()
    if int(buffer[17]) != 17 or not int(buffer.device_address):
        raise RuntimeError("allocated buffer readback/address validation failed")
    print(
        f"allocate: OK ({allocation_mode}; bytes={buffer.nbytes}; "
        f"device_address=0x{int(buffer.device_address):x})"
    )
finally:
    buffer.freebuffer()

if overlay is None:
    print("Overlay Hardware Test: NOT RUN (design files unavailable)")
else:
    dma_names = [
        name
        for name, description in overlay.ip_dict.items()
        if "axi_dma" in str(description.get("type", "")).lower()
    ]
    if dma_names:
        print(f"DMA Discovery: OK ({', '.join(dma_names)})")
    else:
        print("DMA Discovery: NOT PRESENT IN DESIGN")

print("PYNQ Core Runtime: OK")
