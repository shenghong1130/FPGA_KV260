#!/usr/bin/env bash
# Read-only final validation for a Minimal PYNQ KV260 worker node.
set -Eeuo pipefail

report_status=0
PYNQ_VENV="${PYNQ_VENV:-/opt/kv260-pynq}"
PYNQ_SHARE="$PYNQ_VENV/share/kv260-runtime"
MIN_CMA_MB="${PYNQ_MIN_CMA_MB:-256}"
OVERLAY_DIR="${PYNQ_OVERLAY_DIR:-/opt/fpga}"
OVERLAY_NAME="${PYNQ_OVERLAY_NAME:-design}"
[[ "$MIN_CMA_MB" =~ ^[0-9]+$ ]] || {
  echo "PYNQ_MIN_CMA_MB must be an integer: $MIN_CMA_MB" >&2
  exit 1
}

section() {
  printf '\n%s\n' "$1"
}

required() {
  local name="$1"
  local status="$2"
  local detail="$3"
  printf '%-24s %-5s %s\n' "$name:" "$status" "$detail"
  [[ "$status" == OK ]] || report_status=1
}

diagnostic() {
  printf '%-24s %-11s %s\n' "$1:" "DIAGNOSTIC" "$2"
}

has_zocl_dt_node() {
  local compatible
  local dt_root

  dt_root=$(readlink -f /proc/device-tree) || return 1
  [[ -d "$dt_root" ]] || return 1

  while IFS= read -r -d '' compatible; do
    if grep -azFxq 'xlnx,zocl' "$compatible"; then
      return 0
    fi
  done < <(find "$dt_root" -type f -name compatible -print0 2>/dev/null)
  return 1
}

has_pynq_board_marker() {
  local marker=/proc/device-tree/chosen/pynq_board
  [[ -r "$marker" ]] && [[ "$(tr -d '\0' < "$marker")" == KV260 ]]
}

section "KV260 Minimal PYNQ Runtime Check"

hostname_value=$(hostname)
if [[ "$hostname_value" =~ ^kv260([1-9]|1[0-9]|20)$ ]]; then
  board_id="${BASH_REMATCH[1]}"
  expected_ip="192.168.31.$((81 + board_id))"
  required "Hostname" "OK" "$hostname_value (board $board_id)"
else
  board_id=""
  expected_ip=""
  required "Hostname" "FAIL" "expected kv2601 through kv26020, got $hostname_value"
fi

ip_addresses=$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | paste -sd ', ' -)
default_route=$(ip route show default 2>/dev/null | head -n 1)
if [[ -n "$expected_ip" ]] \
  && ip -o -4 addr show 2>/dev/null | awk '{print $4}' | grep -q "^${expected_ip}/" \
  && [[ "$default_route" == *"via 192.168.31.1"* ]]; then
  required "Network" "OK" "${expected_ip}; ${default_route}"
else
  required "Network" "FAIL" "IPv4: ${ip_addresses:-none}; default: ${default_route:-none}"
fi

# /etc/os-release is the fixed system metadata path.
# shellcheck disable=SC1091
os_name=$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")
# shellcheck disable=SC1091
os_codename=$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-unknown}")
if [[ "$os_name" == Ubuntu* && "$os_codename" == noble ]]; then
  required "System" "OK" "$os_name ($os_codename)"
else
  required "System" "FAIL" "$os_name ($os_codename)"
fi

kernel=$(uname -r)
if [[ "$kernel" == *xilinx* ]]; then
  required "Xilinx Kernel" "OK" "$kernel"
else
  required "Xilinx Kernel" "FAIL" "$kernel"
fi

architecture=$(uname -m)
if [[ "$architecture" == aarch64 ]]; then
  required "Architecture" "OK" "$architecture"
else
  required "Architecture" "FAIL" "$architecture"
fi

cma_kb=$(awk '/^CmaTotal:/ {print $2}' /proc/meminfo)
if [[ "$cma_kb" =~ ^[0-9]+$ ]] && (( cma_kb >= MIN_CMA_MB * 1024 )); then
  required "CMA" "OK" "$((cma_kb / 1024)) MiB (minimum ${MIN_CMA_MB} MiB)"
else
  required "CMA" "FAIL" "${cma_kb:-0} KiB (minimum ${MIN_CMA_MB} MiB)"
fi

fpga_state=""
if [[ -r /sys/class/fpga_manager/fpga0/state ]]; then
  fpga_state=$(< /sys/class/fpga_manager/fpga0/state)
fi
if [[ "$fpga_state" == operating ]]; then
  required "FPGA Manager" "OK" "fpga0 state=$fpga_state"
else
  required "FPGA Manager" "FAIL" "fpga0 state=${fpga_state:-unavailable}"
fi

if ! xrt_version=$(dpkg-query -W -f='${Version}' xrt 2>/dev/null); then
  xrt_version=""
fi
if ! dkms_version=$(dpkg-query -W -f='${Version}' xrt-dkms 2>/dev/null); then
  dkms_version=""
fi
if [[ -n "$xrt_version" && "$xrt_version" == "$dkms_version" ]]; then
  required "XRT / xrt-dkms" "OK" "$xrt_version (exact package match)"
else
  required "XRT / xrt-dkms" "FAIL" "xrt=${xrt_version:-none}; xrt-dkms=${dkms_version:-none}"
fi

if ! module_path=$(find "/lib/modules/$kernel" -type f \
  -name '*zocl*.ko*' -print -quit 2>/dev/null); then
  module_path=""
fi
if [[ -n "$module_path" ]] && grep -q '^zocl ' /proc/modules; then
  required "ZOCL Driver" "OK" "$module_path; loaded"
else
  required "ZOCL Driver" "FAIL" "module=${module_path:-none}; loaded=$(grep -q '^zocl ' /proc/modules && echo yes || echo no)"
fi

if [[ -x "$PYNQ_VENV/bin/python" ]]; then
  set +e
  pyxrt_path=$(
    "$PYNQ_VENV/bin/python" -c \
      'import pyxrt; assert all(hasattr(pyxrt, n) for n in ("device", "bo", "kernel")); print(pyxrt.__file__)' \
      2>/dev/null
  )
  pyxrt_rc=$?
  set -e
  if (( pyxrt_rc == 0 )); then
    required "pyxrt" "OK" "$pyxrt_path"
  else
    required "pyxrt" "FAIL" "venv import/API validation failed"
  fi
else
  required "pyxrt" "FAIL" "$PYNQ_VENV/bin/python is unavailable"
fi

zocl_dt=FAIL
pynq_board=FAIL
dt_service=FAIL
if has_zocl_dt_node; then
  zocl_dt=OK
fi
if has_pynq_board_marker; then
  pynq_board=OK
fi
if systemctl is-active --quiet kv260-pynq-dt.service; then
  dt_service=OK
fi

if [[ "$zocl_dt" == OK && "$pynq_board" == OK && "$dt_service" == OK ]]; then
  required "PYNQ DT/runtime" "OK" "xlnx,zocl live; pynq_board=KV260; service active"
else
  required "PYNQ DT/runtime" "FAIL" \
    "zocl=$zocl_dt pynq_board=$pynq_board service=$dt_service"
fi

if [[ -e /dev/dri/renderD128 ]]; then
  diagnostic "renderD128" "present"
else
  diagnostic "renderD128" "absent; not used as a pass/fail condition"
fi

if command -v xrt-smi >/dev/null 2>&1; then
  xrt_tmp=$(mktemp)
  set +e
  xrt-smi examine 2>&1 | tr -d '\0' > "$xrt_tmp"
  xrt_rc=${PIPESTATUS[0]}
  set -e
  xrt_summary=$(<"$xrt_tmp")
  rm -f "$xrt_tmp"
  diagnostic "xrt-smi" "exit=$xrt_rc; $(printf '%s' "$xrt_summary" | head -n 1)"
else
  diagnostic "xrt-smi" "not installed"
fi

validation_script="$PYNQ_SHARE/validate_runtime.py"
validation_args=()
bit_path="$OVERLAY_DIR/${OVERLAY_NAME}.bit"
hwh_path="$OVERLAY_DIR/${OVERLAY_NAME}.hwh"
if [[ -f "$bit_path" && -f "$hwh_path" ]]; then
  validation_args=(--bit "$bit_path")
elif [[ -e "$bit_path" || -e "$hwh_path" ]]; then
  required "Overlay files" "FAIL" "must provide matching $bit_path and $hwh_path"
fi

if [[ -x "$PYNQ_VENV/bin/python" && -f "$validation_script" ]]; then
  set +e
  pynq_output=$(BOARD=KV260 XILINX_XRT=/usr \
    "$PYNQ_VENV/bin/python" "$validation_script" "${validation_args[@]}" 2>&1)
  pynq_rc=$?
  set -e
  printf '%s\n' "$pynq_output"
  if (( pynq_rc == 0 )); then
    pynq_version=$("$PYNQ_VENV/bin/python" -c 'import pynq; print(pynq.__version__)')
    required "Minimal PYNQ" "OK" "version=$pynq_version; imports and real allocate/free passed"
  else
    required "Minimal PYNQ" "FAIL" "validation exit=$pynq_rc"
  fi
else
  required "Minimal PYNQ" "FAIL" "venv or validation script missing"
fi

if command -v xmutil >/dev/null 2>&1; then
  set +e
  xmutil_summary=$(xmutil listapps 2>&1)
  xmutil_rc=$?
  set -e
  diagnostic "XMUtil" "exit=$xmutil_rc; $(printf '%s' "$xmutil_summary" | head -n 1)"
else
  diagnostic "XMUtil" "not installed"
fi

section "Result"
if (( report_status == 0 )); then
  echo "KV260 PYNQ Worker Runtime: OK"
else
  echo "KV260 PYNQ Worker Runtime: FAIL" >&2
fi
exit "$report_status"
