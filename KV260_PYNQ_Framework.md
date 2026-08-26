# 20 × KV260 PYNQ 共享计算平台总体架构

本文定义 20 × KV260 共享 PYNQ 计算平台的总体架构，包括 Central Server、Artifact、Session / Lease、KV260 Worker、PYNQ Overlay 和 FPGA 数据路径。

相关操作文档：

- SD 卡制作、板卡初始化和 Runtime Factory：[KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)
- Central Server 安装、启动和测试：[KV260_Server_Usage_Guide.md](KV260_Server_Usage_Guide.md)
- PYNQ / XRT / Overlay 基础原理：[KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)

## 1. 项目目标与当前边界

平台由一台 Central Server 和最多 20 台 KV260 组成。20 台板卡构成共享资源池，每台 KV260 同一时刻只租给一个 Session，并在该 Session 内串行执行 FPGA 请求。

核心原则：

- Scheduler 的调度单位是 **Session / Lease**，不是单次 Job；
- Scheduler 只在 Session 创建或队列分配时选择 Worker；
- 学生 Artifact 每个 Session 只部署一次；
- Session 建立后，多次 predict 固定路由到同一块 KV260；
- 只有主动 release 后，Worker 才重新成为 `IDLE`；
- 单板 `concurrency = 1`，整个平台最多约 20 个并存的 FPGA Session；
- 核心计算模型是 PYNQ Overlay + MMIO / AXI DMA，不是 Vitis xclbin kernel scheduler。

仓库当前已经实现 `server/` 中的 Central Server V1 和 Mock Worker；真实 KV260 业务 Worker 仍属于后续阶段。基础 KV260 Runtime 已经实机验证，不等于业务 Worker 已经实现。

## 2. 系统总体架构

```text
                     Student / Client
                           │
                    Artifact Upload
                           │
                           ▼
                  ┌─────────────────┐
                  │ Artifact Store  │
                  └────────┬────────┘
                           │
                    POST /sessions
                           │
                           ▼
                  Central Scheduler
                           │
                 random IDLE Worker
                           │
                 atomic reservation
                           │
                           ▼
                      KV260 Worker
                           │
                   deploy Artifact once
                           │
                  load PYNQ Overlay
                           │
                           ▼
                      Session READY
                           │
               ┌───────────┴───────────┐
               │                       │
           predict #1              predict #N
               │                       │
               └───────────┬───────────┘
                           │
                    同一个 KV260
                           │
                     AXI DMA / MMIO
                           │
                          FPGA
                           │
                       result
                           │
                           ▼
                     Session READY
                           │
                    explicit release
                           │
                           ▼
                      Worker IDLE
```

调度发生在 Central Server。单块 KV260 不维护其他板卡状态、全局 Session Queue 或跨板负载均衡。

## 3. Artifact Store

不同学生可以上传不同的 `design.bit` 和 `design.hwh`。Central Server 将每组文件保存为独立 Artifact，并持久化身份、版本、大小、SHA-256、HWH 解析状态和存储路径。

```text
Student
   ↓
Artifact
   ↓
Session
   ↓
Worker
```

Artifact 和 Worker 生命周期解耦：学生不永久绑定某块 KV260，Artifact 也不因一次 Session release 而删除。20 台 KV260 始终属于共享资源池。

上传阶段使用 `multipart/form-data`，并执行大小限制、哈希计算、HWH XML 校验、临时 staging 和原子 rename。用户文件名不直接作为服务端存储路径。

## 4. Central Scheduler 职责

Central Scheduler V1 负责：

- Artifact metadata 与 Artifact Store；
- Worker Registry 和 Health Check；
- FIFO Session Queue；
- Session / Lease 生命周期；
- 从真正 `IDLE` 的 Worker 中随机选择一块；
- 原子执行 `IDLE → RESERVED`；
- 向选中 Worker 部署 Artifact；
- 持久化 `session_id → worker_id`；
- 将 predict 固定转发到 Session 所属 Worker；
- 串行化同一 Session 的 predict 与 release；
- 处理 release、健康故障和恢复状态。

Scheduler **不会**在每次 predict 时重新选择 Worker。当前分配算法等价于从可用 `IDLE` 集合中执行随机选择，而不是 round-robin、LRU 或 Artifact cache preference。

## 5. Session / Lease 绑定

Session 是学生临时独占一块 KV260 的租约。创建成功后，Central Server 保存：

```text
session_id → artifact_id → worker_id
```

Session 不依赖持续不断的 HTTP/TCP 连接。例如：

```text
10:00 创建 Session
10:01 predict
10:05 predict
10:20 predict
11:00 predict
11:30 release
```

这些可以是完全独立的 HTTP 请求。只要 Session 仍为 `READY`，客户端就使用同一个 `session_id` 继续请求同一 Worker。

## 6. KV260 Worker 职责

每台 KV260 是一个独立 Worker Node：

```text
Ubuntu 24.04 / Xilinx kernel
              ↓
Minimal Kria-PYNQ Runtime
              ↓
PYNQ Worker Service
              ↓
Session Artifact
design.bit + design.hwh
              ↓
AXI DMA / MMIO / custom IP
              ↓
FPGA Accelerator
              ↓
result
```

Worker 负责本板 Session ownership、Artifact 部署、Overlay 初始化、请求校验、FPGA 计算、结果返回和 release。它不负责全局调度、其他 Worker 状态或 Session Queue。

## 7. Worker 内部 FPGA 计算路径

```text
POST /predict
      ↓
PYNQ Worker（Python）
      ├── MMIO：参数、启动、状态
      ├── allocate：DMA 可访问 buffer
      └── AXI DMA：输入/输出搬运
                   ↓
           FPGA Accelerator
                   ↓
              result buffer
                   ↓
              HTTP response
```

控制路径和数据路径必须分开：MMIO 适合寄存器，DMA 适合图像、tensor 或其他批量数据。Worker 应在可靠的清理路径释放 buffer，并在超时后将硬件置于可判断状态。

## 8. `design.bit` 与 `design.hwh`

- `design.bit` 配置 PL，决定 FPGA 中实际存在的硬件。
- `design.hwh` 描述 IP 名称、地址、寄存器、AXI 接口、中断和连接，供 PYNQ 解析。

两者必须来自同一次 Vivado build、使用同一 basename，并作为不可拆分的 Artifact。只更新其中一个属于部署错误。

Runtime Factory 的可选硬件验证默认检查 `/opt/fpga/design.bit` 和 `/opt/fpga/design.hwh`。业务平台中的学生 Artifact 则由 Central Server 在 Session 分配后通过 Worker `/internal/deploy` 下发，两者属于不同阶段。

## 9. Overlay 生命周期

当前 Session 模型的 Overlay 生命周期是：

```text
Worker Service 启动
        ↓
Worker IDLE
        ↓
Session 被分配
        ↓
RESERVED
        ↓
Central 下发该学生 Artifact
        ↓
DEPLOYING
        ↓
加载 design.bit
解析 design.hwh
绑定 DMA / MMIO / IP
        ↓
READY
        ↓
predict → BUSY → READY
        ↓
predict → BUSY → READY
        ↓
Session Release
        ↓
Worker IDLE
```

架构硬约束是：

```text
POST /sessions
      ↓
deploy Artifact once
      ↓
READY
      ↓
predict many times
```

禁止在每次 predict 中重新上传 bit/hwh 或调用 Overlay download。Release 时可以物理保留最后一个 Overlay；下一名学生的 `/internal/deploy` 会覆盖它。

## 10. AXI MMIO 与 AXI DMA

MMIO 负责尺寸、模式、启动位和完成状态等控制寄存器；AXI DMA 负责 PS DDR 与 AXI Stream Accelerator 之间的大块数据移动。Worker 必须根据实际 HWH 中的 IP 和接口取得 DMA 对象，不能把示例地址或名称当作通用事实。

DMA 完成条件通常同时涉及发送、接收和 Accelerator 状态。具体顺序由硬件协议决定，必须由真实业务 Worker 与 FPGA design 联调验证。

## 11. Buffer、CMA 与 `allocate`

KV260 的 DMA buffer 来自连续内存。Runtime 默认要求 `CmaTotal >= 256 MiB`，低于 512 MiB 给出容量警告；当前实测约 800 MiB 满足基础验证。

PYNQ 3.1.2 的 `allocate()` 通过 `Device` 和 XRT BO allocator 使用 CMA / PS DDR。Runtime 验收会真实执行 allocate、write、flush、invalidate、readback 和 freebuffer，而不是只验证 import。

实际 Overlay 加载后，HWH 提供的 PS DDR memory topology 可作为默认 allocation target；没有 design 文件时，Runtime Factory 使用 bootstrap PS DDR target 验证相同的 XRT BO 后端，并将 Overlay Hardware Test 明确标记为未运行。

## 12. Session Queue

V1 的全局排队对象是 Session，不是单次 predict：

```text
POST /sessions
      ↓
存在 IDLE Worker？
  ├── 是：随机选择并原子 RESERVED
  └── 否：Session → QUEUED
```

当任意 Session release，Worker 返回 `IDLE`，Scheduler 按 `created_at` 和 ID 的 FIFO 顺序处理等待项：

```text
QUEUED → RESERVED → DEPLOYING → READY
```

单个 Session 内的 predict 不重新进入全局 Session Queue，而是在该 Session 的固定 Worker 上串行执行。

## 13. Worker Registry

Registry 至少保存：

| 字段 | 含义 |
| --- | --- |
| `worker_id` | `kv2601` … `kv26020` 或 Mock Worker ID |
| `base_url` | Worker HTTP 地址 |
| `status` | `IDLE`、`RESERVED`、`DEPLOYING`、`READY`、`BUSY`、`ERROR`、`OFFLINE` |
| `current_session_id` | 当前占用 Worker 的 Session |
| `current_artifact_id` | 最近部署的 Artifact |
| `last_seen` | 最近一次成功健康检查时间 |
| `failure_count` | 连续健康检查失败次数 |

`READY` 表示 Worker 已被一个 Session 独占，并不表示可分配。健康检查会将可用且无远端 Session 的 Worker 置为 `IDLE`；Scheduler 的候选条件是数据库状态为 `IDLE` 且 `session_id` 为空。

## 14. Worker 状态机

```text
IDLE
  ↓
RESERVED
  ↓
DEPLOYING
  ↓
READY
  ↓
BUSY
  ↓
READY
  ↓
release process
  ↓
IDLE
```

故障状态为 `ERROR` 和 `OFFLINE`。当前代码的 Worker enum 不单独保存 `RELEASING`，release 过程由 Session 状态和操作锁表达；释放成功后 Worker 才切换为 `IDLE`。

## 15. Session 状态机

```text
QUEUED
  ↓
RESERVED
  ↓
DEPLOYING
  ↓
READY
  ↓
BUSY
  ↓
READY
  ↓
RELEASING
  ↓
CLOSED
```

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 暂无 `IDLE` Worker，等待 FIFO 分配 |
| `RESERVED` | 已原子占用一个 Worker |
| `DEPLOYING` | Artifact 正在部署，Overlay 正在初始化 |
| `READY` | 已独占 Worker，可以发送 predict |
| `BUSY` | 当前正在执行一次 predict；完成后回到 `READY` |
| `RELEASING` | 正在解除 Session ownership |
| `CLOSED` | release 完成，Session 已结束 |
| `FAILED` | 创建或部署阶段失败 |
| `LOST` | 活动 Session 因 Worker 严重故障而失去执行资源 |

最关键的语义是 `READY != IDLE`。`BUSY → READY` 也不释放 Worker；只有 `DELETE /sessions/{id}` 完成后，Worker 才回到 `IDLE`。

## 16. 分配策略与原子性

当前 V1 使用 `random.choice(idle_workers)` 的语义，从数据库状态为 `IDLE` 且 `session_id` 为空的 Worker 中随机选择一块；健康检查通过更新 Worker 状态将故障节点排除在这个集合外。全局分配锁保证查询候选和 `IDLE → RESERVED` 是一个原子临界区，避免两个并发 Session 占用同一块板。

Round Robin、LRU、Artifact cache preference 或能力分级可以作为未来优化，但不是当前 V1 行为。

## 17. 单板 `concurrency = 1`

一块 KV260 同一时刻只属于一个 Session，同一 Session 内一次只执行一个 FPGA predict。原因是 AXI DMA、MMIO 寄存器、共享 buffer、中断和 Accelerator 状态通常不是多租户资源。

Central Server 使用 per-session lock 串行化 predict 和 release；未来真实 Worker 仍应提供本地串行保护，防止绕过 Central 的请求并发访问硬件。

## 18. 20 板并行模型

如果 20 台健康 Worker 都各自被一个 Session 占用，平台可同时维持约 20 个 FPGA Session；每个 Session 内又能连续执行大量请求。第 21 个 Session 进入 `QUEUED`，直到某个已有 Session release。

因此，平台并行度来自多块物理 FPGA，而不是在单板中让多个 Session 同时操作同一个 DMA/IP。

## 19. Health Check、故障与恢复

Central Server 周期调用 Worker `/health` 和 `/status`，记录 `last_seen` 与连续失败次数。只有健康且 `IDLE` 的 Worker 可以被分配。

Active Session 不做透明 Worker migration。如果 `READY` 或 `BUSY` Worker 严重故障：

```text
Worker  → ERROR / OFFLINE
Session → FAILED / LOST
```

Central Server 不能偷偷换一块 KV260，因为 Overlay、DMA 状态和 Session ownership 已经绑定原 Worker。学生或客户端需要结束旧 Session，并重新申请新 Session。部署阶段失败也不会把一个已活动 Session 伪装为成功。

## 20. 网络与地址规则

```text
N = 1 ... 20
hostname = kv260N
IPv4 = 192.168.31.(81 + N)
gateway = 192.168.31.1
DNS = 223.5.5.5, 192.168.31.1
Worker port = 8080（业务 Worker 规划）
```

示例：

| Board ID | Hostname | IPv4 |
| ---: | --- | --- |
| 1 | `kv2601` | `192.168.31.82` |
| 2 | `kv2602` | `192.168.31.83` |
| 20 | `kv26020` | `192.168.31.101` |

完整烧卡和地址表见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

## 21. Runtime Factory

Runtime Factory 只负责建立板卡基础能力：

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

它验证 XRT、ZOCL、pyxrt、PYNQ、Device Tree、`EmbeddedDevice` 和 `allocate()`，不负责学生 Artifact、Central Scheduler、Session 或业务 Worker API。学生 Artifact 部署发生在 Runtime Ready 之后。

### 21.1 部署 PC 端仓库布局

```text
kv260/
├── prepare_kv260_image.sh
├── runtime_init_kv260.sh
├── deploy_kv260.sh
├── runtime/
│   ├── ensure_sdk_ppa.sh
│   ├── install_runtime.sh
│   ├── install_xrt.sh
│   ├── install_zocl.sh
│   ├── install_pyxrt.sh
│   ├── install_pynq.sh
│   └── pynq_runtime/
│       ├── pynq.dts
│       ├── insert_dtbo.py
│       ├── clear_pl_state.py
│       └── validate_runtime.py
├── scripts/
│   ├── check_xrt.sh
│   ├── check_zocl.sh
│   ├── check_fpga.sh
│   └── kv260_check.sh
├── server/
└── logs/
    └── kv260N.log
```

### 21.2 KV260 端文件系统布局

```text
KV260 filesystem
│
├── /tmp/kv260-runtime/
│   ├── runtime/
│   └── scripts/
│
├── /opt/kv260-pynq/
│   ├── bin/
│   ├── lib/python3.12/site-packages/
│   └── share/kv260-runtime/
│       ├── pynq.dts
│       ├── pynq.dtbo
│       ├── insert_dtbo.py
│       ├── clear_pl_state.py
│       └── validate_runtime.py
│
├── /opt/fpga/
│   ├── design.bit
│   └── design.hwh
│
├── /usr/local/lib/python3.12/dist-packages/
│   └── pyxrt.cpython-312-aarch64-linux-gnu.so
│
├── /var/cache/kv260-runtime/
│   └── xrt-source/
│
├── /etc/
│   ├── xocl.txt
│   ├── profile.d/
│   │   └── kv260-pynq.sh
│   └── systemd/system/
│       ├── kv260-pynq-dt.service
│       └── kv260-pynq-clear-pl-state.service
│
└── /lib/modules/<kernel>/updates/dkms/
    └── zocl.ko*
```

`/tmp/kv260-runtime` 是 PC launcher 通过 SSH 上传的临时 staging，不是最终安装目录。主要持久化 Runtime 位于 `/opt/kv260-pynq`；系统 pyxrt 位于 `/usr/local/lib/python3.12/dist-packages`；systemd 配置位于 `/etc/systemd/system`；ZOCL 由 DKMS 和 `/lib/modules` 管理；应用 FPGA 文件默认位于 `/opt/fpga`。

| 路径 | 用途 | 分类 |
| --- | --- | --- |
| `/tmp/kv260-runtime` | SSH 上传 Runtime 与检查脚本 | 临时 staging |
| `/opt/kv260-pynq` | Python venv 和 Minimal PYNQ | 持久化 Runtime |
| `/opt/kv260-pynq/share/kv260-runtime` | DTS/DTBO、安装辅助和验证脚本 | 持久化 Runtime |
| `/usr/local/lib/python3.12/dist-packages/pyxrt...so` | 与 XRT Debian 版本匹配的 pyxrt | 持久化系统 Python 扩展 |
| `/var/cache/kv260-runtime/xrt-source` | pyxrt 源码与构建缓存 | 可重建缓存 |
| `/etc/profile.d/kv260-pynq.sh` | Shell Runtime 环境 | 系统配置 |
| `/etc/xocl.txt` | KV260 XRT/PYNQ 板卡配置 | 系统配置 |
| `/etc/systemd/system/kv260-pynq-dt.service` | 启动时加载 PYNQ DT overlay | systemd 配置 |
| `/etc/systemd/system/kv260-pynq-clear-pl-state.service` | 启动时清理 PYNQ PL state | systemd 配置 |
| `/opt/fpga` | 默认应用 bit/hwh | 应用文件 |
| `/lib/modules/<kernel>/updates/dkms/zocl.ko*` | 当前 kernel 的 ZOCL module | DKMS/package 管理 |

## 22. Image Factory

Image Factory 将 Ubuntu 24.04 镜像写入整盘 SD 卡，并按 Board ID 配置 hostname、静态 IP、gateway、DNS、SSH 与首次启动网络。它不安装 PYNQ Runtime；Runtime Factory 在板卡启动后独立执行。

具体命令见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

## 23. Central Server 与 Worker API

### 23.1 Central 对学生 / Client

```text
POST   /fpga/artifacts
GET    /fpga/artifacts
GET    /fpga/artifacts/{artifact_id}

POST   /sessions
GET    /sessions/{session_id}
POST   /sessions/{session_id}/predict
DELETE /sessions/{session_id}

GET    /workers
GET    /health
```

完整使用方式见 [KV260_Server_Usage_Guide.md](KV260_Server_Usage_Guide.md)。

### 23.2 Central 对 Worker

```text
GET  /health
GET  /status
POST /internal/deploy
POST /predict
POST /internal/release
```

- `/internal/deploy`：Session 初始化时下发一次 `design.bit`、`design.hwh`、`session_id`、`artifact_id` 与哈希，并等待 Overlay Ready。
- `/predict`：Session 建立后多次调用，必须核对 `session_id` ownership。
- `/internal/release`：结束 Worker 的 Session ownership，使其可回到 `IDLE`。

当前 Mock Worker 实现测试契约；真实 KV260 业务 Worker 尚未实现。

## 24. PYNQ Overlay 与 XRT Accelerator 模型

| 维度 | 本项目采用：PYNQ Overlay | 另一模型：Vitis/XRT Accelerator |
| --- | --- | --- |
| 应用文件 | `design.bit` + `design.hwh` | `xclbin` + 平台 DTBO |
| 配置入口 | PYNQ `Overlay` / FPGA Manager | XRT application loader |
| 控制方式 | MMIO、PYNQ IP driver | XRT kernel API / scheduler |
| 数据路径 | `allocate()` + AXI DMA/MMIO | XRT BO + compute unit |
| 元数据 | HWH | xclbin metadata |
| 本项目角色 | 核心路径 | 非核心应用模型 |

XRT userspace、pyxrt 和 ZOCL 仍是当前 PYNQ `EmbeddedDevice` 与 allocator 的底层能力，但不应把两种应用模型混为一谈。

本项目采用 PYNQ Overlay，是因为学生硬件以 Vivado bit/hwh 交付，控制路径需要 Python 快速解析 HWH、发现自定义 IP、使用 MMIO 和 AXI DMA。多板调度由 Central Server 完成，无需在单板内部建立 Vitis compute scheduler。

## 25. 两层部署生命周期

### 25.1 基础板卡生命周期

```text
SD Card
   ↓
Image Factory
   ↓
首次启动 / cloud-init
   ↓
Runtime Factory
   ↓
Runtime Ready
   ↓
Worker Service
   ↓
Worker IDLE
```

### 25.2 学生 Session 生命周期

```text
Student Upload Artifact
          ↓
Central Artifact Store
          ↓
POST /sessions
          ↓
随机选择 IDLE KV260
          ↓
RESERVED
          ↓
deploy student's bit/hwh once
          ↓
Overlay Ready
          ↓
Session READY
          ↓
predict many times
          ↓
explicit release
          ↓
Worker IDLE
```

基础 Runtime 生命周期只建立通用板卡能力；学生 Session 生命周期才部署具体业务 Artifact。两者不能混为一个安装流程。

## 26. 当前实现与后续工作

### 26.1 已实现 V1

- Central Server 基础 REST API；
- Artifact Store、SHA-256 与 HWH 基础校验；
- SQLite 持久化；
- Worker Registry 和周期健康检查；
- Session / Lease Scheduler；
- 随机 `IDLE` Worker 分配与原子 reservation；
- FIFO Session Queue；
- Artifact 每个 Session 部署一次；
- Session 固定 Worker 路由；
- per-session predict / release 串行化；
- 严重故障时不透明迁移；
- Mock Worker、pytest 与 Smoke Test；
- KV260 Minimal PYNQ Runtime Factory 实机验证。

### 26.2 尚未实现

- 真实 KV260 PYNQ 业务 Worker；
- 学生算法对应的真实 `/internal/deploy` 与 `/predict` 协议；
- 真实 AXI DMA 输入输出和 Accelerator 控制逻辑；
- 身份认证、权限系统和生产 TLS；
- Web UI 与高级监控；
- Redis/Celery、HA Scheduler；
- Active Session 透明迁移。

## 27. 验收条件

### 27.1 基础 Runtime

- Ubuntu 24.04 Noble、`aarch64`、Xilinx kernel；
- FPGA Manager `operating`；
- XRT / xrt-dkms 版本一致；
- ZOCL、pyxrt、PYNQ DT/runtime 正常；
- `PYNQ ON_TARGET=True`；
- `Device=EmbeddedDevice`；
- `allocate()` 完成功能测试；
- 重启后服务与 Runtime 仍正常。

### 27.2 Central Server V1

- Artifact 上传、校验和持久化成功；
- 仅从 `IDLE` Worker 随机分配；
- 并发 Session 不会重复占用同一 Worker；
- Artifact 每个 Session 只部署一次；
- 多次 predict 固定路由到同一 Worker并串行执行；
- release 后 Worker 回到 `IDLE`；
- 无空闲 Worker 时 Session FIFO 排队；
- Worker 严重故障时 Session 明确进入 `FAILED` / `LOST`，不透明迁移；
- pytest 与 Smoke Test 通过。

### 27.3 真实业务 Worker

该项当前仍是 TODO。完成后还需用真实 bit/hwh 验证 Overlay Load、HWH Parse、IP dictionary、AXI DMA discovery、真实 DMA send/receive 和业务结果正确性，不能用 Mock 测试代替。
