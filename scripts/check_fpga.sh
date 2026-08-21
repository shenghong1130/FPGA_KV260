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
  echo "XMUtil check: FAIL (xmutil is not installed)" >&2
  exit 1
}

echo "XMUtil applications:"
xmutil listapps
echo "XMUtil examine:"
xmutil examine
echo "FPGA/XMUtil check: OK"
