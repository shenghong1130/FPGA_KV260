#!/usr/bin/env bash
# Read-only FPGA, XRT and XMUtil inspection for the KV260 target.
set -Eeuo pipefail

[[ -d /sys/class/fpga_manager ]] || {
  echo "FPGA Manager check: /sys/class/fpga_manager is unavailable" >&2
  exit 1
}

echo "FPGA Manager:"
ls -l /sys/class/fpga_manager/

command -v xmutil >/dev/null || {
  echo "XMUtil check: xmutil is not installed" >&2
  exit 1
}

echo "XMUtil applications:"
xmutil listapps
echo "XMUtil examine:"
xmutil examine
