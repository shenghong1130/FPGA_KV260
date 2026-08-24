#!/usr/bin/env bash
# Ensure the Ubuntu Xilinx SDK PPA is configured for the running Ubuntu release.
set -Eeuo pipefail

die() {
  echo "SDK PPA setup failed: $*" >&2
  exit 1
}

detect_sdk_types() {
  local source_file types_line
  sdk_binary=0
  sdk_source=0

  while IFS= read -r -d '' source_file; do
    if types_line=$(sed -n 's/^[[:space:]]*Types:[[:space:]]*//p' "$source_file") \
      && [[ -n "$types_line" ]]; then
      [[ " $types_line " == *" deb "* ]] && sdk_binary=1
      [[ " $types_line " == *" deb-src "* ]] && sdk_source=1
    else
      grep -Eq '^[[:space:]]*deb[[:space:]]+' "$source_file" && sdk_binary=1
      grep -Eq '^[[:space:]]*deb-src[[:space:]]+' "$source_file" && sdk_source=1
    fi
  done < <(
    grep -RslZE 'ppa\.launchpad(content)?\.net/ubuntu-xilinx/sdk/ubuntu|ubuntu-xilinx/sdk' \
      /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true
  )
  return 0
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ -r /etc/os-release ]] || die "无法读取 /etc/os-release"
# Fixed OS metadata path, validated above.
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_CODENAME:-}" == "noble" ]] || \
  die "仅支持 Ubuntu 24.04 Noble；当前系统: ${PRETTY_NAME:-unknown}"

if ! command -v add-apt-repository >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common
fi

detect_sdk_types
if (( sdk_binary && sdk_source )); then
  echo "Ubuntu Xilinx SDK PPA: deb and deb-src already configured"
else
  echo "Enabling Ubuntu Xilinx SDK PPA deb/deb-src for ${VERSION_CODENAME}..."
  add-apt-repository -y --enable-source ppa:ubuntu-xilinx/sdk
  apt-get update
fi

detect_sdk_types
(( sdk_binary )) || die "PPA 配置后仍未启用 ubuntu-xilinx/sdk binary packages"
(( sdk_source )) || die "PPA 配置后仍未启用 ubuntu-xilinx/sdk source packages"

echo "Ubuntu Xilinx SDK PPA: deb and deb-src enabled"
