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
[[ -e /sys/class/fpga_manager/fpga0 ]] || die "找不到 FPGA Manager fpga0"
[[ -d "$CHECK_DIR" ]] || die "找不到检查脚本目录: $CHECK_DIR"

if command -v cloud-init >/dev/null 2>&1; then
  # cloud-init exit code 2 means it completed with recoverable warnings;
  # those warnings must not block the Runtime Factory.
  set +e
  cloud-init status --wait
  CLOUD_INIT_RC=$?
  set -e

  case "$CLOUD_INIT_RC" in
    0)
      ;;
    2)
      echo "WARNING: cloud-init completed with recoverable warnings, continue runtime installation"
      ;;
    *)
      die "cloud-init failed with exit code $CLOUD_INIT_RC"
      ;;
  esac
fi

echo "========================================"
echo "KV260 Runtime Factory"
echo "========================================"
echo "Hostname: $(hostname)"
echo "System: $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
echo "Kernel: $(uname -a)"
echo "Architecture: $(uname -m)"

printf '\n[1/6] System and FPGA Manager\n'
echo "System: OK"
echo "aarch64: OK"
echo "Xilinx Kernel: OK"
echo "FPGA Manager: OK ($(</sys/class/fpga_manager/fpga0/state))"

printf '\n[2/6] XRT userspace\n'
"$SCRIPT_DIR/install_xrt.sh"

printf '\n[3/6] XRT-matched ZOCL DKMS driver\n'
set +e
"$SCRIPT_DIR/install_zocl.sh"
zocl_rc=$?
set -e
if (( zocl_rc == 75 )); then
  echo "REBOOT_REQUIRED: Runtime Stage 1 complete; reboot into the newer Xilinx kernel"
  exit 75
elif (( zocl_rc != 0 )); then
  die "ZOCL 安装失败，exit=$zocl_rc"
fi

printf '\n[4/6] Driver and platform diagnostics\n'
"$CHECK_DIR/check_zocl.sh"
"$CHECK_DIR/check_xrt.sh"
"$CHECK_DIR/check_fpga.sh"

printf '\n[5/6] Minimal Kria-PYNQ Runtime\n'
"$SCRIPT_DIR/install_pynq.sh"

printf '\n[6/6] Final PYNQ worker runtime report\n'
"$CHECK_DIR/kv260_check.sh"

echo "========================================"
echo "KV260 Runtime Factory Complete"
echo "========================================"
