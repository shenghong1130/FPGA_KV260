#!/usr/bin/env python3
"""Idempotently insert the complete KV260 Minimal PYNQ runtime overlay."""

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


def live_tree_has_pynq_board() -> bool:
    marker = DT_ROOT / "chosen" / "pynq_board"
    try:
        value = marker.read_bytes().split(b"\0", 1)[0].decode("ascii")
    except (OSError, UnicodeDecodeError):
        return False
    return value == "KV260"


def runtime_status() -> tuple[bool, bool]:
    return live_tree_has_zocl(), live_tree_has_pynq_board()


zocl_present, board_present = runtime_status()
if zocl_present and board_present:
    print("PYNQ device-tree runtime: already complete (xlnx,zocl; pynq_board=KV260)")
elif OVERLAY_DIR.exists():
    status_file = OVERLAY_DIR / "status"
    status = status_file.read_text(encoding="ascii").strip() if status_file.exists() else "unknown"
    print(
        "PYNQ managed overlay is incomplete; replacing it: "
        f"zocl={zocl_present} pynq_board={board_present} status={status}"
    )
    try:
        # This exact directory is owned by this script.  Never remove or alter
        # any other configfs overlay while repairing an older PYNQ runtime.
        OVERLAY_DIR.rmdir()
    except OSError as error:
        raise RuntimeError(
            "PYNQ overlay exists but runtime is incomplete: "
            f"zocl={zocl_present} pynq_board={board_present}; "
            f"cannot safely replace {OVERLAY_DIR}: {error}"
        ) from error
elif zocl_present or board_present:
    raise RuntimeError(
        "PYNQ runtime is incomplete but is not owned by the managed overlay "
        f"{OVERLAY_DIR}: zocl={zocl_present} pynq_board={board_present}; "
        "refusing to alter another device-tree overlay"
    )

if not (zocl_present and board_present):
    if OVERLAY_DIR.exists():
        raise RuntimeError(f"managed overlay directory still exists: {OVERLAY_DIR}")
    if not DTBO.is_file():
        raise FileNotFoundError(f"PYNQ runtime DTBO is missing: {DTBO}")

    segment = DeviceTreeSegment(str(DTBO))
    segment.insert()
    zocl_present, board_present = runtime_status()
    if not (zocl_present and board_present):
        raise RuntimeError(
            "pynq.dtbo reported applied but runtime is incomplete: "
            f"zocl={zocl_present} pynq_board={board_present}"
        )
    print("PYNQ device-tree runtime: applied (xlnx,zocl; pynq_board=KV260)")
