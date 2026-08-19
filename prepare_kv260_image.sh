#!/usr/bin/env bash
set -Eeuo pipefail

# Write and prepare a KV260 Ubuntu image.
# Usage: sudo ./prepare_kv260_image.sh image.img /dev/sda 20

IMAGE_PATH="${1:-}"
DISK="${2:-}"
LAST_OCTET="${3:-}"

die() {
  echo "错误: $*" >&2
  exit 1
}

usage() {
  echo "用法: sudo $0 image.img /dev/sda IP最后一段" >&2
  echo "示例: sudo $0 kv260.img /dev/sda 20" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
[[ $EUID -eq 0 ]] || die "请使用 root 运行此脚本"
[[ -f "$IMAGE_PATH" ]] || die "镜像文件不存在: $IMAGE_PATH"
[[ -b "$DISK" ]] || die "目标不是块设备: $DISK"
[[ "$LAST_OCTET" =~ ^([1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])$ ]] || \
  die "IP 最后一段必须是 1-254: $LAST_OCTET"

for command in blockdev dd partprobe udevadm lsblk mount umount mountpoint \
  growpart e2fsck resize2fs blkid curl gpg usermod groupmod chpasswd; do
  command -v "$command" >/dev/null || die "缺少命令: $command"
done

partition_path() {
  case "$1" in
    /dev/mmcblk*|/dev/nvme*|/dev/loop*) printf '%sp%s\n' "$1" "$2" ;;
    *) printf '%s%s\n' "$1" "$2" ;;
  esac
}

ROOT_PART=$(partition_path "$DISK" 2)
BOOT_PART=$(partition_path "$DISK" 1)
IMAGE_BYTES=$(stat -c '%s' "$IMAGE_PATH")
DISK_BYTES=$(blockdev --getsize64 "$DISK")
(( DISK_BYTES >= IMAGE_BYTES )) || die "目标磁盘容量小于镜像文件"

while read -r device mount_path; do
  [[ -z "${mount_path:-}" ]] || die "目标设备或分区已挂载: $device -> $mount_path"
done < <(lsblk -nrpo NAME,MOUNTPOINT "$DISK" | awk 'NF > 1 {print $1, $2}')

echo "即将覆盖目标设备: $DISK"
echo "镜像文件: $IMAGE_PATH"
echo "目标容量: $((DISK_BYTES / 1024 / 1024)) MiB"
read -r -p "请输入目标设备路径以确认写盘: " confirmation
[[ "$confirmation" == "$DISK" ]] || die "确认失败，已取消写盘"

echo "正在写入镜像..."
dd if="$IMAGE_PATH" of="$DISK" bs=16M conv=fsync status=progress
sync
partprobe "$DISK"
udevadm settle

[[ -b "$ROOT_PART" ]] || die "写盘后找不到 root 分区: $ROOT_PART"
[[ -b "$BOOT_PART" ]] || die "写盘后找不到启动分区: $BOOT_PART"
[[ "$(blkid -o value -s TYPE "$ROOT_PART")" == "ext4" ]] || \
  die "root 分区不是 ext4: $ROOT_PART"

echo "正在扩展 root 分区..."
growpart "$DISK" 2
partprobe "$DISK"
udevadm settle
e2fsck -f -p "$ROOT_PART" || {
  status=$?
  [[ $status -eq 1 ]] || die "e2fsck 失败，退出码: $status"
}
resize2fs "$ROOT_PART"

ROOT_MNT=$(mktemp -d /tmp/kv260-root.XXXXXX)
BOOT_MNT=$(mktemp -d /tmp/kv260-boot.XXXXXX)
mounted_root=0
mounted_boot=0
cleanup() {
  sync || true
  if (( mounted_boot )); then umount "$BOOT_MNT" || true; fi
  if (( mounted_root )); then umount "$ROOT_MNT" || true; fi
  rmdir "$BOOT_MNT" "$ROOT_MNT" 2>/dev/null || true
}
trap cleanup EXIT

mount -o rw "$ROOT_PART" "$ROOT_MNT"
mounted_root=1
mount -o rw "$BOOT_PART" "$BOOT_MNT"
mounted_boot=1

TUNA_APT='http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports'
TUNA_APT_HTTPS='https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports'
TUNA_PIP='https://pypi.tuna.tsinghua.edu.cn/simple'
KRIA_PPA='http://ppa.launchpadcontent.net/ubuntu-xilinx/kria/ubuntu/'
KRIA_PPA_HTTPS='https://ppa.launchpadcontent.net/ubuntu-xilinx/kria/ubuntu/'
KRIA_KEYRING="$ROOT_MNT/etc/apt/keyrings/ubuntu-xilinx-kria.gpg"
KRIA_KEY_FINGERPRINT='803DDF595EA7B6644F9B96B752150A179A9E84C9'
IP="192.168.31.${LAST_OCTET}/24"
HOSTNAME="KV260-${LAST_OCTET}"
INSTANCE_ID="kv260-${LAST_OCTET}"

NETWORK_CONFIG=$(cat <<EOF
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    wired:
      match:
        name: "e*"
      dhcp4: false
      addresses:
        - ${IP}
      routes:
        - to: default
          via: 192.168.31.1
EOF
)

# Keep both NoCloud locations synchronized. KV260 firmware images may read
# the seed from the boot partition before using the rootfs seed.
install -d -m 0755 "$ROOT_MNT/var/lib/cloud/seed/nocloud"
printf '%s\n' "$NETWORK_CONFIG" > "$ROOT_MNT/var/lib/cloud/seed/nocloud/network-config"
printf '%s\n' "$NETWORK_CONFIG" > "$BOOT_MNT/network-config"

cat > "$ROOT_MNT/var/lib/cloud/seed/nocloud/meta-data" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: ${HOSTNAME}
EOF
cat > "$BOOT_MNT/meta-data" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: ${HOSTNAME}
EOF

# Rename the original image account while preserving its UID and home data.
if grep -q '^kv:' "$ROOT_MNT/etc/passwd"; then
  grep -q '^ubuntu:' "$ROOT_MNT/etc/passwd" && die "ubuntu 用户已存在，无法将 kv 重命名为 ubuntu"
  usermod --root "$ROOT_MNT" --login ubuntu --home /home/ubuntu --move-home kv
  if grep -q '^kv:' "$ROOT_MNT/etc/group"; then
    groupmod --root "$ROOT_MNT" --new-name ubuntu kv
  fi
  sed -i 's/\(^gpio:[^:]*:[^:]*:\).*\bkv\b/\1ubuntu/' "$ROOT_MNT/etc/group"
fi
printf 'ubuntu:ubuntu\n' | chpasswd --root "$ROOT_MNT"

# Hostname and passwordless sudo are configured in the image immediately;
# cloud-init repeats the relevant settings on first boot.
printf '%s\n' "$HOSTNAME" > "$ROOT_MNT/etc/hostname"
if grep -qE '^[[:space:]]*127\.0\.1\.1[[:space:]]' "$ROOT_MNT/etc/hosts"; then
  sed -i "s/^[[:space:]]*127\.0\.1\.1[[:space:]].*/127.0.1.1\t${HOSTNAME}/" "$ROOT_MNT/etc/hosts"
else
  printf '127.0.1.1\t%s\n' "$HOSTNAME" >> "$ROOT_MNT/etc/hosts"
fi
install -d -m 0755 "$ROOT_MNT/etc/sudoers.d"
printf 'ubuntu ALL=(ALL) NOPASSWD:ALL\n' > "$ROOT_MNT/etc/sudoers.d/90-ubuntu-nopasswd"
chmod 0440 "$ROOT_MNT/etc/sudoers.d/90-ubuntu-nopasswd"

install -d -m 0755 "$ROOT_MNT/etc/ssh/sshd_config.d"
cat > "$ROOT_MNT/etc/ssh/sshd_config.d/99-kv260.conf" <<'EOF'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin no
EOF
chmod 0644 "$ROOT_MNT/etc/ssh/sshd_config.d/99-kv260.conf"

# Use TUNA for Ubuntu packages. HTTP is only the bootstrap transport used
# while ca-certificates is refreshed on first boot.
if [[ -f "$ROOT_MNT/etc/apt/sources.list.d/ubuntu.sources" ]]; then
  sed -i "s|https\?://ports.ubuntu.com/ubuntu-ports|${TUNA_APT}|g" \
    "$ROOT_MNT/etc/apt/sources.list.d/ubuntu.sources"
fi
if [[ -f "$ROOT_MNT/etc/cloud/cloud.cfg" ]]; then
  sed -i "s|https\?://ports.ubuntu.com/ubuntu-ports|${TUNA_APT}|g" \
    "$ROOT_MNT/etc/cloud/cloud.cfg"
fi

# Install the Kria PPA signing key and use it explicitly.
install -d -m 0755 "$ROOT_MNT/etc/apt/keyrings"
curl -fsSL \
  "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${KRIA_KEY_FINGERPRINT}&options=mr" \
  | gpg --dearmor > "$KRIA_KEYRING"
chmod 0644 "$KRIA_KEYRING"
CODENAME=$(sed -n 's/^VERSION_CODENAME=//p' "$ROOT_MNT/etc/os-release" | tr -d '"' || true)
CODENAME="${CODENAME:-noble}"
cat > "$ROOT_MNT/etc/apt/sources.list.d/ubuntu-xilinx-ubuntu-kria.sources" <<EOF
Types: deb
URIs: ${KRIA_PPA}
Suites: ${CODENAME}
Components: main
Signed-By: /etc/apt/keyrings/ubuntu-xilinx-kria.gpg
EOF
chmod 0644 "$ROOT_MNT/etc/apt/sources.list.d/ubuntu-xilinx-ubuntu-kria.sources"

# Temporarily allow bootstrap package installation when old CA/signatures
# prevent the first apt update. cloud-init removes this after CA refresh.
install -d -m 0755 "$ROOT_MNT/etc/apt/apt.conf.d"
cat > "$ROOT_MNT/etc/apt/apt.conf.d/99-cloud-init-temporary-ca-bootstrap" <<'EOF'
Acquire::AllowInsecureRepositories "true";
Acquire::AllowDowngradeToInsecureRepositories "true";
APT::Get::AllowUnauthenticated "true";
EOF

# System-wide pip mirror.
cat > "$ROOT_MNT/etc/pip.conf" <<EOF
[global]
index-url = ${TUNA_PIP}
[install]
index-url = ${TUNA_PIP}
EOF
chmod 0644 "$ROOT_MNT/etc/pip.conf"

# Clear old cloud-init state so this image is treated as a new NoCloud image.
rm -rf "$ROOT_MNT/var/lib/cloud/instances/"* \
       "$ROOT_MNT/var/lib/cloud/data/"*
if [[ -L "$ROOT_MNT/var/lib/cloud/instance" || -d "$ROOT_MNT/var/lib/cloud/instance" ]]; then
  rm -rf "$ROOT_MNT/var/lib/cloud/instance"
fi
find "$ROOT_MNT/etc/netplan" -maxdepth 1 -type f \
  \( -name '*cloud-init*.yaml' -o -name '*cloud-init*.yml' \) -delete 2>/dev/null || true

USER_DATA="$ROOT_MNT/var/lib/cloud/seed/nocloud/user-data"
cat > "$USER_DATA" <<EOF
#cloud-config
apt_preserve_sources_list: true
package_update: true
packages:
  - openssh-server
  - ca-certificates
hostname: ${HOSTNAME}
ssh_pwauth: true
user:
  name: ubuntu
  sudo: "ALL=(ALL) NOPASSWD:ALL"
chpasswd:
  expire: false
  users:
    - name: ubuntu
      password: ubuntu
      type: text
runcmd:
  - apt-get update
  - DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y ca-certificates
  - sed -i 's|${TUNA_APT}|${TUNA_APT_HTTPS}|g; s|${KRIA_PPA}|${KRIA_PPA_HTTPS}|g' /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu-xilinx-ubuntu-kria.sources
  - rm -f /etc/apt/apt.conf.d/99-cloud-init-temporary-ca-bootstrap
  - systemctl enable --now ssh.service || systemctl enable --now ssh
EOF
chmod 0644 "$USER_DATA"

sync
echo "已完成: $DISK"
echo "镜像: $IMAGE_PATH"
echo "IP: $IP"
echo "主机名: $HOSTNAME"
echo "用户/密码: ubuntu/ubuntu"
echo "SSH: 已启用密码登录，ubuntu 免密 sudo"
