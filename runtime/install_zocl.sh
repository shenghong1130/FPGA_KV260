#!/usr/bin/env bash
# Install the XRT-matched ZOCL DKMS driver for the running KV260 kernel.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CHECK_SCRIPT="$SCRIPT_DIR/../scripts/check_zocl.sh"

die() {
  printf '%s\n' \
    "ZOCL installation failed: $*" \
    "Current kernel: $(uname -r)" \
    "Current XRT version: $(dpkg-query -W -f='${Version}' xrt 2>/dev/null || printf 'not installed')" \
    "Current xrt-dkms version: $(dpkg-query -W -f='${Version}' xrt-dkms 2>/dev/null || printf 'not installed')" >&2
  exit 1
}

on_error() {
  local line="$1"
  local status="$2"
  trap - ERR
  die "unexpected command failure at line $line (exit=$status)"
}

trap 'on_error "$LINENO" "$?"' ERR

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"

for command in apt-cache apt-get dpkg-query find modprobe depmod; do
  command -v "$command" >/dev/null 2>&1 || die "缺少命令: $command"
done

if ! command -v dkms >/dev/null 2>&1; then
  echo "DKMS is missing; installing dkms..."
  apt-get update || die "无法更新 APT package index；不能自动安装 dkms"
  DEBIAN_FRONTEND=noninteractive apt-get install -y dkms || \
    die "无法自动安装 dkms"
fi
command -v dkms >/dev/null 2>&1 || \
  die "dkms 安装后仍找不到 dkms 命令"

kernel=$(uname -r)
architecture=$(uname -m)
headers_package="linux-headers-${kernel}"

echo "Kernel: $kernel"

if [[ ! -e "/lib/modules/${kernel}/build" ]]; then
  echo "Kernel headers are missing; installing $headers_package"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$headers_package" || \
    die "无法安装 $headers_package；ZOCL 不能为当前 kernel 构建"
fi
[[ -e "/lib/modules/${kernel}/build" ]] || \
  die "$headers_package 安装后 /lib/modules/${kernel}/build 仍不存在"

XRT_VERSION=$(dpkg-query -W -f='${Version}' xrt 2>/dev/null) || \
  die "xrt Debian package 未安装；请先运行 install_xrt.sh"
echo "XRT package version: $XRT_VERSION"

"$SCRIPT_DIR/ensure_sdk_ppa.sh"
apt-get update

mapfile -t SDK_DKMS_VERSIONS < <(
  apt-cache madison xrt-dkms | \
    awk '/ubuntu-xilinx\/sdk/ {print $3}' | \
    sort -uV
)

matching_version=0
for version in "${SDK_DKMS_VERSIONS[@]}"; do
  if [[ "$version" == "$XRT_VERSION" ]]; then
    matching_version=1
    break
  fi
done

if (( ! matching_version )); then
  candidate=$(apt-cache policy xrt-dkms | awk '/Candidate:/ {print $2}')
  printf '%s\n' \
    "XRT / ZOCL version mismatch" \
    "Current XRT version: $XRT_VERSION" \
    "xrt-dkms candidate: ${candidate:-none}" \
    "SDK PPA xrt-dkms versions: ${SDK_DKMS_VERSIONS[*]:-none}" >&2
  die "ubuntu-xilinx/sdk 中没有与 xrt=$XRT_VERSION 完全一致的 xrt-dkms"
fi

installed_dkms_version=$(dpkg-query -W -f='${Version}' xrt-dkms 2>/dev/null || true)
if [[ "$installed_dkms_version" != "$XRT_VERSION" ]]; then
  echo "Installing matched ZOCL DKMS package: xrt-dkms=$XRT_VERSION"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades \
    "xrt-dkms=${XRT_VERSION}"
else
  echo "xrt-dkms already matches XRT: $installed_dkms_version"
fi

installed_dkms_version=$(dpkg-query -W -f='${Version}' xrt-dkms 2>/dev/null) || \
  die "安装后无法读取 xrt-dkms package version"
[[ "$installed_dkms_version" == "$XRT_VERSION" ]] || \
  die "XRT / ZOCL version mismatch: xrt=$XRT_VERSION xrt-dkms=$installed_dkms_version"

dkms_output=$(dkms status)
if ! printf '%s\n' "$dkms_output" | grep -Eq \
  "^xrt/[^,]+,[[:space:]]+${kernel//./\\.},[[:space:]]+${architecture}:[[:space:]]+installed"; then
  echo "Building xrt DKMS module for running kernel: $kernel"
  dkms autoinstall -k "$kernel"
fi
depmod -a "$kernel"

module_path=$(find "/lib/modules/${kernel}" -type f -name '*zocl*.ko*' -print -quit)
[[ -n "$module_path" ]] || \
  die "xrt-dkms=$XRT_VERSION 已安装，但当前 kernel $kernel 中没有 zocl module"

dkms_output=$(dkms status)
printf '%s\n' "$dkms_output"
printf '%s\n' "$dkms_output" | grep -Eq \
  "^xrt/[^,]+,[[:space:]]+${kernel//./\\.},[[:space:]]+${architecture}:[[:space:]]+installed" || \
  die "DKMS 未报告 xrt driver 对当前 kernel $kernel ($architecture) 为 installed"

if ! grep -q '^zocl ' /proc/modules; then
  modprobe zocl
fi
"$CHECK_SCRIPT"

latest_kernel=$(find /lib/modules -mindepth 1 -maxdepth 1 -type d \
  -name '*-xilinx' -printf '%f\n' | sort -V | tail -n 1)
if [[ -n "$latest_kernel" && "$latest_kernel" != "$kernel" ]]; then
  if [[ ! -e "/lib/modules/${latest_kernel}/build" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-headers-${latest_kernel}" || \
      die "检测到新 kernel $latest_kernel，但无法安装对应 headers"
  fi
  dkms_output=$(dkms status)
  if ! printf '%s\n' "$dkms_output" | grep -Eq \
    "^xrt/[^,]+,[[:space:]]+${latest_kernel//./\\.},[[:space:]]+${architecture}:[[:space:]]+installed"; then
    dkms autoinstall -k "$latest_kernel"
  fi
  find "/lib/modules/${latest_kernel}" -type f -name '*zocl*.ko*' -print -quit | grep -q . || \
    die "检测到新 kernel $latest_kernel，但尚未为其生成 zocl module"
  printf '%s\n' \
    "REBOOT_REQUIRED" \
    "Running kernel: $kernel" \
    "Installed newer Xilinx kernel: $latest_kernel" \
    "ZOCL is ready for the new kernel; reboot before continuing Runtime Factory."
  exit 75
fi

echo "ZOCL Driver Installation: OK"
