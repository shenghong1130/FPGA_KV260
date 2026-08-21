#!/usr/bin/env bash
# Install the minimal PYNQ Python runtime in an isolated virtual environment.
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || {
  echo "请使用 sudo 运行 $0" >&2
  exit 1
}

PYNQ_VENV="${PYNQ_VENV:-/opt/kv260-pynq}"

apt-get update
apt-get install -y python3-pip python3-venv

if [[ ! -x "$PYNQ_VENV/bin/python" ]]; then
  python3 -m venv "$PYNQ_VENV"
fi

"$PYNQ_VENV/bin/python" -m pip install --upgrade pip
"$PYNQ_VENV/bin/python" -m pip install \
  --upgrade --upgrade-strategy only-if-needed --no-build-isolation pynq

cat > /etc/profile.d/kv260-pynq.sh <<EOF
export KV260_PYNQ_VENV="$PYNQ_VENV"
export PATH="\$KV260_PYNQ_VENV/bin:\$PATH"
EOF
chmod 0644 /etc/profile.d/kv260-pynq.sh

"$PYNQ_VENV/bin/python" -c 'from pynq import MMIO, Overlay, allocate; print("PYNQ minimal runtime: OK")'
