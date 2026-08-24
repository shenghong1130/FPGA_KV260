#!/usr/bin/env bash
# Ensure the Ubuntu Xilinx SDK PPA is configured for the running Ubuntu release.
set -Eeuo pipefail

die() {
  echo "SDK PPA setup failed: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ -r /etc/os-release ]] || die "无法读取 /etc/os-release"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_CODENAME:-}" == "noble" ]] || \
  die "仅支持 Ubuntu 24.04 Noble；当前系统: ${PRETTY_NAME:-unknown}"

if grep -RqsE 'ppa\.launchpad(content)?\.net/ubuntu-xilinx/sdk/ubuntu|ubuntu-xilinx/sdk' \
  /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
  echo "Ubuntu Xilinx SDK PPA: already configured"
  exit 0
fi

if ! command -v add-apt-repository >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common
fi

echo "Adding Ubuntu Xilinx SDK PPA for ${VERSION_CODENAME}..."
add-apt-repository -y ppa:ubuntu-xilinx/sdk
apt-get update

grep -RqsE 'ppa\.launchpad(content)?\.net/ubuntu-xilinx/sdk/ubuntu|ubuntu-xilinx/sdk' \
  /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || \
  die "PPA 添加完成后仍未找到 ubuntu-xilinx/sdk 软件源"
