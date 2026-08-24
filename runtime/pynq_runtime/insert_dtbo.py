#!/usr/bin/env python3
"""Idempotently insert the minimal PYNQ zocl device-tree fragment."""

from pathlib import Path

from pynq import DeviceTreeSegment


DT_ROOT = Path("/proc/device-tree")
OVERLAY_DIR = Path("/sys/kernel/config/device-tree/overlays/pynq")
DTBO = Path(__file__).with_name("pynq.dtbo")


def live_tree_has_zocl() -> bool:
    for compatible in DT_ROOT.rglob("compatible"):
        try:
            values = compatible.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"xlnx,zocl" in values:
            return True
    return False


if live_tree_has_zocl():
    print("PYNQ device-tree runtime: xlnx,zocl already present")
elif OVERLAY_DIR.exists():
    status_file = OVERLAY_DIR / "status"
    status = status_file.read_text(encoding="ascii").strip() if status_file.exists() else "unknown"
    raise RuntimeError(
        f"PYNQ overlay directory exists but live xlnx,zocl is absent (status={status})"
    )
else:
    segment = DeviceTreeSegment(str(DTBO))
    segment.insert()
    if not live_tree_has_zocl():
        raise RuntimeError("pynq.dtbo reported applied but xlnx,zocl is absent")
    print("PYNQ device-tree runtime: applied")
