# 20 × KV260 PYNQ Worker 集群最终架构

本文是项目的最终架构设计文档。它定义 Central Server、20 台 KV260 Worker、PYNQ Overlay 计算路径、接口、状态和验收边界。具体烧卡及 Runtime 命令见 `KV260完整执行流程.md`；FPGA/PYNQ 基础概念见 `KV260_PYNQ_Architecture_Notes.md`。

## 1. 项目目标

系统由一台 Central Server 和最多 20 台 KV260 组成。Central Server 负责调度、队列和对外 API；每台 KV260 只运行一个 PYNQ Worker，通过匹配的 `design.bit`/`design.hwh` 驱动本板 FPGA。

初始目标是：

- 单板 `concurrency = 1`；
- 20 块板最多提供约 20 个互相独立的 FPGA Job 并行度；
- Worker 进程启动时加载一次 Overlay，不在每次请求中重配置 FPGA；
- 核心应用模型是 PYNQ Overlay + MMIO/AXI DMA，不是 Vitis xclbin kernel scheduler；
- Runtime 安装不包含 Notebook、Demo 或生产调度平台。

## 2. 系统总体架构

```text
                 ┌────────────────────┐
                 │   Central Server   │
                 │ Scheduler / API    │
                 │ Job Queue          │
                 └─────────┬──────────┘
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ▼              ▼              ▼
         kv2601          kv2602       ... kv26020
      192.168.31.82   192.168.31.83   192.168.31.101
            │              │              │
       PYNQ Worker     PYNQ Worker     PYNQ Worker
            │              │              │
      Overlay(bit)    Overlay(bit)    Overlay(bit)
            │              │              │
         AXI DMA          AXI DMA        AXI DMA
            │              │              │
           FPGA            FPGA           FPGA
            │              │              │
         result           result         result
            └──────────────┼──────────────┘
                           ▼
                    Central Server
```

调度发生在 Central Server，不发生在单块 FPGA 板内。单板不知道其他板的状态，也不持有集群 Job Queue。

## 3. Central Scheduler 职责

Central Scheduler 负责：

- 接收外部 Job；
- 维护 Worker Registry 和健康状态；
- 原子选择并占用一个 `idle` Worker；
- 把请求转发到该 Worker 的 `/predict`；
- 处理超时、失败、重试和重新入队；
- 在所有 Worker 都 `busy` 时保留 Job Queue；
- 聚合结果并返回请求者。

初期只需单一 Scheduler 实例、内存或轻量持久化队列以及明确的锁。当前不引入 Kubernetes 或复杂分布式一致性系统。

## 4. KV260 Worker 职责

每台 KV260 是一个独立 Worker Node，内部路径为：

```text
Ubuntu 24.04 / Xilinx kernel
              ↓
Minimal Kria-PYNQ Runtime
              ↓
PYNQ Worker Service
              ↓
Overlay("/opt/fpga/design.bit")
              ↓
/opt/fpga/design.hwh
              ↓
AXI DMA / MMIO / custom IP
              ↓
FPGA Accelerator
              ↓
result
```

Worker 只负责本板 Overlay 生命周期、请求校验、一次 FPGA 计算和结果返回。它不负责集群调度、其他 Worker 状态、跨板负载均衡或全局 Job Queue。

## 5. Worker 内部 FPGA 计算路径

```text
HTTP request
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

控制路径和数据路径必须分开：MMIO 适合少量寄存器；DMA 适合图像、tensor 或其他批量数据。Worker 应在 `finally` 路径释放 buffer，并在超时后把硬件恢复到可判断的状态。

## 6. `design.bit` / `design.hwh`

- `design.bit` 配置 PL，决定 FPGA 中真正存在的硬件。
- `design.hwh` 描述 IP 名称、地址、寄存器、AXI 接口、中断和连接，供 PYNQ 解析。

两者必须来自同一次 Vivado 构建、使用同一 basename，并作为不可拆分的发布单元。只更新其中一个属于部署错误。Runtime Factory 默认在 `/opt/fpga/design.bit` 和 `/opt/fpga/design.hwh` 查找它们。

## 7. Overlay 生命周期

正确生命周期是：

```text
Worker service 启动
        ↓
加载 Overlay（一次）
        ↓
解析 HWH，绑定 DMA/IP/MMIO
        ↓
READY / IDLE
        ↓
请求 → FPGA Job → 返回结果
        ↓
重新 IDLE（不重新加载 Overlay）
```

只有 Worker 重启、Overlay 版本变更或明确的硬件恢复流程才重新配置 FPGA。每个请求调用 `Overlay(..., download=True)` 会增加延迟、破坏并发状态并放大失败面，因此禁止作为默认请求路径。

## 8. AXI MMIO 与 AXI DMA

MMIO 负责控制寄存器，例如尺寸、模式、启动位和完成状态；AXI DMA 负责 DDR 与 AXI Stream Accelerator 之间的大块数据移动。Worker 必须根据实际 HWH/IP 驱动名称取得 DMA 对象，不能把示例地址或 IP 名硬编码成通用事实。

DMA 完成条件通常同时包括发送、接收和 Accelerator 完成状态。具体等待顺序由硬件协议决定，必须在 Worker 实现阶段与设计联调。

## 9. Buffer、CMA 与 `allocate`

KV260 的 DMA buffer 来自受限的连续内存资源。Runtime 默认要求 `CmaTotal >= 256 MiB`，低于 512 MiB 给出容量警告；实际图像/tensor 尺寸可能需要更高阈值。当前实测约 800 MiB 满足基础要求。

PYNQ 3.1.2 的 `allocate()` 使用 PYNQ `Device` 和 XRT BO allocator。官方 sdbuild 中的 `libsds/libcma.so` 仍为旧 SDS API 保留，但不在 PYNQ 3.1.2 `pynq.allocate` 的调用路径中。本项目不把旧预编译库当作 Noble allocator；验收直接创建 buffer、写入、同步、读取并释放。

当实际 Overlay 已加载时，HWH 生成的 PS DDR memory topology 成为默认 allocation target。没有设计文件时，Runtime Factory 用显式 PS DDR target 验证同一个 XRT BO 后端，并明确将 Overlay Hardware Test 标为未运行。

## 10. Job Queue

初始流程为：

```text
收到 Job
  ↓
寻找 idle Worker
  ↓
原子占用并标记 busy
  ↓
调用 Worker /predict
  ├── 成功：Worker → idle，返回结果
  └── 失败：Worker → error/offline，Job 按策略重试或重新入队
```

所有 Worker 都 `busy` 时，Job 留在队列中等待，不应并发挤入同一块板。

## 11. Worker Registry

Registry 至少保存：

| 字段 | 含义 |
| --- | --- |
| `board` | `kv2601` … `kv26020` |
| `ip` | 固定管理地址 |
| `status` | `idle` / `busy` / `offline` / `error` |
| `fpga_ready` | Overlay、DMA/IP 初始化是否完成 |
| `last_seen` | 最近一次健康检查时间 |
| `worker_version` | Worker 软件版本 |
| `overlay` | Overlay 名称/版本或校验值 |

示例状态：

```text
kv2601  192.168.31.82  idle
kv2602  192.168.31.83  busy
kv2603  192.168.31.84  idle
kv2604  192.168.31.85  offline
```

## 12. Worker 状态机

```text
STARTING ──成功──> IDLE ──原子占用──> BUSY ──成功──> IDLE
    │                 │                   │
    └──初始化失败──> ERROR <──计算失败────┘

任意在线状态 ──健康检查超时──> OFFLINE
OFFLINE/ERROR ──人工或自动恢复并复检──> IDLE
```

Central Scheduler 对 `busy` 的写入必须是原子的。Worker 本地也必须使用单进程队列、互斥锁或等效机制拒绝第二个同时访问同一 DMA/IP 的请求。

## 13. Health Check

Scheduler 定期调用 `/health`，并使用超时与连续失败次数避免一次网络抖动就永久下线。`/health` 只说明服务进程可响应；`/status` 才报告 `fpga_ready` 和当前忙闲状态。

Worker 启动时的深度自检应覆盖 Overlay/HWH、DMA/IP 绑定和必要 buffer 分配。不要在每次轻量健康检查中重载 FPGA。

## 14. 初始调度策略

第一版可采用 round-robin among idle workers：在所有 `idle` Worker 中轮询选择，原子改为 `busy` 后发送请求。后续可增加最近延迟、温度或失败率权重，但不能破坏单板只占用一次的约束。

示例：

```text
Job #1001 → kv2603
Job #1002 → kv2601
Job #1003 → kv26020
Job #1004 → 无 idle Worker，进入 Job Queue
```

## 15. Failure / Retry

- 连接失败：Worker 标记 `offline`，Job 可转移到另一块板。
- Worker 返回可恢复错误：标记 `error`，按 Job 幂等性决定重试。
- FPGA/DMA 超时：不能立即把 Worker 重新标为 `idle`；先执行硬件恢复或重启 Worker。
- 请求者取消：只有确认 DMA/Accelerator 已停止或结果可安全丢弃后才能释放 Worker。
- Scheduler 重启：生产实现应持久化或重建 `busy` Job，避免重复计算。

重试必须设置次数、总时限和幂等键，防止故障循环。

## 16. 为什么单板 `concurrency = 1`

同一 Worker 的请求共享一套 AXI DMA、Accelerator 寄存器和 buffer 生命周期。并行请求可能交叉覆盖寄存器、交换 buffer 或错误消费中断。第一版用串行执行换取确定性；只有硬件明确提供多通道、队列或多个独立 Accelerator 实例后，才能提升单板并发。

## 17. 20 板并行模型

系统并行度来自板间隔离：每台 KV260 同时处理一个 Job，20 台健康 Worker 可同时处理约 20 个 Job。单板失败只减少容量，不应破坏其他 Worker。Scheduler 负责把负载分散到仍为 `idle` 的节点。

## 18. 网络与地址规则

```text
Board ID: 1 … 20
hostname: kv260N
IPv4:    192.168.31.(81 + N)/24
gateway: 192.168.31.1
DNS:     223.5.5.5, 192.168.31.1
Worker API port（规划）: 8080
```

| Board | Hostname | IPv4 |
| ---: | --- | --- |
| 1 | `kv2601` | `192.168.31.82` |
| 2 | `kv2602` | `192.168.31.83` |
| 3 | `kv2603` | `192.168.31.84` |
| 20 | `kv26020` | `192.168.31.101` |

Board ID 是唯一配置输入；同一网段不能重复使用 ID。

## 19. Runtime Factory

Runtime Factory 在已启动的 KV260 上建立可运行的 FPGA Python 基础：

1. 验证 Noble、aarch64、Xilinx kernel、FPGA Manager 和 CMA。
2. 从 `ppa:ubuntu-xilinx/sdk` 使用明确 Debian 版本的 XRT。
3. 安装与 XRT 完全同版本的 `xrt-dkms`，验证当前 kernel 的 `zocl.ko` 和已加载状态。
4. 如存在更新 Xilinx kernel，返回 `REBOOT_REQUIRED`；PC 端重启并继续。
5. 建立 `/opt/kv260-pynq`，安装固定的 PYNQ/pynqmetadata/pynqutils。
6. 编译并幂等加载 Minimal PYNQ DT，安装 PL state 清理 service。
7. 真实执行 `allocate`/同步/释放；存在设计文件时再加载 Overlay 和解析 HWH。

Runtime Factory 不实现业务 Worker，也不把未部署的 Worker 报为 OK。

## 20. Image Factory

`prepare_kv260_image.sh` 只负责：镜像写盘、rootfs 扩容、hostname、静态网络、用户、SSH 和 cloud-init。它不在离线 ARM rootfs 中安装 PYNQ/XRT，也不写入应用 Overlay。这样所有需要在目标架构和当前 kernel 上验证的 Runtime 工作都留在启动后的 KV260 上完成。

## 21. Central Server 与 Worker API

API 是架构契约，当前尚未实现。建议最小接口如下。

`GET /health`

```json
{"ok": true}
```

`GET /status`

```json
{
  "board": "kv2602",
  "status": "idle",
  "fpga_ready": true
}
```

`GET /info`

```json
{
  "hostname": "kv2602",
  "ip": "192.168.31.83",
  "overlay": "design",
  "worker_version": "1.0"
}
```

`POST /predict` 接收一个 Job，串行执行 DMA/FPGA 并返回结果。数据 envelope 应版本化，但不要过早写死 payload：结构化小输入可用 JSON；图像或大二进制通常更适合 `multipart/form-data` 或 raw binary，避免 base64 的尺寸和复制开销。

服务端还应约定 request/job ID、超时、错误码、最大 payload 和认证方式。

## 22. PYNQ Overlay 与 XRT Accelerator 模型

| 项目 | 方案 A：PYNQ Overlay（本项目） | 方案 B：Vitis/XRT Accelerator |
| --- | --- | --- |
| 设计产物 | `.bit` + `.hwh` | `.xclbin` + platform/DTBO |
| FPGA 配置 | FPGA Manager / PYNQ Overlay | XRT/xclbin flow |
| 控制 | MMIO、PYNQ IP driver | XRT kernel API/scheduler |
| 数据 | PYNQ `allocate` + AXI DMA | XRT BO + compute unit |
| 调度目标 | Central Server 调度整块板 | XRT 调度 device/compute unit |
| 本项目地位 | 核心应用模型 | 非核心、仅保留底层能力 |

两个层级也必须分开：

- ZOCL Driver：`xrt-dkms` 已安装、当前 kernel 有 `zocl.ko`、module 已加载。
- XRT accelerator platform：live DT 有 `xlnx,zocl`、DRM render node、`xrt-smi` 能枚举 device。

基础 firmware 没有 `xlnx,zocl` 时，`renderD128` 缺失或 `xrt-smi` 报 0 device 不能反推 DKMS 安装失败。Runtime 的 ZOCL 阶段只验证 driver。Minimal PYNQ DT 随后提供 allocator 所需的 platform node；最终不硬编码 render node 名称，而用真实 `allocate()` 功能测试验收。

## 23. 为什么采用 PYNQ Overlay

当前硬件交付物和应用控制需求是 `design.bit` + `design.hwh` + AXI DMA/MMIO。PYNQ 能直接解析 HWH、暴露 IP 字典并为 Python Worker 提供合适抽象。集群调度的单位又是整块板，因此没有必要把 Central Scheduler 问题转换成单板 XRT compute-unit scheduler 问题。

XRT/ZOCL 仍作为 PYNQ 3.1.2 allocator 的底层能力保留，但不改变“服务器调度板、Worker 控制 Overlay”的系统边界。

## 24. 部署生命周期

```text
同一 Noble base image
        ↓
Image Factory 按 Board ID 制卡
        ↓
首次启动 / cloud-init / 网络验证
        ↓
Runtime Factory Stage 1（XRT/ZOCL）
        ├── 需要新 kernel：自动 reboot/resume
        └── 不需要重启：继续
        ↓
Runtime Factory Stage 2（Minimal PYNQ/DT/allocator）
        ↓
部署匹配的 /opt/fpga/design.bit + design.hwh
        ↓
部署并启动 PYNQ Worker（TODO）
        ↓
Central Scheduler 注册并健康检查（TODO）
```

## 25. 最终验收条件与 TODO

Runtime Factory 的硬性条件：

- Ubuntu 24.04 Noble、`aarch64`、Xilinx kernel；
- FPGA Manager `fpga0` 为 `operating`；
- CMA 达到配置阈值；
- XRT 与 `xrt-dkms` Debian version 完全一致；
- 当前 kernel 存在并加载 ZOCL；
- PYNQ 3.1.2、pynqmetadata、pynqutils 安装完成；
- `Overlay`/`MMIO` import 成功；
- Minimal PYNQ DT/runtime active；
- `allocate()` 实际分配、同步、读回和释放成功；
- 有匹配 design 文件时，Overlay load/HWH parse 成功；没有时明确输出 `NOT RUN`。

`renderD128` 的固定路径、`xrt-smi` device count 和 XMUtil application 枚举是诊断项，不是独立硬失败条件。

尚未实现，必须保持 TODO：

- `worker/`：实际 `/health`、`/status`、`/info`、`/predict` 服务和硬件协议；
- `scheduler/`：Registry、Job Queue、原子占用、retry 和对外 API；
- 真实 `design.bit`/`design.hwh` 的项目级发布、DMA loopback/算法验收；
- 生产认证、TLS、指标、日志和持久化策略。

## 官方机制参考与 Noble 取舍

设计参考 [Xilinx/Kria-PYNQ](https://github.com/Xilinx/Kria-PYNQ) 及其 [install.sh](https://github.com/Xilinx/Kria-PYNQ/blob/main/install.sh)，但不直接执行该脚本。官方脚本限定 Ubuntu 22.04、Python 3.10、PYNQ 3.0.1 和 Jammy 源；本项目在 Noble 上使用系统 Python 3.12、PYNQ 3.1.2 和 SDK PPA 的同版本 XRT/DKMS。

提取并适配的机制包括 venv、BOARD/XRT 环境、pynqmetadata/pynqutils、`/etc/xocl.txt` 兼容配置、PYNQ DT 和 PL state 清理。明确排除 Jupyter/JupyterLab、Notebook、HelloWorld、Base Overlay、Composable Pipeline、DPU-PYNQ、peripherals/examples、OpenCV demo 和 MicroBlaze 工具链。
