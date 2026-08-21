#!/usr/bin/env bash
# Ensure the zocl module matches the running KV260 kernel.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CHECK_SCRIPT="$SCRIPT_DIR/../scripts/check_zocl.sh"

[[ $EUID -eq 0 ]] || {
  echo "请使用 sudo 运行 $0" >&2
  exit 1
}

kernel=$(uname -r)

if ! find "/lib/modules/${kernel}" -type f -name '*zocl*.ko*' -print -quit | grep -q .; then
  package="${ZOCL_PACKAGE:-linux-modules-extra-${kernel}}"
  echo "zocl module is absent; attempting to install kernel-matched package: $package"
  apt-get update
  apt-get install -y "$package" || {
    cat >&2 <<EOF
无法安装 $package。不要安装其他内核版本的 zocl。
请从当前 Xilinx/Kria 软件源中提供与 $kernel 精确匹配的包，并通过
ZOCL_PACKAGE=<匹配包名> sudo $0 重新运行。
EOF
    exit 1
  }
fi

depmod -a "$kernel"
modprobe zocl
"$CHECK_SCRIPT"
