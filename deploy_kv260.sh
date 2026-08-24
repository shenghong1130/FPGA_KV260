#!/usr/bin/env bash
# End-to-end orchestrator: SD image factory on this host, then runtime factory on KV260.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREPARE_SCRIPT="$SCRIPT_DIR/prepare_kv260_image.sh"
RUNTIME_INIT_SCRIPT="$SCRIPT_DIR/runtime_init_kv260.sh"
LOG_DIR="$SCRIPT_DIR/logs"
WAIT_TIMEOUT_SECONDS=1800

die() {
  echo "错误: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<EOF
用法:
  sudo $0 <KV260编号> <目标SD卡> <原始镜像>

示例:
  sudo $0 2 /dev/sdb kv260.img

KV260编号范围为 1-20。脚本使用同一个原始镜像写入 SD 卡，随后等待
kv260N 上线，并通过 SSH 在目标 KV260（ARM64）上安装 Runtime。
EOF
  exit 2
}

[[ $# -eq 3 ]] || usage
[[ $EUID -eq 0 ]] || die "请使用 sudo 运行此脚本"

BOARD_ID="$1"
DISK="$2"
IMAGE_PATH="$3"
[[ "$BOARD_ID" =~ ^([1-9]|1[0-9]|20)$ ]] || die "KV260 编号必须在 1-20 之间: $BOARD_ID"
[[ -b "$DISK" ]] || die "目标不是块设备: $DISK"
[[ -f "$IMAGE_PATH" ]] || die "镜像文件不存在: $IMAGE_PATH"
[[ -x "$PREPARE_SCRIPT" ]] || die "找不到可执行写卡脚本: $PREPARE_SCRIPT"
[[ -x "$RUNTIME_INIT_SCRIPT" ]] || die "找不到可执行 Runtime 入口: $RUNTIME_INIT_SCRIPT"
for command in lsblk ping tee; do
  command -v "$command" >/dev/null || die "缺少命令: $command"
done

LAST_OCTET=$((81 + BOARD_ID))
HOSTNAME="kv260${BOARD_ID}"
IP_ADDRESS="192.168.31.${LAST_OCTET}"

while read -r device mount_path; do
  [[ -z "${mount_path:-}" ]] || die "目标设备或分区已挂载: $device -> $mount_path"
done < <(lsblk -nrpo NAME,MOUNTPOINT "$DISK" | awk 'NF > 1 {print $1, $2}')

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${HOSTNAME}.log"

cat <<EOF
========================================
KV260 Deployment
========================================
Board:
  $HOSTNAME
IP:
  $IP_ADDRESS
SD:
  $DISK
Image:
  $IMAGE_PATH
Log:
  $LOG_FILE
========================================
EOF

read -r -p "输入 DEPLOY 确认写入 $DISK 并继续一键部署: " deploy_confirmation
[[ "$deploy_confirmation" == "DEPLOY" ]] || die "确认失败，已取消部署"

printf '\n[%s] SD image factory started for %s\n' "$(date -Is)" "$HOSTNAME" | tee -a "$LOG_FILE"
"$PREPARE_SCRIPT" "$IMAGE_PATH" "$DISK" "$BOARD_ID" 2>&1 | tee -a "$LOG_FILE"

cat <<EOF

SD 卡制作完成。请安全取出 SD 卡，将其插入 $HOSTNAME、接入网络并上电。
脚本将等待 $IP_ADDRESS 上线，最长等待 30 分钟。
EOF

deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
until ping -n -c 1 -W 1 "$IP_ADDRESS" >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    printf '[%s] TIMEOUT: %s did not answer ping\n' "$(date -Is)" "$IP_ADDRESS" | tee -a "$LOG_FILE" >&2
    die "KV260 未上线，请检查电源、网线、IP、SD 卡和启动日志"
  fi
  printf '[%s] waiting for %s (%s) ...\n' "$(date -Is)" "$HOSTNAME" "$IP_ADDRESS" | tee -a "$LOG_FILE"
  sleep 5
done

printf '[%s] %s is reachable\n' "$(date -Is)" "$HOSTNAME" | tee -a "$LOG_FILE"

cat <<EOF

目标已上线。现在调用 runtime_init_kv260.sh，通过 SSH 在 ARM64 KV260 上
完成 XRT、ZOCL、Minimal PYNQ 和 FPGA Manager 初始化；如果检测到新
Xilinx kernel，Runtime Factory 会重启目标并自动继续第二阶段。
EOF

"$RUNTIME_INIT_SCRIPT" "$BOARD_ID"

cat <<EOF | tee -a "$LOG_FILE"
========================================
KV260 Deployment Complete
========================================
Board ID: $BOARD_ID
Board: $HOSTNAME
IP: $IP_ADDRESS
System / FPGA Manager: OK
XRT userspace: OK
ZOCL Driver: OK
Minimal PYNQ: OK
Overlay Hardware Test: see Runtime report
Log: $LOG_FILE
========================================
EOF
