#!/usr/bin/env bash
# Read-only XRT availability check; intended to run on the KV260 target.
set -Eeuo pipefail

command -v xrt-smi >/dev/null || {
  echo "XRT check: xrt-smi is not installed" >&2
  exit 1
}

echo "XRT check: $(xrt-smi --version 2>/dev/null | head -n 1 || true)"
xrt-smi examine
