#!/usr/bin/env bash
# Read-only XRT and device visibility check for a booted KV260.
set -Eeuo pipefail

command -v xrt-smi >/dev/null || {
  echo "XRT check: FAIL (xrt-smi is not installed)" >&2
  exit 1
}

xrt_output=$(xrt-smi examine 2>&1) || {
  echo "$xrt_output" >&2
  echo "XRT check: FAIL (xrt-smi examine failed)" >&2
  exit 1
}
printf '%s\n' "$xrt_output"

if printf '%s\n' "$xrt_output" | grep -Eiq '(^|[^0-9])0[[:space:]]+devices?[[:space:]]+found|no devices?[[:space:]]+(present|found)'; then
  echo "XRT check: FAIL (no device found)" >&2
  exit 1
fi

echo "XRT check: OK"
