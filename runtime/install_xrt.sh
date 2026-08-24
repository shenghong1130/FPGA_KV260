#!/usr/bin/env bash
# Install XRT from ubuntu-xilinx/sdk and report the exact Debian package version.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

die() {
  echo "XRT installation failed: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
for command in apt-cache apt-get dpkg-query; do
  command -v "$command" >/dev/null 2>&1 || die "缺少命令: $command"
done
"$SCRIPT_DIR/ensure_sdk_ppa.sh"
apt-get update

mapfile -t SDK_XRT_VERSIONS < <(
  apt-cache madison xrt | \
    awk '/ubuntu-xilinx\/sdk/ {print $3}' | \
    sort -uV
)

(( ${#SDK_XRT_VERSIONS[@]} > 0 )) || \
  die "ubuntu-xilinx/sdk 没有为当前系统/架构提供 xrt"

installed_version=$(dpkg-query -W -f='${Version}' xrt 2>/dev/null || true)

# Preserve an explicitly requested or already-installed SDK version.  Do not
# silently upgrade a working userspace package and then leave xrt-dkms behind.
if [[ -n "${XRT_VERSION:-}" ]]; then
  TARGET_XRT_VERSION="$XRT_VERSION"
elif [[ -n "$installed_version" ]]; then
  TARGET_XRT_VERSION="$installed_version"
else
  TARGET_XRT_VERSION="${SDK_XRT_VERSIONS[-1]}"
fi
version_available=0
for version in "${SDK_XRT_VERSIONS[@]}"; do
  if [[ "$version" == "$TARGET_XRT_VERSION" ]]; then
    version_available=1
    break
  fi
done

if (( ! version_available )); then
  printf 'SDK PPA available XRT versions:\n  %s\n' "${SDK_XRT_VERSIONS[*]}" >&2
  die "指定的 XRT_VERSION=$TARGET_XRT_VERSION 不在 ubuntu-xilinx/sdk 中"
fi

if [[ "$installed_version" != "$TARGET_XRT_VERSION" ]]; then
  echo "Installing XRT from SDK PPA: ${TARGET_XRT_VERSION} (current: ${installed_version:-not installed})"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades \
    "xrt=${TARGET_XRT_VERSION}" || \
    die "无法安装 xrt=$TARGET_XRT_VERSION"
else
  echo "XRT package already matches SDK PPA: $installed_version"
fi

installed_version=$(dpkg-query -W -f='${Version}' xrt 2>/dev/null) || \
  die "安装后无法读取 xrt package version"
[[ "$installed_version" == "$TARGET_XRT_VERSION" ]] || \
  die "安装后版本不一致：expected=$TARGET_XRT_VERSION installed=$installed_version"
command -v xrt-smi >/dev/null 2>&1 || die "xrt 已安装但找不到 xrt-smi"

echo "XRT userspace installation: OK ($installed_version)"
