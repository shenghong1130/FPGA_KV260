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
# /etc/os-release is the fixed system metadata path.
# shellcheck disable=SC1091
echo "System: $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
echo "Kernel: $(uname -a)"
echo "Architecture: $(uname -m)"

printf '\n[preflight] System and FPGA Manager\n'
echo "System: OK"
echo "aarch64: OK"
echo "Xilinx Kernel: OK"
echo "FPGA Manager: OK ($(</sys/class/fpga_manager/fpga0/state))"

printf '\n[1/7] XRT userspace\n'
"$SCRIPT_DIR/install_xrt.sh"

printf '\n[2/7] XRT-matched ZOCL DKMS driver\n'
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

printf '\n[3/7] XRT-matched Python 3.12 pyxrt binding\n'
"$SCRIPT_DIR/install_pyxrt.sh"

printf '\n[4/7] Minimal PYNQ 3.1.2 packages and runtime assets\n'
PYNQ_DEFER_ACTIVATION=1 "$SCRIPT_DIR/install_pynq.sh"

printf '\n[5/7] PYNQ device tree and boot services\n'
systemctl restart kv260-pynq-dt.service kv260-pynq-clear-pl-state.service
systemctl is-active --quiet kv260-pynq-dt.service || \
  die "kv260-pynq-dt.service 未处于 active 状态"

printf '\n[6/7] Minimal PYNQ functional validation\n'
pynq_venv="${PYNQ_VENV:-/opt/kv260-pynq}"
pynq_share="$pynq_venv/share/kv260-runtime"
overlay_dir="${PYNQ_OVERLAY_DIR:-/opt/fpga}"
overlay_name="${PYNQ_OVERLAY_NAME:-design}"
bit_path="$overlay_dir/${overlay_name}.bit"
hwh_path="$overlay_dir/${overlay_name}.hwh"
validation_args=()
if [[ -f "$bit_path" && -f "$hwh_path" ]]; then
  validation_args=(--bit "$bit_path")
elif [[ -e "$bit_path" || -e "$hwh_path" ]]; then
  die "Overlay 文件不完整；必须同时提供 $bit_path 和 $hwh_path"
fi
BOARD=KV260 XILINX_XRT=/usr \
  "$pynq_venv/bin/python" "$pynq_share/validate_runtime.py" "${validation_args[@]}"
echo "Minimal Kria-PYNQ Runtime Installation: OK"

printf '\n[7/7] Final diagnostics and worker runtime report\n'
"$CHECK_DIR/check_zocl.sh"
"$CHECK_DIR/check_xrt.sh"
"$CHECK_DIR/check_fpga.sh"
"$CHECK_DIR/kv260_check.sh"

echo "========================================"
echo "KV260 Runtime Factory Complete"
echo "========================================"
