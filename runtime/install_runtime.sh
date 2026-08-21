#!/usr/bin/env bash
# Runtime Factory entry point. Run only on a booted ARM64 KV260 target.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
CHECK_DIR="$PROJECT_DIR/scripts"

die() {
  echo "Runtime deployment failed: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ $(uname -m) == "aarch64" ]] || die "Runtime 只能在 ARM64 KV260 上运行"
[[ $(uname -r) == *xilinx* ]] || die "当前内核不是 Xilinx kernel: $(uname -r)"
[[ -d "$CHECK_DIR" ]] || die "找不到检查脚本目录: $CHECK_DIR"

if command -v cloud-init >/dev/null 2>&1; then
  cloud-init status --wait
fi

echo "========================================"
echo "KV260 Runtime Factory"
echo "========================================"
echo "Hostname: $(hostname)"
echo "System: $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
echo "Kernel: $(uname -a)"
echo "Architecture: $(uname -m)"

printf '\n[1/5] XRT package\n'
if ! command -v xrt-smi >/dev/null 2>&1; then
  xrt_package="${XRT_PACKAGE:-xrt}"
  echo "Installing XRT package: $xrt_package"
  apt-get update
  apt-get install -y "$xrt_package"
fi

printf '\n[2/5] ZOCL\n'
"$SCRIPT_DIR/install_zocl.sh"

printf '\n[3/5] XRT, ZOCL and FPGA/XMUtil checks\n'
"$CHECK_DIR/check_zocl.sh"
"$CHECK_DIR/check_xrt.sh"
"$CHECK_DIR/check_fpga.sh"

printf '\n[4/5] Minimal PYNQ Runtime\n'
"$SCRIPT_DIR/install_pynq.sh"

printf '\n[5/5] Final node report\n'
"$CHECK_DIR/kv260_check.sh"

echo "========================================"
echo "KV260 Runtime Factory Complete"
echo "========================================"
