#!/usr/bin/env bash
# Read-only XRT userspace and accelerator-platform diagnostic.
set -Eeuo pipefail

command -v xrt-smi >/dev/null || {
  echo "XRT check: FAIL (xrt-smi is not installed)" >&2
  exit 1
}

xrt_version=$(dpkg-query -W -f='${Version}' xrt 2>/dev/null) || {
  echo "XRT check: FAIL (xrt Debian package is not installed)" >&2
  exit 1
}
echo "XRT userspace package: OK ($xrt_version)"

set +e
xrt_output=$(xrt-smi examine 2>&1)
xrt_rc=$?
set -e
printf '%s\n' "$xrt_output"

if (( xrt_rc != 0 )); then
  echo "WARNING: xrt-smi examine exited with $xrt_rc; this does not block the PYNQ Overlay model"
elif printf '%s\n' "$xrt_output" | grep -Eiq '(^|[^0-9])0[[:space:]]+devices?[[:space:]]+found|no devices?[[:space:]]+(present|found)'; then
  echo "WARNING: xrt-smi reports 0 accelerator devices; PYNQ Runtime validation will continue"
else
  echo "XRT accelerator-platform diagnostic: device reported"
fi
