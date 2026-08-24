#!/usr/bin/env bash
# Read-only ZOCL driver check for the running kernel.
set -Eeuo pipefail

kernel=$(uname -r)
if ! module_path=$(find "/lib/modules/${kernel}" -type f \
  -name '*zocl*.ko*' -print -quit 2>/dev/null); then
  echo "ZOCL check: FAIL (cannot inspect modules for kernel $kernel)" >&2
  exit 1
fi
[[ -n "$module_path" ]] || {
  echo "ZOCL check: FAIL (no module for kernel $kernel)" >&2
  exit 1
}

grep -q '^zocl ' /proc/modules || {
  echo "ZOCL check: FAIL (module exists but is not loaded)" >&2
  exit 1
}

echo "ZOCL module: $module_path"
echo "ZOCL loaded: OK"

# A render node belongs to the XRT accelerator-platform layer. Its absence
# does not mean the xrt-dkms module failed to install or load.
if [[ -e /dev/dri/renderD128 ]]; then
  echo "XRT accelerator render node: PRESENT (/dev/dri/renderD128)"
else
  echo "XRT accelerator render node: NOT PRESENT (diagnostic only)"
fi

echo "ZOCL Driver Installation: OK"
