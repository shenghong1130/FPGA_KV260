#!/usr/bin/env bash
# Read-only ZOCL module and render-node check for the running kernel.
set -Eeuo pipefail

kernel=$(uname -r)
module_path=$(find "/lib/modules/${kernel}" -type f -name '*zocl*.ko*' -print -quit 2>/dev/null || true)
[[ -n "$module_path" ]] || {
  echo "ZOCL check: FAIL (no module for kernel $kernel)" >&2
  exit 1
}

grep -q '^zocl ' /proc/modules || {
  echo "ZOCL check: FAIL (module exists but is not loaded)" >&2
  exit 1
}

ls -la /dev/dri/ 2>/dev/null || true
[[ -e /dev/dri/renderD128 ]] || {
  echo "ZOCL check: FAIL (/dev/dri/renderD128 is absent)" >&2
  exit 1
}

echo "ZOCL module: $module_path"
echo "ZOCL check: OK (/dev/dri/renderD128 present)"
