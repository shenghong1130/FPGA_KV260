#!/usr/bin/env bash
# Runtime Factory launcher. Run on the deployment PC after the KV260 has booted.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUNTIME_DIR="$SCRIPT_DIR/runtime"
CHECK_DIR="$SCRIPT_DIR/scripts"
LOG_DIR="$SCRIPT_DIR/logs"

die() {
  echo "错误: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<EOF
用法:
  $0 <KV260编号>

示例:
  $0 2

编号范围为 1-20。脚本会连接对应的 ubuntu@192.168.31.(81+编号)，
在已启动的 ARM64 KV260 上执行 XRT、ZOCL、FPGA Manager 和 Minimal PYNQ 初始化。
若发现新 Xilinx kernel，脚本会自动重启目标并继续 Runtime Stage 2。
EOF
  exit 2
}

[[ $# -eq 1 ]] || usage
BOARD_ID="$1"
[[ "$BOARD_ID" =~ ^([1-9]|1[0-9]|20)$ ]] || die "KV260 编号必须在 1-20 之间: $BOARD_ID"
[[ -d "$RUNTIME_DIR" ]] || die "找不到 Runtime 目录: $RUNTIME_DIR"
[[ -d "$CHECK_DIR" ]] || die "找不到检查脚本目录: $CHECK_DIR"
for command in ssh tar tee timeout; do
  command -v "$command" >/dev/null || die "缺少命令: $command"
done

LAST_OCTET=$((81 + BOARD_ID))
HOSTNAME="kv260${BOARD_ID}"
IP_ADDRESS="192.168.31.${LAST_OCTET}"
SSH_TARGET="ubuntu@${IP_ADDRESS}"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${HOSTNAME}.log"
KNOWN_HOSTS="$LOG_DIR/known_hosts"
SSH_OPTIONS=(
  -o StrictHostKeyChecking=accept-new
  -o UserKnownHostsFile="$KNOWN_HOSTS"
  -o ConnectTimeout=10
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
  -o ControlMaster=auto
  -o ControlPersist=120
  -o ControlPath="$LOG_DIR/ssh-%C"
)

cat <<EOF | tee -a "$LOG_FILE"
========================================
KV260 Runtime Initialization
========================================
Board ID: $BOARD_ID
Hostname: $HOSTNAME
IP: $IP_ADDRESS
SSH: $SSH_TARGET
========================================
EOF

echo "正在测试 SSH；使用密码认证时请输入 ubuntu 用户当前密码。" | tee -a "$LOG_FILE"
REMOTE_HOSTNAME=$(ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" hostname)
[[ "$REMOTE_HOSTNAME" == "$HOSTNAME" ]] || \
  die "目标主机名不匹配：预期 $HOSTNAME，实际 $REMOTE_HOSTNAME；已停止 Runtime 安装"
echo "SSH target verified: $REMOTE_HOSTNAME" | tee -a "$LOG_FILE"

upload_runtime() {
  echo "正在上传 Runtime 和检查脚本..." | tee -a "$LOG_FILE"
  tar -C "$SCRIPT_DIR" -czf - runtime scripts | \
    ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
      'rm -rf /tmp/kv260-runtime && mkdir -p /tmp/kv260-runtime && tar -xzf - -C /tmp/kv260-runtime'
}

run_runtime() {
  echo "正在目标 KV260 上执行 Runtime Factory..." | tee -a "$LOG_FILE"
  ssh -tt "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    'sudo /tmp/kv260-runtime/runtime/install_runtime.sh' 2>&1 | tee -a "$LOG_FILE"
  local remote_rc=${PIPESTATUS[0]}
  return "$remote_rc"
}

ssh_port_open() {
  timeout 2 bash -c "exec 3<>/dev/tcp/${IP_ADDRESS}/22" >/dev/null 2>&1
}

wait_for_reboot() {
  local deadline

  echo "Runtime 请求重启；正在重启 $HOSTNAME 并等待继续 Stage 2..." | tee -a "$LOG_FILE"
  set +e
  ssh -tt "${SSH_OPTIONS[@]}" "$SSH_TARGET" 'sudo systemctl reboot'
  ssh "${SSH_OPTIONS[@]}" -O exit "$SSH_TARGET" >/dev/null 2>&1
  set -e

  deadline=$((SECONDS + 180))
  while ssh_port_open; do
    (( SECONDS < deadline )) || die "已请求 reboot，但 SSH 端口在 180 秒内没有关闭"
    sleep 3
  done
  printf '[%s] %s is rebooting\n' "$(date -Is)" "$HOSTNAME" | tee -a "$LOG_FILE"

  deadline=$((SECONDS + 1800))
  until ssh_port_open; do
    (( SECONDS < deadline )) || die "重启后 30 分钟内 SSH 未恢复"
    printf '[%s] waiting for %s after reboot ...\n' "$(date -Is)" "$HOSTNAME" | tee -a "$LOG_FILE"
    sleep 5
  done

  REMOTE_HOSTNAME=$(ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" hostname)
  [[ "$REMOTE_HOSTNAME" == "$HOSTNAME" ]] || \
    die "重启后目标主机名不匹配：预期 $HOSTNAME，实际 $REMOTE_HOSTNAME"
  echo "Reboot complete; SSH target verified: $REMOTE_HOSTNAME" | tee -a "$LOG_FILE"
}

upload_runtime
set +e
run_runtime
runtime_rc=$?
set -e

if (( runtime_rc == 75 )); then
  wait_for_reboot
  upload_runtime
  set +e
  run_runtime
  runtime_rc=$?
  set -e
fi

(( runtime_rc == 0 )) || die "Runtime Factory 失败，远端退出码: $runtime_rc；请查看 $LOG_FILE"

cat <<EOF | tee -a "$LOG_FILE"
========================================
KV260 Runtime Initialization Complete
========================================
Board: $HOSTNAME
IP: $IP_ADDRESS
System / FPGA Manager: OK
XRT userspace: OK
ZOCL Driver: OK
Minimal PYNQ: OK
Overlay Hardware Test: see Runtime report (NOT RUN when design files are unavailable)
Log: $LOG_FILE
========================================
EOF
