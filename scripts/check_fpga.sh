#!/usr/bin/env bash
# Read-only FPGA Manager and XMUtil inspection for a booted KV260.
set -Eeuo pipefail

[[ -d /sys/class/fpga_manager ]] || {
  echo "FPGA Manager check: FAIL (/sys/class/fpga_manager is unavailable)" >&2
  exit 1
}

echo "FPGA Manager:"
ls -l /sys/class/fpga_manager/

command -v xmutil >/dev/null || {
  echo "WARNING: xmutil is not installed (optional platform diagnostic)"
  echo "FPGA Manager check: OK"
  exit 0
}

echo "XMUtil applications:"
if ! xmutil listapps; then
  echo "WARNING: xmutil listapps failed (optional platform diagnostic)"
fi
echo "XMUtil examine:"
if ! xmutil examine; then
  echo "WARNING: xmutil examine failed (optional platform diagnostic)"
fi
echo "FPGA Manager check: OK"
