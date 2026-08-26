#!/usr/bin/env bash
# Install the real KV260 Worker service after the Minimal PYNQ runtime is ready.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PYNQ_VENV="${PYNQ_VENV:-/opt/kv260-pynq}"
INSTALL_DIR="${KV260_WORKER_INSTALL_DIR:-/opt/kv260-worker}"
STATE_DIR="${KV260_WORKER_STATE_DIR:-/var/lib/kv260-worker}"
SERVICE_NAME="kv260-worker.service"

die() {
  printf 'KV260 Worker installation failed: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"
[[ $(uname -m) == aarch64 ]] || die "需要 aarch64，当前为 $(uname -m)"
[[ $(uname -r) == *xilinx* ]] || die "需要 Xilinx kernel，当前为 $(uname -r)"
[[ -x "$PYNQ_VENV/bin/python" ]] || die "找不到 PYNQ Python: $PYNQ_VENV/bin/python"
[[ -d "$SCRIPT_DIR/app" ]] || die "找不到 Worker app: $SCRIPT_DIR/app"
[[ -f "$SCRIPT_DIR/requirements.txt" ]] || die "找不到 Worker requirements.txt"
[[ -f "$SCRIPT_DIR/kv260-worker.service" ]] || die "找不到 systemd unit"

hostname_value=$(hostname)
[[ "$hostname_value" =~ ^kv260([1-9]|1[0-9]|20)$ ]] || \
  die "hostname 必须为 kv2601 ... kv26020，当前为 $hostname_value"

for command in apt-get install systemctl; do
  command -v "$command" >/dev/null 2>&1 || die "缺少命令: $command"
done
DEBIAN_FRONTEND=noninteractive apt-get install -y curl iproute2
for command in curl ss; do
  command -v "$command" >/dev/null 2>&1 || die "缺少命令: $command"
done

BOARD=KV260 XILINX_XRT=/usr PYTHONNOUSERSITE=1 \
  "$PYNQ_VENV/bin/python" - <<'PY'
import pyxrt
from pynq import Device
from pynq.ps import ON_TARGET

if not ON_TARGET:
    raise RuntimeError("PYNQ ON_TARGET is false")
if type(Device.active_device).__name__ != "EmbeddedDevice":
    raise RuntimeError(
        f"PYNQ active device is {type(Device.active_device).__name__}, not EmbeddedDevice"
    )
for name in ("device", "bo", "kernel"):
    if not hasattr(pyxrt, name):
        raise RuntimeError(f"pyxrt API missing: {name}")
print("Worker PYNQ prerequisite: OK")
PY

# Stop only the previously installed formal service before inspecting port 8080.
if systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
  systemctl stop "$SERVICE_NAME"
fi

listener_output=$(ss -H -lntp 'sport = :8080')
if [[ -n "$listener_output" ]]; then
  mapfile -t listener_pids < <(
    sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$listener_output" | sort -u
  )
  (( ${#listener_pids[@]} > 0 )) || \
    die "port 8080 already occupied; unable to identify PID: $listener_output"

  temporary_pids=()
  for listener_pid in "${listener_pids[@]}"; do
    [[ -r "/proc/$listener_pid/cmdline" ]] || continue
    command_line=$(tr '\0' ' ' <"/proc/$listener_pid/cmdline")
    if [[ " $command_line " == *" /home/ubuntu/test_worker.py "* ]]; then
      temporary_pids+=("$listener_pid")
    else
      die "port 8080 already occupied; PID=$listener_pid command=$command_line"
    fi
  done

  for listener_pid in "${temporary_pids[@]}"; do
    kill -TERM "$listener_pid"
  done
  for _ in {1..20}; do
    ss -H -lnt 'sport = :8080' 2>/dev/null | grep -q . || break
    sleep 0.25
  done
  ss -H -lnt 'sport = :8080' 2>/dev/null | grep -q . && \
    die "temporary test_worker.py did not release port 8080"
  echo "Temporary test_worker.py detected and stopped"
fi

PYTHONNOUSERSITE=1 PIP_USER=0 "$PYNQ_VENV/bin/python" -m pip install \
  --upgrade-strategy only-if-needed -r "$SCRIPT_DIR/requirements.txt"

install -d -m 0755 "$INSTALL_DIR/app"
install -m 0644 "$SCRIPT_DIR/app/__init__.py" "$INSTALL_DIR/app/__init__.py"
install -m 0644 "$SCRIPT_DIR/app/main.py" "$INSTALL_DIR/app/main.py"
install -m 0644 "$SCRIPT_DIR/app/state.py" "$INSTALL_DIR/app/state.py"
install -m 0644 "$SCRIPT_DIR/app/fpga.py" "$INSTALL_DIR/app/fpga.py"
install -m 0644 "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
install -d -m 0750 "$STATE_DIR/artifacts"
install -m 0644 "$SCRIPT_DIR/kv260-worker.service" \
  "/etc/systemd/system/$SERVICE_NAME"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

worker_ready=0
for _ in {1..30}; do
  if curl --noproxy '*' --silent --show-error --fail --max-time 2 \
      http://127.0.0.1:8080/health >/tmp/kv260-worker-health.json; then
    worker_ready=1
    break
  fi
  sleep 1
done
if (( worker_ready == 0 )); then
  set +e
  systemctl status "$SERVICE_NAME" --no-pager >&2
  journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2
  set -e
  die "Worker API did not become ready on 127.0.0.1:8080"
fi

curl --noproxy '*' --silent --show-error --fail --max-time 5 \
  http://127.0.0.1:8080/status >/tmp/kv260-worker-status.json
"$PYNQ_VENV/bin/python" - "$hostname_value" \
  /tmp/kv260-worker-health.json /tmp/kv260-worker-status.json <<'PY'
import json
import pathlib
import sys

expected_board = sys.argv[1]
health = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
status = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
if health.get("ok") is not True or health.get("board") != expected_board:
    raise RuntimeError(f"unexpected Worker health: {health}")
expected_status = {
    "board": expected_board,
    "fpga_ready": False,
    "lease_id": None,
    "artifact_id": None,
}
if status != expected_status:
    raise RuntimeError(f"unexpected initial Worker status: {status}")
print(f"Worker health: OK ({health})")
print(f"Worker status: OK ({status})")
PY
rm -f /tmp/kv260-worker-health.json /tmp/kv260-worker-status.json

systemctl is-enabled --quiet "$SERVICE_NAME" || die "$SERVICE_NAME is not enabled"
systemctl is-active --quiet "$SERVICE_NAME" || die "$SERVICE_NAME is not active"
echo "KV260 Worker Service Installation: OK"
