#!/usr/bin/env bash
# Runtime Factory entry point. Run only on a booted ARM64 KV260 target.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

die() {
  echo "Runtime deployment failed: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ $(uname -m) == "aarch64" ]] || die "Runtime 只能在 ARM64 KV260 上运行"

if command -v cloud-init >/dev/null 2>&1; then
  cloud-init status --wait
fi

echo "========================================"
echo "KV260 Runtime Factory"
echo "========================================"
echo "Hostname: $(hostname)"
echo "Kernel: $(uname -r)"
echo "Architecture: $(uname -m)"

echo "\n[1/4] XRT package"
if ! command -v xrt-smi >/dev/null 2>&1; then
  xrt_package="${XRT_PACKAGE:-xrt}"
  echo "Installing XRT package: $xrt_package"
  apt-get update
  apt-get install -y "$xrt_package"
fi

echo "\n[2/4] ZOCL"
"$SCRIPT_DIR/install_zocl.sh"

echo "\n[3/4] XRT and FPGA/XMUtil"
"$SCRIPT_DIR/check_xrt.sh"
"$SCRIPT_DIR/check_fpga.sh"

echo "\n[4/4] Minimal PYNQ Runtime"
"$SCRIPT_DIR/install_pynq.sh"

echo "========================================"
echo "KV260 Runtime Factory Complete"
echo "========================================"
