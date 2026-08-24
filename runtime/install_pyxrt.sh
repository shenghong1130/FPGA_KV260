#!/usr/bin/env bash
# Build the Python 3.12 pyxrt binding from the exact installed XRT source package.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CACHE_ROOT="${PYXRT_CACHE_ROOT:-/var/cache/kv260-runtime/xrt-source}"
PYNQ_VENV="${PYNQ_VENV:-/opt/kv260-pynq}"
XRT_VERSION="not installed"
SOURCE_VERSION="not resolved"
PYXRT_OUTPUT="not resolved"

die() {
  printf '%s\n' \
    "pyxrt installation failed: $*" \
    "Kernel: $(uname -r)" \
    "Python version: $(python3 --version 2>&1 || printf 'unavailable')" \
    "XRT Debian version: $XRT_VERSION" \
    "xilinx-runtime source version: $SOURCE_VERSION" \
    "pyxrt output path: $PYXRT_OUTPUT" >&2
  exit 1
}

on_error() {
  local line="$1"
  local status="$2"
  trap - ERR
  die "unexpected command failure at line $line (exit=$status)"
}

trap 'on_error "$LINENO" "$?"' ERR

validate_pyxrt() {
  local interpreter="$1"
  "$interpreter" - <<'PY'
import pathlib
import sys
import sysconfig

if sys.version_info[:2] != (3, 12):
    raise RuntimeError(f"pyxrt requires Python 3.12, got {sys.version.split()[0]}")

import pyxrt

missing = [name for name in ("device", "bo", "kernel") if not hasattr(pyxrt, name)]
if missing:
    raise RuntimeError(f"pyxrt basic API missing: {', '.join(missing)}")

path = pathlib.Path(pyxrt.__file__).resolve()
local_platlib = pathlib.Path(
    sysconfig.get_path("platlib", scheme="posix_local")
).resolve()
if local_platlib not in path.parents:
    raise RuntimeError(
        f"pyxrt must be installed from matching XRT source under "
        f"{local_platlib}, got {path}"
    )
print(f"Python version: {sys.version.split()[0]}")
print(f"pyxrt path: {path}")
print("pyxrt API: device, bo, kernel")
PY
}

validate_existing_install() {
  validate_pyxrt python3 || return 1

  if [[ -x "$PYNQ_VENV/bin/python" ]]; then
    if [[ -f "$PYNQ_VENV/pyvenv.cfg" ]]; then
      sed -i \
        's/^include-system-site-packages = false$/include-system-site-packages = true/' \
        "$PYNQ_VENV/pyvenv.cfg"
    fi
    validate_pyxrt "$PYNQ_VENV/bin/python" || return 1
  fi
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ $(uname -m) == aarch64 ]] || die "需要 aarch64，当前为 $(uname -m)"

python_version=$(python3 -c \
  'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
[[ "$python_version" == 3.12 ]] || die "仅支持 Python 3.12，当前为 $python_version"

if validate_existing_install; then
  echo "pyxrt: already available"
  echo "pyxrt: OK"
  exit 0
fi

for command in apt-cache apt-get dpkg-query find python3 sed; do
  command -v "$command" >/dev/null 2>&1 || die "缺少命令: $command"
done

XRT_VERSION=$(dpkg-query -W -f='${Version}' xrt 2>/dev/null) || \
  die "xrt Debian package 未安装"
[[ -d /usr/include/xrt ]] || die "xrt 已安装但缺少 /usr/include/xrt"
[[ -e /usr/lib/libxrt_coreutil.so ]] || \
  die "xrt 已安装但缺少 /usr/lib/libxrt_coreutil.so"

"$SCRIPT_DIR/ensure_sdk_ppa.sh"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential g++ python3-dev pybind11-dev uuid-dev libboost-dev dpkg-dev

for command in dpkg-source g++ python3-config; do
  command -v "$command" >/dev/null 2>&1 || \
    die "安装 build dependencies 后仍缺少命令: $command"
done

if ! source_metadata=$(apt-cache showsrc xilinx-runtime 2>/dev/null); then
  die "无法读取 xilinx-runtime source package metadata；检查 deb-src"
fi
mapfile -t source_versions < <(
  printf '%s\n' "$source_metadata" | awk '/^Version:/ {print $2}' | sort -uV
)
for available_version in "${source_versions[@]}"; do
  if [[ "$available_version" == "$XRT_VERSION" ]]; then
    SOURCE_VERSION="$available_version"
    break
  fi
done
if [[ "$SOURCE_VERSION" != "$XRT_VERSION" ]]; then
  printf 'Available xilinx-runtime source versions:\n  %s\n' \
    "${source_versions[*]:-none}" >&2
  die "installed xrt and xilinx-runtime source versions do not match"
fi
echo "XRT Debian version: $XRT_VERSION"
echo "xilinx-runtime source version: $SOURCE_VERSION"

version_key=${XRT_VERSION//\//_}
version_key=${version_key//:/_}
version_root="$CACHE_ROOT/$version_key"
download_dir="$version_root/download"
source_dir="$version_root/source"
build_dir="$version_root/build"
install -d -m 0755 "$download_dir" "$build_dir"

source_cpp=""
if [[ -d "$source_dir" ]]; then
  source_cpp=$(find "$source_dir" -type f \
    -path '*/src/python/pybind11/src/pyxrt.cpp' -print -quit 2>/dev/null)
fi
if [[ -z "$source_cpp" ]]; then
  if [[ -d "$source_dir" ]]; then
    die "cached source directory exists but pyxrt.cpp is missing: $source_dir"
  fi

  echo "Downloading matching source package: xilinx-runtime=$XRT_VERSION"
  (
    cd "$download_dir"
    apt-get source --download-only "xilinx-runtime=$XRT_VERSION"
  )
  dsc_file=$(find "$download_dir" -maxdepth 1 -type f \
    -name 'xilinx-runtime_*.dsc' -print -quit)
  [[ -n "$dsc_file" ]] || die "apt source did not produce a xilinx-runtime .dsc"

  dsc_version=$(sed -n 's/^Version:[[:space:]]*//p' "$dsc_file" | head -n 1)
  [[ "$dsc_version" == "$XRT_VERSION" ]] || \
    die "downloaded source version mismatch: expected=$XRT_VERSION got=${dsc_version:-unknown}"

  dpkg-source -x "$dsc_file" "$source_dir"
  source_cpp=$(find "$source_dir" -type f \
    -path '*/src/python/pybind11/src/pyxrt.cpp' -print -quit)
fi
[[ -f "$source_cpp" ]] || die "找不到 src/python/pybind11/src/pyxrt.cpp"

extension_suffix=$(python3-config --extension-suffix)
[[ -n "$extension_suffix" ]] || die "python3-config 未返回 extension suffix"
PYXRT_OUTPUT="$build_dir/pyxrt${extension_suffix}"

python_dist_packages=$(python3 -c \
  'import sysconfig; print(sysconfig.get_path("platlib", scheme="posix_local"))')
[[ "$python_dist_packages" == /usr/local/*/python3.12/dist-packages ]] || \
  die "unexpected Python posix_local platlib: $python_dist_packages"

echo "Building pyxrt from: $source_cpp"
read -r -a python_include_flags <<<"$(python3-config --includes)"
g++ \
  -O2 \
  -shared \
  -std=c++17 \
  -fPIC \
  "${python_include_flags[@]}" \
  -I/usr/include/pybind11 \
  -I/usr/include/xrt \
  "$source_cpp" \
  -L/usr/lib \
  -Wl,-rpath,/usr/lib \
  -lxrt_coreutil \
  -luuid \
  -lpthread \
  -o "$PYXRT_OUTPUT"

install -d -m 0755 "$python_dist_packages"
install -m 0755 "$PYXRT_OUTPUT" "$python_dist_packages/pyxrt${extension_suffix}"
PYXRT_OUTPUT="$python_dist_packages/pyxrt${extension_suffix}"

validate_pyxrt python3
if [[ -x "$PYNQ_VENV/bin/python" ]]; then
  if [[ -f "$PYNQ_VENV/pyvenv.cfg" ]]; then
    sed -i \
      's/^include-system-site-packages = false$/include-system-site-packages = true/' \
      "$PYNQ_VENV/pyvenv.cfg"
  fi
  validate_pyxrt "$PYNQ_VENV/bin/python"
fi

echo "pyxrt: OK"
