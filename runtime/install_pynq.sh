#!/usr/bin/env bash
# Install and functionally validate the Minimal PYNQ runtime for KV260/Noble.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ASSET_DIR="$SCRIPT_DIR/pynq_runtime"
PYNQ_VENV="${PYNQ_VENV:-/opt/kv260-pynq}"
PYNQ_SHARE="$PYNQ_VENV/share/kv260-runtime"
PYNQ_VERSION="${PYNQ_VERSION:-3.1.2}"
PYNQ_METADATA_VERSION="${PYNQ_METADATA_VERSION:-0.1.9}"
PYNQ_UTILS_VERSION="${PYNQ_UTILS_VERSION:-0.1.2}"
NUMPY_VERSION="${NUMPY_VERSION:-1.26.4}"
SETUPTOOLS_VERSION="80.0.0"
MIN_CMA_MB="${PYNQ_MIN_CMA_MB:-256}"
OVERLAY_DIR="${PYNQ_OVERLAY_DIR:-/opt/fpga}"
OVERLAY_NAME="${PYNQ_OVERLAY_NAME:-design}"

die() {
  printf '%s\n' \
    "Minimal PYNQ installation failed: $*" \
    "Kernel: $(uname -r)" \
    "XRT version: $(dpkg-query -W -f='${Version}' xrt 2>/dev/null || printf 'not installed')" \
    "PYNQ requested version: $PYNQ_VERSION" >&2
  exit 1
}

on_error() {
  local line="$1"
  local status="$2"
  local installed_pynq="not installed"
  trap - ERR
  if [[ -x "$PYNQ_VENV/bin/python" ]]; then
    installed_pynq=$("$PYNQ_VENV/bin/python" -c \
      'import pynq; print(pynq.__version__)' 2>/dev/null) || installed_pynq="unavailable"
  fi
  printf '%s\n' \
    "Minimal PYNQ unexpected failure at line $line (exit=$status)" \
    "Kernel: $(uname -r)" \
    "XRT version: $(dpkg-query -W -f='${Version}' xrt 2>/dev/null || printf 'not installed')" \
    "PYNQ installed/requested: $installed_pynq / $PYNQ_VERSION" >&2
  exit "$status"
}

trap 'on_error "$LINENO" "$?"' ERR

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ $(uname -m) == aarch64 ]] || die "需要 aarch64，当前为 $(uname -m)"
[[ $(uname -r) == *xilinx* ]] || die "需要 Xilinx kernel"
[[ -e /sys/class/fpga_manager/fpga0 ]] || die "找不到 /sys/class/fpga_manager/fpga0"
[[ -d "$ASSET_DIR" ]] || die "找不到 PYNQ runtime assets: $ASSET_DIR"
[[ "$MIN_CMA_MB" =~ ^[0-9]+$ ]] || die "PYNQ_MIN_CMA_MB 必须是整数"

[[ -r /etc/os-release ]] || die "无法读取 /etc/os-release"
# Fixed OS metadata path, validated above.
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_CODENAME:-}" == noble ]] || \
  die "仅支持 Ubuntu 24.04 Noble；当前系统: ${PRETTY_NAME:-unknown}"

cma_kb=$(awk '/^CmaTotal:/ {print $2}' /proc/meminfo)
[[ "$cma_kb" =~ ^[0-9]+$ ]] || die "/proc/meminfo 中没有 CmaTotal"
cma_mb=$((cma_kb / 1024))
(( cma_mb >= MIN_CMA_MB )) || \
  die "CMA 过小：${cma_mb} MiB，最低要求 ${MIN_CMA_MB} MiB"
echo "CMA: OK (${cma_mb} MiB; minimum ${MIN_CMA_MB} MiB)"
if (( cma_mb < 512 )); then
  echo "WARNING: CMA 小于 512 MiB；大 buffer/DMA 工作负载可能需要增大 CMA"
fi

for command in apt-get dpkg-query modprobe systemctl xclbinutil; do
  command -v "$command" >/dev/null 2>&1 || die "缺少命令: $command"
done
dpkg-query -W -f='${Version}' xrt >/dev/null 2>&1 || die "xrt Debian package 未安装"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential device-tree-compiler libdrm-dev libffi-dev libssl-dev \
  python3-dev python3-pip python3-venv

system_python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
[[ "$system_python_version" == 3.12 ]] || \
  die "Noble Runtime 需要已验证的 Python 3.12，当前为 $system_python_version"

if [[ ! -x "$PYNQ_VENV/bin/python" ]]; then
  echo "Creating PYNQ virtual environment: $PYNQ_VENV"
  python3 -m venv --system-site-packages "$PYNQ_VENV"
else
  venv_python_version=$("$PYNQ_VENV/bin/python" -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  [[ "$venv_python_version" == "$system_python_version" ]] || \
    die "现有 venv Python=$venv_python_version，与系统 Python=$system_python_version 不一致"
  # pyxrt is installed separately in /usr/local by install_pyxrt.sh.  Keep
  # system-site-packages enabled so the venv can load that matching binding.
  sed -i 's/^include-system-site-packages = false$/include-system-site-packages = true/' \
    "$PYNQ_VENV/pyvenv.cfg"
  echo "Reusing PYNQ virtual environment: $PYNQ_VENV"
fi

export BOARD=KV260
export XILINX_XRT=/usr
export PYNQ_JUPYTER_NOTEBOOKS=
export PYTHONNOUSERSITE=1
export PIP_USER=0

"$PYNQ_VENV/bin/python" - <<'PY' || \
  die "pyxrt missing; run install_pyxrt.sh first"
import pyxrt
import pathlib
import sysconfig

missing = [name for name in ("device", "bo", "kernel") if not hasattr(pyxrt, name)]
if missing:
    raise RuntimeError(f"pyxrt basic API missing: {', '.join(missing)}")
path = pathlib.Path(pyxrt.__file__).resolve()
local_platlib = pathlib.Path(
    sysconfig.get_path("platlib", scheme="posix_local")
).resolve()
if local_platlib not in path.parents:
    raise RuntimeError(f"unexpected pyxrt path: {path}; expected under {local_platlib}")
print(f"pyxrt: OK ({path})")
PY

"$PYNQ_VENV/bin/python" -m pip install --upgrade \
  pip wheel "setuptools==$SETUPTOOLS_VERSION"
"$PYNQ_VENV/bin/python" -m pip install --ignore-installed \
  "numpy==$NUMPY_VERSION" \
  "pynqmetadata==$PYNQ_METADATA_VERSION" \
  "pynqutils==$PYNQ_UTILS_VERSION" \
  "grpcio==1.64.0" \
  "grpcio-tools==1.64.0" \
  nest_asyncio

# PYNQ_REMOTE is only a PYNQ setup.py build switch.  It skips optional native
# HDMI/DisplayPort/audio/PCam extensions on aarch64; this KV260 still runs the
# normal on-target EmbeddedDevice path, not PYNQ RemoteDevice.
PYNQ_REMOTE=1 BOARD=KV260 XILINX_XRT=/usr \
  "$PYNQ_VENV/bin/python" -m pip install \
    --upgrade --upgrade-strategy only-if-needed --no-build-isolation --no-cache-dir \
    "pynq==$PYNQ_VERSION"
"$PYNQ_VENV/bin/python" -m pip check

"$PYNQ_VENV/bin/python" - "$PYNQ_VENV" "$PYNQ_VERSION" \
  "$PYNQ_METADATA_VERSION" "$PYNQ_UTILS_VERSION" <<'PY'
import importlib
import importlib.metadata
import pathlib
import sys
import sysconfig

venv = pathlib.Path(sys.argv[1]).resolve()
expected_versions = {
    "pynq": sys.argv[2],
    "pynqmetadata": sys.argv[3],
    "pynqutils": sys.argv[4],
}
site_packages = pathlib.Path(sysconfig.get_path("purelib")).resolve()
if venv not in site_packages.parents:
    raise RuntimeError(f"venv site-packages is outside {venv}: {site_packages}")

for name, expected in expected_versions.items():
    module = importlib.import_module(name)
    actual = getattr(module, "__version__", "unknown")
    module_path = pathlib.Path(module.__file__).resolve()
    if site_packages not in module_path.parents:
        raise RuntimeError(f"{name} loaded outside venv: {module_path}")
    if actual != expected:
        raise RuntimeError(f"{name} version mismatch: expected={expected} actual={actual}")
    print(f"{name} {actual}: {module_path}")

for name in ("grpc", "grpc_tools", "nest_asyncio"):
    module_path = pathlib.Path(importlib.import_module(name).__file__).resolve()
    if site_packages not in module_path.parents:
        raise RuntimeError(f"{name} loaded outside venv: {module_path}")

for distribution, expected in (
    ("setuptools", "80.0.0"),
    ("grpcio", "1.64.0"),
    ("grpcio-tools", "1.64.0"),
):
    actual = importlib.metadata.version(distribution)
    if actual != expected:
        raise RuntimeError(
            f"{distribution} version mismatch: expected={expected} actual={actual}"
        )
PY

install -d -m 0755 "$PYNQ_SHARE"
install -m 0644 "$ASSET_DIR/pynq.dts" "$PYNQ_SHARE/pynq.dts"
install -m 0755 "$ASSET_DIR/insert_dtbo.py" "$PYNQ_SHARE/insert_dtbo.py"
install -m 0755 "$ASSET_DIR/clear_pl_state.py" "$PYNQ_SHARE/clear_pl_state.py"
install -m 0755 "$ASSET_DIR/validate_runtime.py" "$PYNQ_SHARE/validate_runtime.py"
dtc -@ -I dts -O dtb -o "$PYNQ_SHARE/pynq.dtbo" "$PYNQ_SHARE/pynq.dts"

# Retained for XRT/Kria platform compatibility. PYNQ 3.1.2 itself selects the
# EmbeddedDevice through XILINX_XRT and FPGA Manager rather than reading this.
printf 'KV260\n' > /etc/xocl.txt
chmod 0644 /etc/xocl.txt

cat > /etc/profile.d/kv260-pynq.sh <<EOF
export KV260_PYNQ_VENV="$PYNQ_VENV"
export BOARD="KV260"
export XILINX_XRT="/usr"
export PATH="\$KV260_PYNQ_VENV/bin:\$PATH"
EOF
chmod 0644 /etc/profile.d/kv260-pynq.sh

cat > /etc/systemd/system/kv260-pynq-dt.service <<EOF
[Unit]
Description=Insert the KV260 Minimal PYNQ device-tree runtime
Requires=sys-kernel-config.mount
After=sys-kernel-config.mount systemd-modules-load.service
Before=kv260-pynq-clear-pl-state.service

[Service]
Type=oneshot
Environment=BOARD=KV260
Environment=XILINX_XRT=/usr
ExecStartPre=/sbin/modprobe zocl
ExecStart=$PYNQ_VENV/bin/python $PYNQ_SHARE/insert_dtbo.py
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/kv260-pynq-clear-pl-state.service <<EOF
[Unit]
Description=Clear stale PYNQ PL state before the worker loads its overlay
Requires=kv260-pynq-dt.service
After=kv260-pynq-dt.service

[Service]
Type=oneshot
Environment=BOARD=KV260
Environment=XILINX_XRT=/usr
ExecStart=$PYNQ_VENV/bin/python $PYNQ_SHARE/clear_pl_state.py
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kv260-pynq-dt.service kv260-pynq-clear-pl-state.service

if [[ "${PYNQ_DEFER_ACTIVATION:-0}" == 1 ]]; then
  echo "Minimal PYNQ packages and runtime assets: OK"
  exit 0
fi

systemctl restart kv260-pynq-dt.service kv260-pynq-clear-pl-state.service
systemctl is-active --quiet kv260-pynq-dt.service || \
  die "kv260-pynq-dt.service 未处于 active 状态"

bit_path="$OVERLAY_DIR/${OVERLAY_NAME}.bit"
hwh_path="$OVERLAY_DIR/${OVERLAY_NAME}.hwh"
validation_args=()
if [[ -f "$bit_path" && -f "$hwh_path" ]]; then
  validation_args=(--bit "$bit_path")
elif [[ -e "$bit_path" || -e "$hwh_path" ]]; then
  die "Overlay 文件不完整；必须同时提供 $bit_path 和 $hwh_path"
fi

BOARD=KV260 XILINX_XRT=/usr \
  "$PYNQ_VENV/bin/python" "$PYNQ_SHARE/validate_runtime.py" "${validation_args[@]}"

echo "Minimal Kria-PYNQ Runtime Installation: OK"
