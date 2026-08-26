# KV260 SD 卡制作与基础运行环境部署说明

本文面向需要从一张 SD 卡开始部署 KV260 的操作人员，覆盖以下流程：

```text
SD 卡镜像制作
      ↓
Board ID / hostname / 网络配置
      ↓
KV260 首次启动
      ↓
Runtime Factory
      ↓
XRT / ZOCL / pyxrt / PYNQ
      ↓
基础 Runtime 验收
```

本文不展开 Central Server、Session Scheduler、学生 Artifact 管理或业务 Worker API。总体架构见 [`KV260_PYNQ_Framework.md`](KV260_PYNQ_Framework.md)，Central Server 的安装和使用见 [`KV260_Server_Usage_Guide.md`](KV260_Server_Usage_Guide.md)，PYNQ/XRT/Overlay 技术基础见 [`KV260_PYNQ_Architecture_Notes.md`](KV260_PYNQ_Architecture_Notes.md)。

> 写卡会完整覆盖目标设备。每次插入或更换 SD 卡后都必须重新执行 `lsblk`，不能沿用上一次的 `/dev/sdX` 判断。

## 1. 当前实测基线

当前已在 `kv2602` 完成实机验证：

```text
hostname:       kv2602
IPv4:           192.168.31.83/24
gateway:        192.168.31.1
DNS:            223.5.5.5, 192.168.31.1
OS:             Ubuntu 24.04 Noble
architecture:   aarch64
kernel:         6.8.0-1035-xilinx
FPGA Manager:   operating
XRT:            2.18.0-0ubuntu1
xrt-dkms:       2.18.0-0ubuntu1
PYNQ:           3.1.2
```

已验证通过：Runtime Factory、ZOCL 加载、`pyxrt` 的系统与 PYNQ venv 导入、`PYNQ ON_TARGET=True`、`EmbeddedDevice`、真实 `allocate()`、XRT device enumeration，以及重启后的 Runtime persistence。当前实机还可看到 `/dev/dri/renderD128`，`xrt-smi` 报告 KV260 且 `Device Ready: Yes`。

## 2. 推荐部署流程

```text
部署 PC
  ↓
prepare_kv260_image.sh
  ↓
SD Card
  ↓
插入 KV260 并上电
  ↓
boot / cloud-init
  ↓
runtime_init_kv260.sh <Board ID>
  ↓
XRT → ZOCL → pyxrt → Minimal PYNQ → PYNQ DT
  ↓
allocate() 功能验证
  ↓
Runtime OK
```

普通部署主要使用两个入口：

```bash
sudo ./prepare_kv260_image.sh <image> <disk> <Board ID>
./runtime_init_kv260.sh <Board ID>
```

可选的一键入口会串联写卡、等待板卡上线和 Runtime Factory：

```bash
sudo ./deploy_kv260.sh <Board ID> <disk> <image>
```

例如：

```bash
sudo ./deploy_kv260.sh \
  2 \
  /dev/sdb \
  ./iot-limerick-kria-classic-server-2404-classic-24.04-x07-20250423.img
```

## 3. 制作 SD 卡

### 3.1 确认设备与镜像

每次插卡后重新检查：

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS,MODEL,SERIAL
```

目标参数必须是整盘设备，例如 `/dev/sdb`，不能是 `/dev/sdb1` 或 `/dev/sdb2`。如果分区被桌面自动挂载，先卸载：

```bash
sudo umount /dev/sdb2 2>/dev/null || true
sudo umount /dev/sdb1 2>/dev/null || true
```

检查原始镜像与目标容量：

```bash
stat -c '%n %s bytes' \
  ./iot-limerick-kria-classic-server-2404-classic-24.04-x07-20250423.img
lsblk -b -dn -o NAME,SIZE /dev/sdb
```

脚本接收未压缩的 `.img`。只有 `.img.xz` 时先解压：

```bash
xz -dk --verbose \
  ./iot-limerick-kria-classic-server-2404-classic-24.04-x07-20250423.img.xz
```

### 3.2 运行 Image Factory

制作 Board ID 2：

```bash
sudo ./prepare_kv260_image.sh \
  ./iot-limerick-kria-classic-server-2404-classic-24.04-x07-20250423.img \
  /dev/sdb \
  2
```

Board ID 2 对应：

```text
hostname = kv2602
IP       = 192.168.31.83
```

开始写盘前，脚本会显示 Board ID、hostname、IP、镜像和目标整盘设备，并要求两次确认。随后它会写入镜像、扩展 ext4 rootfs，并配置 hostname、静态网络、`ubuntu` 用户、SSH、NoCloud/netplan 和首次启动任务。

脚本完成后执行：

```bash
sync
```

等待 `sync` 返回，再安全拔卡。将 SD 卡插入对应 KV260，接好网络并上电。

## 4. 首次启动检查

以 Board ID 2 为例：

```bash
ping 192.168.31.83
ssh ubuntu@192.168.31.83
```

在 KV260 内执行：

```bash
cloud-init status --wait
hostname
hostname -I
ip addr show eth0
ip route
resolvectl status eth0
uname -r
uname -m
cat /sys/class/fpga_manager/fpga0/state
grep -E '^Cma(Total|Free):' /proc/meminfo
```

预期检查表：

| 项目 | 预期 |
| --- | --- |
| cloud-init | `done` |
| hostname | 与 Board ID 对应，例如 `kv2602` |
| IP | 与 Board ID 对应，例如 `192.168.31.83` |
| architecture | `aarch64` |
| kernel | 名称包含 `xilinx` |
| FPGA Manager | `operating` |
| CMA | 存在；当前实测约 800 MiB |

`cloud-init` 返回码 `2` 表示带 recoverable warnings 完成，可以继续；其他非零结果应先检查 `cloud-init status --long`。完成后执行 `exit` 返回部署 PC。

## 5. 运行 Runtime Factory

回到部署 PC 的仓库根目录：

```bash
./runtime_init_kv260.sh 2
```

launcher 会核对远端 hostname，将 `runtime/` 和 `scripts/` 上传到目标板，然后依次执行：

```text
[preflight] System and FPGA Manager
[1/7] XRT userspace
[2/7] XRT-matched ZOCL DKMS driver
[3/7] XRT-matched Python 3.12 pyxrt binding
[4/7] Minimal PYNQ 3.1.2 packages and runtime assets
[5/7] PYNQ device tree and boot services
[6/7] Minimal PYNQ functional validation
[7/7] Final diagnostics and worker runtime report
```

如果 Runtime 返回 `REBOOT_REQUIRED`，PC 端 `runtime_init_kv260.sh` 会自动执行：

```text
reboot → wait SSH → verify hostname → upload runtime → continue Runtime Factory
```

正常使用者不需要手工续跑。密码认证时可能在首次连接和自动重启后再次要求密码。完整日志写入：

```text
logs/kv260N.log
```

验证失败时，优先查看该日志以及失败 stage 输出的 systemd 或 validation 信息。

## 6. 验证方法

### 6.1 Runtime Factory 成功验证

最终报告的重点应包括：

```text
System:                    OK
Xilinx Kernel:             OK
Architecture:              OK
CMA:                       OK
FPGA Manager:              OK
XRT / xrt-dkms:            OK
ZOCL Driver:               OK
pyxrt:                     OK
PYNQ DT/runtime:            OK
PYNQ ON_TARGET:            True
Device:                    EmbeddedDevice
allocate:                  OK (...)
Minimal PYNQ:              OK
KV260 PYNQ Worker Runtime: OK
KV260 Runtime Factory Complete
```

`allocate: OK` 不是 import-only 检查。验证脚本实际执行：

```text
allocate → write → flush → invalidate → readback → freebuffer
```

### 6.2 重启持久化验证

Runtime Factory 成功后重启 KV260：

```bash
sudo reboot
```

重新 SSH 登录后检查两个 service：

```bash
systemctl is-active kv260-pynq-dt.service
systemctl is-active kv260-pynq-clear-pl-state.service
```

预期：

```text
active
active
```

继续检查目标标记、ZOCL 和 render node：

```bash
echo -n "pynq_board="
tr -d '\0' </proc/device-tree/chosen/pynq_board
echo

lsmod | grep '^zocl'
ls -l /dev/dri/renderD128
```

当前实测预期至少看到 `pynq_board=KV260`、已加载的 `zocl` 和 `/dev/dri/renderD128`。最后执行完整功能验证：

```bash
sudo env \
  XILINX_XRT=/usr \
  BOARD=KV260 \
  /opt/kv260-pynq/bin/python \
  /opt/kv260-pynq/share/kv260-runtime/validate_runtime.py
```

预期重点：

```text
PYNQ ON_TARGET: True
Overlay import: OK
MMIO import: OK
Device: EmbeddedDevice
allocate: OK (...)
PYNQ Core Runtime: OK
```

重启后仍通过以上检查，才表示 Runtime persistence 验证完成。

### 6.3 实际 FPGA Overlay 验证

应用 FPGA 文件默认放置在：

```text
/opt/fpga/design.bit
/opt/fpga/design.hwh
```

两者必须来自同一次 Vivado build，并使用相同 basename。验证命令：

```bash
sudo env \
  XILINX_XRT=/usr \
  BOARD=KV260 \
  /opt/kv260-pynq/bin/python \
  /opt/kv260-pynq/share/kv260-runtime/validate_runtime.py \
  --bit /opt/fpga/design.bit
```

它会检查 Overlay Load、HWH Parse、IP dictionary、`allocate()`，以及设计中存在 AXI DMA 时的 DMA discovery。

没有 `design.bit`/`design.hwh` 时：

```text
Overlay Hardware Test: NOT RUN (design files unavailable)
```

这是正常状态：Minimal Runtime 已通过，只是尚未验证具体 FPGA design。真实 DMA send/receive 必须由符合应用硬件协议的测试程序验证。

# 附录 A：Runtime Factory 安装内容

## A.1 XRT / ZOCL

Runtime Factory 启用 Noble 对应的 `ppa:ubuntu-xilinx/sdk` binary 与 source repository，并通过 dpkg/DKMS 管理：

```text
dkms                缺失时由 Runtime Factory 自动安装
xrt                 2.18.0-0ubuntu1
xrt-dkms            2.18.0-0ubuntu1
zocl.ko             当前 Xilinx kernel 的 DKMS module
linux-headers-*     当前及待启动 Xilinx kernel headers（需要时）
```

XRT 与 `xrt-dkms` 必须使用完全一致的 Debian version。
Runtime Factory 会自动安装 DKMS，用户不需要提前手工执行 `apt install dkms`。

## A.2 pyxrt

`install_pyxrt.sh` 获取与当前 XRT Debian version 完全匹配的 `xilinx-runtime` source package，并编译：

```text
src/python/pybind11/src/pyxrt.cpp
```

Python 3.12 binding 安装到系统 local platlib：

```text
/usr/local/lib/python3.12/dist-packages/
└── pyxrt.cpython-312-aarch64-linux-gnu.so
```

它不会安装到 `/opt/kv260-pynq/local/`。PYNQ venv 使用 `--system-site-packages` 加载系统 `pyxrt`。

## A.3 Minimal PYNQ

持久化 venv：

```text
/opt/kv260-pynq
```

当前固定主要版本：

| Package | Version |
| --- | --- |
| PYNQ | `3.1.2` |
| pynqmetadata | `0.1.9` |
| pynqutils | `0.1.2` |
| numpy | `1.26.4` |
| pycparser | `2.22` |
| setuptools | `80.0.0` |
| grpcio | `1.64.0` |
| grpcio-tools | `1.64.0` |

安装阶段使用 `PYNQ_REMOTE=1` 跳过本项目不需要的 multimedia native extensions。

## A.4 PYNQ Runtime / systemd

```text
/opt/kv260-pynq/share/kv260-runtime/
├── pynq.dts
├── pynq.dtbo
├── insert_dtbo.py
├── clear_pl_state.py
└── validate_runtime.py

/etc/profile.d/kv260-pynq.sh
/etc/xocl.txt

/etc/systemd/system/
├── kv260-pynq-dt.service
└── kv260-pynq-clear-pl-state.service
```

`pynq.dts`/`pynq.dtbo` 提供 live Device Tree 中的 `xlnx,zocl` 和 `/chosen/pynq_board = "KV260"`，使 PYNQ 得到 `ON_TARGET=True` 并选择 `EmbeddedDevice`。clear PL state service 在启动时清理 stale PYNQ PL metadata。

## A.5 构建和 Runtime 依赖

当前 Runtime 脚本按需安装：

```text
dkms
software-properties-common
build-essential
g++
python3-dev
python3-pip
python3-venv
pybind11-dev
uuid-dev
libboost-dev
dpkg-dev
device-tree-compiler
libdrm-dev
libffi-dev
libssl-dev
linux-headers-<Xilinx kernel>
```

其中 `software-properties-common` 只在缺少 `add-apt-repository` 时安装；kernel headers 只在对应 kernel build tree 缺失时安装。

## A.6 明确没有安装什么

Minimal Runtime 不主动部署 Jupyter、JupyterLab、Notebook、PYNQ 示例、Base Overlay、DPU-PYNQ、OpenCV/摄像头 Demo、MicroBlaze 开发环境、旧 `libxlnk`、旧 `libcma`/SDSoC runtime，或 Ubuntu universe 中的旧 `python3-xrt`。

# 附录 B：Board ID 与地址规则

```text
N = 1 ... 20

hostname = kv260N
IP       = 192.168.31.(81 + N)/24
gateway  = 192.168.31.1
DNS      = 223.5.5.5, 192.168.31.1
```

| Board ID | Hostname | IP |
| ---: | --- | --- |
| 1 | `kv2601` | `192.168.31.82` |
| 2 | `kv2602` | `192.168.31.83` |
| 3 | `kv2603` | `192.168.31.84` |
| … | … | … |
| 20 | `kv26020` | `192.168.31.101` |

不同 KV260 不能使用相同 Board ID，否则会产生 hostname 和 IP 冲突。
