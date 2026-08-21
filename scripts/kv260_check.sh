#!/usr/bin/env bash
# First-boot read-only validation for a KV260 deployment.
set -u -o pipefail

report_status=0

section() {
  printf '\n%s\n' "$1"
}

report() {
  local name="$1"
  local status="$2"
  local detail="$3"
  printf '%-18s %-5s %s\n' "$name:" "$status" "$detail"
  [[ "$status" == "OK" ]] || report_status=1
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

section "KV260 Check Report"

hostname_value=$(hostname)
if [[ "$hostname_value" =~ ^kv260([1-9]|1[0-9]|20)$ ]]; then
  board_id="${BASH_REMATCH[1]}"
  expected_ip="192.168.31.$((81 + board_id))"
  report "Hostname" "OK" "$hostname_value (board $board_id)"
else
  board_id=""
  expected_ip=""
  report "Hostname" "FAIL" "expected kv2601 through kv26020, got $hostname_value"
fi

ip_addresses=$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | paste -sd ', ' -)
default_route=$(ip route show default 2>/dev/null | head -n 1)
if [[ -n "$expected_ip" ]] && ip -o -4 addr show 2>/dev/null | awk '{print $4}' | grep -q "^${expected_ip}/" \
  && [[ "$default_route" == *"via 192.168.31.1"* ]]; then
  report "Network" "OK" "${expected_ip}; ${default_route}"
else
  report "Network" "FAIL" "IPv4: ${ip_addresses:-none}; default: ${default_route:-none}"
fi

os_name=$(source /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-unknown}")
if [[ "$os_name" == Ubuntu* ]]; then
  report "Ubuntu" "OK" "$os_name"
else
  report "Ubuntu" "FAIL" "$os_name"
fi

kernel=$(uname -r)
if [[ "$kernel" == *xilinx* ]]; then
  report "Kernel" "OK" "$kernel"
else
  report "Kernel" "FAIL" "$kernel"
fi

architecture=$(uname -m)
if [[ "$architecture" == "aarch64" ]]; then
  report "Architecture" "OK" "$architecture"
else
  report "Architecture" "FAIL" "$architecture"
fi

cma_info=$(grep -E '^Cma(Total|Free):' /proc/meminfo 2>/dev/null | paste -sd '; ' -)
if [[ -n "$cma_info" ]]; then
  report "CMA" "OK" "$cma_info"
else
  report "CMA" "FAIL" "CmaTotal/CmaFree not found in /proc/meminfo"
fi

if [[ -d /sys/class/fpga_manager ]] && compgen -G '/sys/class/fpga_manager/*' >/dev/null; then
  fpga_managers=$(find /sys/class/fpga_manager -mindepth 1 -maxdepth 1 -printf '%f ' 2>/dev/null)
  report "FPGA Manager" "OK" "${fpga_managers% }"
else
  report "FPGA Manager" "FAIL" "/sys/class/fpga_manager is unavailable"
fi

if has_command xrt-smi; then
  if xrt_output=$(xrt-smi examine 2>&1); then
    report "XRT" "OK" "xrt-smi examine completed"
  else
    report "XRT" "FAIL" "xrt-smi examine failed: $(printf '%s' "$xrt_output" | head -n 1)"
  fi
else
  report "XRT" "FAIL" "xrt-smi is not installed"
fi

if has_command xmutil; then
  if [[ $EUID -eq 0 ]]; then
    xmutil_output=$(xmutil listapps 2>&1)
    xmutil_result=$?
  elif sudo -n true 2>/dev/null; then
    xmutil_output=$(sudo -n xmutil listapps 2>&1)
    xmutil_result=$?
  else
    xmutil_output="run this script with sudo, or allow passwordless sudo"
    xmutil_result=1
  fi

  if [[ $xmutil_result -eq 0 ]]; then
    report "XMUtil" "OK" "xmutil listapps completed"
  else
    report "XMUtil" "FAIL" "${xmutil_output%%$'\n'*}"
  fi
else
  report "XMUtil" "FAIL" "xmutil is not installed"
fi

section "Raw details"
printf 'Hostname: %s\n' "$hostname_value"
printf 'IPv4: %s\n' "${ip_addresses:-none}"
printf 'Kernel: %s\n' "$kernel"
printf 'Architecture: %s\n' "$architecture"
printf 'CMA: %s\n' "${cma_info:-none}"

exit "$report_status"
