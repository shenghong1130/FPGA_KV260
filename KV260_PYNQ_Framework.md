# 20 × KV260 PYNQ 共享计算平台总体架构

## 1. 文档定位与核心设计原则

本文定义 20 × KV260 共享 PYNQ FPGA 计算平台的系统架构，重点描述 Central Server、Student / Client、Artifact、Session / Lease、Scheduler、KV260 Worker 及其状态和故障边界。

相关文档：

- PYNQ / FPGA 技术理论：[KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)
- SD 卡制作和 Runtime 基础部署：[KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)
- Central Server 安装、启动、API 使用和测试：[KV260_Server_Usage_Guide.md](KV260_Server_Usage_Guide.md)

平台遵循以下核心原则：

```text
Session / Lease 是调度单位，不是单次 predict

一个 Session 独占一块 KV260

Artifact 在 Session 初始化时部署一次

Session 建立后：session_id → worker_id 固定

Session 内：predict many times

predict 不重新调度 Worker
predict 不重新上传 bit/hwh
predict 不重新加载 Overlay

predict 完成：BUSY → READY
不是：        BUSY → IDLE

READY != IDLE

READY = 已被某个 Session 占用，当前可接收该 Session 的下一次 predict
IDLE  = 没有 Session ownership，可以分配给新 Session

只有 Release 后：Worker → IDLE

单块 KV260：concurrency = 1

Active Session 不透明迁移到其他 Worker
```

还必须区分：

```text
Artifact != Session
Session  != predict
```

Artifact 是可重复引用的 FPGA 设计资产；Session 是临时资源租约；predict 是 Session 内的一次计算请求。

## 2. 系统整体框架

```text
                     Student / Client
                           │
                    upload Artifact
                           │
                           ▼
                  ┌──────────────────┐
                  │  Central Server  │
                  │                  │
                  │ Artifact Store   │
                  │ Session Manager  │
                  │ Scheduler        │
                  │ Worker Registry  │
                  └────────┬─────────┘
                           │
                    allocate Session
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       kv2601           kv2602          ... kv26020
          │                │                │
      PYNQ Worker      PYNQ Worker      PYNQ Worker
          │
       Overlay
          │
      DMA / MMIO
          │
         FPGA
          │
        Result
```

Central Server 管理整个集群。Student / Client 只通过 Central Server 使用 FPGA，不直接选择某块 KV260。Scheduler 位于 Central Server；Worker 只维护本板状态，不知道其他 KV260 的状态，也不维护全局 Session Queue。

Central Server 负责控制面，真实 Overlay 加载和 FPGA 计算发生在 KV260 Worker 上。

## 3. 平台角色与职责边界

| 角色 | 核心职责 |
| --- | --- |
| Student / Client | 上传 Artifact、创建 Session、在 Session 内连续 predict、主动 Release |
| Central Server | 管理 Artifact、Session Queue、Scheduler、Worker Registry、固定路由和持久化状态 |
| KV260 Worker | 接收 Artifact、管理 Session ownership、加载 Overlay、执行本板 FPGA 计算并返回结果 |

Student / Client 不负责选择具体 `kv260N`、修改 Worker 状态、管理 Session Queue、调用 Overlay、直接操作 DMA / MMIO、管理其他学生或维护 Worker Registry。

Central Server 不负责 FPGA 实际计算、Overlay 内部算法或真实 DMA 数据搬运。

KV260 Worker 不负责全局 Scheduler、全局 Queue、其他 Worker 状态、跨板选择或集群负载均衡。

## 4. Central Server 总体框架

Central Server V1 位于仓库 `server/`，采用 FastAPI + SQLAlchemy + SQLite + httpx + asyncio 构建。

```text
                  Central Server V1
                         │
       ┌─────────────────┼────────────────┐
       │                 │                │
       ▼                 ▼                ▼
 Artifact Store     Session Manager   Worker Registry
       │                 │                │
       ▼                 ▼                │
 local files          Scheduler           │
       │                 │                │
       └──────► SQLite ◄──┴────────────────┘
                         │
                    WorkerClient
                         │ HTTP
       ┌─────────────────┼──────────────────┐
       ▼                 ▼                  ▼
    kv2601            kv2602             kv26020
    :8080             :8080              :8080
```

当前源码结构：

```text
server/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── db_models.py
│   ├── schemas.py
│   ├── artifact_store.py
│   ├── scheduler.py
│   ├── session_manager.py
│   ├── worker_registry.py
│   ├── worker_client.py
│   └── api/
│       ├── __init__.py
│       ├── artifacts.py
│       ├── sessions.py
│       ├── workers.py
│       └── health.py
├── config/
│   ├── workers.json
│   └── workers.mock.json
├── data/
├── testbed/
│   ├── __init__.py
│   ├── mock_worker.py
│   ├── run_mock_cluster.py
│   └── smoke_test.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_artifacts.py
│   ├── test_sessions.py
│   ├── test_scheduler.py
│   ├── test_release.py
│   └── test_concurrency.py
├── .gitignore
├── requirements.txt
└── requirements-dev.txt
```

模块关系概览：

| 模块 | 作用 |
| --- | --- |
| `main.py` | 创建应用，初始化服务并管理 startup/shutdown |
| `config.py` | 读取环境变量、默认路径和超时配置 |
| `database.py` / `db_models.py` | 创建数据库连接并定义 Artifact、Session、Worker 模型 |
| `schemas.py` | 定义 Pydantic 请求和响应模型 |
| `artifact_store.py` | 校验并保存 bit/hwh，持久化 Artifact metadata |
| `session_manager.py` | 编排 create、deploy、predict、release 与 Queue allocator |
| `scheduler.py` | 原子选择 `IDLE` Worker，并查询最早的 `QUEUED` Session |
| `worker_registry.py` | 同步配置、健康检查、状态维护和启动恢复 |
| `worker_client.py` | 通过 httpx 调用 Worker HTTP API |
| `api/` | 暴露 Artifact、Session、Worker 和 health 路由 |
| `testbed/` | 提供 Mock Worker、Mock Cluster 和 Smoke Test |
| `tests/` | 提供 pytest 自动化测试 |

## 5. Central Server 职责

### 5.1 Artifact Store

Artifact Store 负责：

- 接收学生上传的 `design.bit` 和 `design.hwh`；
- 检查扩展名、空文件和上传大小；
- 分块写入并计算 SHA-256；
- 对 HWH 执行 XML 基础解析；
- 在临时 staging 目录完成写入；
- 生成 `manifest.json` 并执行 atomic move；
- 在 SQLite 中保存 Artifact metadata 和文件路径。

实际 bit/hwh 和 manifest 存在本地文件系统，SQLite 不保存 bitstream BLOB。

### 5.2 Session Manager

Session Manager 负责：

- 创建 Session；
- 检查 Artifact 是否存在、是否 `READY`，以及 `student_id` ownership；
- 协调 Scheduler reservation；
- 发起 Artifact deploy；
- 固定路由 predict；
- 处理 release；
- 维护 Session 生命周期；
- 使用 per-session lock 串行化 predict 与 release；
- 在 Worker 释放后唤醒 Queue allocator。

### 5.3 Scheduler

Scheduler 只负责从真正可分配的 Worker 中选择资源。当前候选条件是：

```text
Worker.state == IDLE
并且
Worker.session_id == NULL
```

然后使用 `random.choice(idle_workers)` 随机选择一块。allocation lock 将以下过程保护为同一临界区：

```text
查询 IDLE Worker
        +
随机选择 Worker
        +
Worker：IDLE → RESERVED
        +
Session 绑定 worker_id
```

因此两个并发 Session 不会同时占用同一块 KV260。Scheduler 只在 Session 创建或 Queue 分配阶段运行，不参与每一次 predict。

### 5.4 Worker Registry

Worker Registry 负责配置同步、Worker 列表、地址、状态、Session ownership、Artifact ownership、健康检查和 Central 启动恢复。主要持久化字段包括 `board`、`base_url`、`state`、`session_id`、`current_artifact_id`、`fpga_ready`、`last_seen` 和 `last_error`。

### 5.5 WorkerClient

WorkerClient 使用 httpx 执行 Central Server 到 KV260 Worker 的异步 HTTP 调用：

```text
GET  /health
GET  /status
POST /internal/deploy
POST /predict
POST /internal/release
```

Central Server 不 import PYNQ、不调用 Overlay，也不操作 FPGA。

## 6. Student / Client 职责

学生正常只做四件事：

```text
1. 上传自己的 FPGA Artifact
2. 创建 Session
3. 在 Session 内连续发送 predict
4. 不再使用时主动 Release
```

```text
Student
   │
   ├── POST /fpga/artifacts
   │
   ├── POST /sessions
   │
   ├── POST /sessions/{session_id}/predict
   ├── POST /sessions/{session_id}/predict
   ├── POST /sessions/{session_id}/predict
   │
   └── DELETE /sessions/{session_id}
```

学生不需要知道真实 KV260 IP，也不需要自行选择 `kv2607` 或维持永久 HTTP 连接。一次 HTTP 请求结束不等于 Session 结束；真正的绑定关系是：

```text
session_id → worker_id
```

只要未 Release，Session 就继续占用该 Worker。

## 7. Artifact 生命周期

每个学生可以拥有自己的 FPGA Artifact。V1 的一个 Artifact 至少包含：

```text
design.bit
design.hwh
```

系统生命周期：

```text
Student Upload
      ↓
Central validation
      ↓
Artifact Store
      ↓
Artifact READY
      ↓
未来 Session 引用
```

Artifact 长期保存在 Central Server。Session release 不删除 Artifact，Artifact 也不永久绑定某一台 KV260。同一 Artifact 可以被未来多个 Session 引用。

每个 Session 初始化时，Artifact 只向被选中的 Worker 部署一次；Session 内后续 predict 不再重复部署。

## 8. Session / Lease 完整流程

Session 是对一块 KV260 的资源租约，不是一次 predict，也不是一次 HTTP Request。

```text
Student already has Artifact
          ↓
POST /sessions
          ↓
创建 Session
          ↓
Session → QUEUED
          ↓
存在 IDLE Worker？
      ┌───┴───┐
      │       │
     YES      NO
      │       │
      │       └── 保持 QUEUED
      │
      ▼
random IDLE Worker
      ↓
atomic reservation
      ↓
Worker → RESERVED
Session → RESERVED
      ↓
session_id ↔ worker_id
固定绑定
      ↓
DEPLOYING
      ↓
Central POST /internal/deploy
      ↓
发送 design.bit / design.hwh
      ↓
Worker 加载 Overlay
      ↓
硬件初始化成功
      ↓
Session READY
Worker READY
      ↓
POST /sessions/{id}/predict
      ↓
BUSY → FPGA compute → result → READY
      ↓
predict → BUSY → READY
      ↓
predict → BUSY → READY
      ↓
...
      ↓
DELETE /sessions/{id}
      ↓
RELEASING
      ↓
Worker /internal/release
      ↓
Session CLOSED
Worker IDLE
```

`session_id ↔ worker_id` 的固定绑定从 reservation 开始，在正常 release 前不会改变。

## 9. Session Queue 与 Worker 分配

每个新 Session 先以 `QUEUED` 状态写入 SQLite。如果存在候选 Worker，Scheduler 会立即 reserve；如果所有 Worker 都被占用，Session 保持 `QUEUED`。

当前 Queue 是 FIFO，按照 `created_at` 和 Session ID 选择最早等待项。正常 release 后：

```text
Worker → IDLE
       ↓
allocator_event
       ↓
oldest QUEUED Session
       ↓
reserve
       ↓
deploy
       ↓
READY
```

全局 Queue 排的是 Session，不是 predict。Session `READY` 后的 predict 不重新进入 Scheduler Queue。

## 10. Session 固定路由与并发模型

Session 一旦完成分配：

```text
sess_A → kv2607

predict #1 → kv2607
predict #2 → kv2607
predict #3 → kv2607
predict #4 → kv2607
```

不会在每次请求时重新随机选择其他 Worker。predict 路径直接使用：

```text
session_id
   ↓
worker_id
   ↓
WorkerClient
```

同一 Session 的 `concurrency = 1`。Central Server 的 per-session lock 串行化 predict 和 release：

```text
predict A 正在执行
      ↓
release 到达并等待
      ↓
predict A 完成
      ↓
release 执行
```

未来真实 Worker 本地也必须提供硬件访问串行保护，防止绕过 Central 的请求并发操作同一套 FPGA 资源。

## 11. KV260 Worker 完整流程

```text
                    KV260 Worker Service
                            │
             ┌──────────────┼──────────────┐
             │              │              │
             ▼              ▼              ▼
         /health         /status      /internal/deploy
                                           │
                                           ▼
                                   接收 Session Artifact
                                           │
                                           ▼
                              校验 session / artifact / hash
                                           │
                                           ▼
                                     load Overlay
                                           │
                                           ▼
                                    初始化 FPGA 资源
                                           │
                                           ▼
                                         READY
                                           │
                                           ▼
                                       /predict
                                           │
                                           ▼
                                validate session ownership
                                           │
                                           ▼
                                     prepare input
                                           │
                                           ▼
                                      DMA / MMIO
                                           │
                                           ▼
                                    FPGA Accelerator
                                           │
                                           ▼
                                      collect result
                                           │
                                           ▼
                                     HTTP response
                                           │
                                           ▼
                                         READY
                                           │
                                  可继续下一次 predict
                                           │
                                           ▼
                                  /internal/release
                                           │
                                           ▼
                                  remove ownership
                                           │
                                           ▼
                                          IDLE
```

Worker 负责管理本板 Session ownership、接收 Artifact、检查部署身份和哈希、加载 Overlay、初始化硬件、验证 predict ownership、执行 FPGA 计算、返回结果、串行访问硬件并处理 release。

Worker 不负责全局 Scheduler、Session Queue、其他 Worker 查询、跨板选择或全局负载均衡。

本节只标明 Overlay、DMA、MMIO 和 `allocate()` 在 Worker 流程中的位置。具体 PYNQ、Overlay、MMIO、DMA、Buffer 和 CMA 原理见 [KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)。

## 12. Overlay 生命周期

```text
Worker Service Running
        ↓
Worker IDLE
        ↓
Session RESERVED
        ↓
DEPLOYING
        ↓
receive Artifact
        ↓
load Overlay
        ↓
hardware initialization
        ↓
READY
        ↓
predict → BUSY → READY
        ↓
predict → BUSY → READY
        ↓
...
        ↓
Session Release
        ↓
Worker IDLE
```

Overlay 每个 Session 只部署一次。正常路径是：

```text
deploy once
predict many times
```

禁止在 predict 中重新传输 bit/hwh 或重新调用 Overlay。Release 只解除逻辑 ownership，不要求主动擦空 FPGA；Worker 可以物理保留最后一个 Overlay。下一名学生的 Session 获得该 Worker 后，新的 `/internal/deploy` 会加载新 Artifact 并覆盖旧 Overlay。

## 13. Session 状态机

真实 Session 状态为：

```text
QUEUED
RESERVED
DEPLOYING
READY
BUSY
RELEASING
CLOSED
FAILED
LOST
```

正常状态图：

```text
QUEUED
   ↓
RESERVED
   ↓
DEPLOYING
   ↓
READY ⇄ BUSY
  ↓
RELEASING
  ↓
CLOSED
```

异常路径：

```text
DEPLOYING → FAILED

READY / BUSY
     ↓
Worker serious failure
     ↓
LOST
```

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 暂无可分配 Worker，等待 FIFO allocator |
| `RESERVED` | 已原子占用一个 Worker并建立固定绑定 |
| `DEPLOYING` | 正在向 Worker 部署 Artifact |
| `READY` | Session 已独占 Worker，可以发送下一次 predict |
| `BUSY` | 当前正在执行一次 predict |
| `RELEASING` | 正在通知 Worker 解除 ownership |
| `CLOSED` | Session 已结束 |
| `FAILED` | Artifact 部署等 Session 初始化过程失败 |
| `LOST` | 活动 Worker 严重故障或 ownership 不一致 |

单次计算完成是 `BUSY → READY`，不是 `BUSY → IDLE`；只有 Session release 才释放 Worker。

## 14. Worker 状态机

真实 Worker 状态为：

```text
IDLE
RESERVED
DEPLOYING
READY
BUSY
ERROR
OFFLINE
```

正常流程：

```text
IDLE
 ↓
RESERVED
 ↓
DEPLOYING
 ↓
READY ⇄ BUSY
  ↓
release
  ↓
IDLE
```

异常流程：

```text
DEPLOYING / READY / BUSY → ERROR

online
  ↓
health failure threshold
  ↓
OFFLINE
```

`IDLE` 表示没有 Session ownership，可以被 Scheduler 分配；`READY` 表示已经属于某个 Session，只是当前没有执行 predict。因此 `READY != IDLE`，Scheduler 只能选择 `IDLE` Worker。

当前 Worker enum 没有单独的 `RELEASING`；release 过程由 Session 状态、per-session lock 和 allocation lock 表达。成功后 Worker 进入 `IDLE`，失败时保持不可调度状态。

## 15. Worker Registry 与 Health Check

Worker Registry 当前保存的信息包括：

```text
board
base_url
state
session_id
current_artifact_id
fpga_ready
last_seen
last_error
```

真实 Worker 清单来自 `server/config/workers.json`，地址范围为 `kv2601`（`192.168.31.82:8080`）至 `kv26020`（`192.168.31.101:8080`）。完整 Board ID 和地址规则见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

Health Monitor 周期调用：

```text
GET /health
GET /status
```

一次网络失败只增加内存中的连续失败计数，不会立即永久下线。连续失败达到 `HEALTH_FAILURE_THRESHOLD` 后，Worker 进入 `OFFLINE`；如果其当前拥有 Active Session，该 Session 进入 `LOST`。

Worker 返回成功后，Registry 还会比较远端 `session_id` / `artifact_id` 与 SQLite ownership，不能仅凭 `/health` 返回成功就把它视为可分配。

## 16. 故障、Release 与恢复

### 16.1 Artifact deploy 失败

```text
Session → FAILED
Worker  → ERROR
```

失败 Worker 不会被伪装成 `IDLE`。

### 16.2 predict 或 Worker 严重故障

predict 的 Worker HTTP 调用失败时：

```text
Session → LOST
Worker  → ERROR
```

健康检查达到失败阈值时，Worker 进入 `OFFLINE`；若存在 Active Session，该 Session 同样进入 `LOST`。

### 16.3 禁止 Active Session 透明迁移

```text
sess_A → kv2607
kv2607 fail
```

Central 不能自动改成 `sess_A → kv2608`。Overlay、本板状态、DMA 状态和 ownership 已绑定原 Worker。客户端需要放弃旧 Session，并创建新 Session。

### 16.4 正常 Release

```text
Session READY
      ↓
RELEASING
      ↓
Worker /internal/release
      ↓
Session CLOSED
Worker IDLE
```

释放一个仍在 `QUEUED` 的 Session 时，不涉及 Worker，Session 直接进入 `CLOSED`。

### 16.5 Release 失败

如果 `/internal/release` 失败，Session 仍记录为 `CLOSED` 并保存错误信息，但 Worker 进入 `ERROR`，不会假装成为 `IDLE`，因此不能重新调度。

## 17. 数据持久化与 Central 重启恢复

Central Server 使用 SQLite 保存核心 metadata：

```text
SQLite
├── Artifact metadata 与文件路径
├── Worker Registry、ownership 与状态
└── Session metadata、ownership 与状态
```

实际 `design.bit`、`design.hwh` 和 `manifest.json` 保存在 Artifact Store 的本地文件系统，不是 SQLite BLOB。

Central 启动流程：

```text
Central Start
     ↓
初始化 SQLite schema
     ↓
load workers config
     ↓
sync Worker Registry
     ↓
读取持久化 Session / Worker ownership
     ↓
GET /health
     ↓
GET /status
     ↓
比较 Central ownership 与 Worker ownership
```

如果 Active Session 与 Worker 报告的 `session_id` 和 `artifact_id` 一致，恢复流程将双方置为 `READY`，继续使用原绑定。如果不一致，Worker 进入 `ERROR`，对应 Session 进入 `LOST`。如果 Worker 不报告远端 Session且 Central 没有 Active ownership，才可以恢复为 `IDLE`。

Central 重启后绝不能简单把所有 Worker 都设为 `IDLE`，否则可能把仍被某个 Session 占用的 FPGA 再次分配给另一名学生。

## 18. Central / Client / Worker API 边界

Student / Client 到 Central：

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

Central 到 Worker：

```text
GET  /health
GET  /status
POST /internal/deploy
POST /predict
POST /internal/release
```

第一组接口属于学生与平台控制面；第二组接口属于 Central 与 Worker 的内部契约。详细请求字段、启动命令和交互测试方式见 [KV260_Server_Usage_Guide.md](KV260_Server_Usage_Guide.md)。

## 19. 20 板共享资源与并行模型

20 台 KV260 是公共 Worker Pool，不是每个学生永久拥有一块板，也不是每次 predict 随机找一块板。

```text
Student A → Session A → kv2607
Student B → Session B → kv2603
```

在 Session A 存续期间，`kv2607` 只属于 Session A；Session B 可以同时在 `kv2603` 上运行。20 台健康 KV260 最多约支持 20 个同时存在的 FPGA Session。

这不是“最多只能执行 20 次 predict”。每个 Session 内都可以持续：

```text
predict
predict
predict
...
```

当 20 台 Worker 全部被占用时，第 21 个 Session 进入 `QUEUED`，等待已有 Session release。单块 KV260 同时只有一个 Session、一次一个 FPGA operation；不同板卡之间可以并行。

## 20. 当前实现、测试与后续工作

### 20.1 当前已经实现

Central Server V1 已实现：

- FastAPI REST API；
- Artifact Store 与 bit/hwh 文件保存；
- 上传大小限制、SHA-256、HWH XML 基础验证、staging 和 atomic move；
- SQLite persistence；
- Session Manager；
- `random.choice` IDLE Worker selection；
- asyncio allocation lock；
- FIFO Session Queue；
- `session_id → worker_id` 固定路由；
- per-session predict/release lock；
- Worker Registry、Health Check 和 Central restart recovery；
- Mock Worker、pytest 与 Smoke Test；
- 实机验证通过的 KV260 Minimal PYNQ Runtime。

### 20.2 测试边界

pytest 和 Smoke Test 覆盖 Artifact 校验、原子分配、排除 `READY` Worker、FIFO Queue、release、deploy once、固定 Worker 上连续 predict、并发 Session 创建和 per-session 串行化。

```text
Mock Worker != 真实 PYNQ Worker
```

Mock Worker 只验证 Central Server 的接口、调度和状态模型，不执行真实 PYNQ、Overlay、DMA、MMIO 或 FPGA 计算。

### 20.3 两层生命周期

基础设施生命周期：

```text
SD Card
   ↓
Image Factory
   ↓
Ubuntu boot
   ↓
Runtime Factory
   ↓
Runtime Ready
   ↓
Worker Service
   ↓
Worker IDLE
```

学生业务生命周期：

```text
Artifact Upload
      ↓
POST /sessions
      ↓
Worker allocation
      ↓
Artifact deploy
      ↓
Overlay Ready
      ↓
predict many times
      ↓
Release
      ↓
Worker IDLE
```

Image Factory 和 Runtime Factory 建立通用板卡运行环境；Session 才部署具体学生 FPGA 设计。具体 SD 卡和 Runtime 操作见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

### 20.4 当前尚未实现

- 真实 KV260 PYNQ Worker；
- 真实 `/internal/deploy`、Overlay 业务初始化和学生 bit/hwh 运行逻辑；
- 真实 `/predict`、DMA 输入输出和 MMIO / Accelerator 协议；
- 不同学生设计的业务接口标准；
- 身份认证和权限系统；
- 生产 TLS、Web UI 和高级监控；
- Redis / Celery 与 HA Scheduler；
- Active Session 透明迁移。

真实 Worker 是下一阶段。Central Server V1 和基础 Runtime 已经存在，但不能因此宣称真实 FPGA 业务链路已经完成。

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

### 21.3 Central Server V1 目录与模块关系

Central Server V1 位于仓库 `server/` 目录中，采用 FastAPI + SQLAlchemy + SQLite + httpx + asyncio 构建。

它负责：

- 接收并持久化学生 FPGA Artifact；
- 管理 Session / Lease 生命周期；
- 从 `IDLE` Worker 中随机并原子分配 KV260；
- 将 `design.bit` / `design.hwh` 部署到已分配 Worker；
- 在整个 Session 生命周期内保持固定 Worker；
- 转发连续的 `/predict` 请求；
- 在 Session release 后回收 Worker；
- 执行 Worker 健康检查并维护故障状态；
- 提供 Mock Worker、Smoke Test 和 pytest 测试环境。

Central Server 自身不执行 PYNQ Overlay、DMA 或 MMIO。真实 FPGA 计算发生在 KV260 Worker；当前 `testbed/mock_worker.py` 只模拟 Worker 接口和状态，不访问 FPGA。

当前目录结构如下。`data/central.db` 与 `data/smoke*.db` 是运行或测试时生成的 SQLite 文件，不属于应用源码：

```text
server/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── db_models.py
│   ├── schemas.py
│   │
│   ├── artifact_store.py
│   ├── scheduler.py
│   ├── session_manager.py
│   ├── worker_registry.py
│   ├── worker_client.py
│   │
│   └── api/
│       ├── __init__.py
│       ├── artifacts.py
│       ├── sessions.py
│       ├── workers.py
│       └── health.py
│
├── config/
│   ├── workers.json
│   └── workers.mock.json
│
├── data/
│   ├── .gitkeep
│   ├── central.db       # 运行时生成
│   └── smoke*.db        # Smoke Test 生成
│
├── testbed/
│   ├── __init__.py
│   ├── mock_worker.py
│   ├── run_mock_cluster.py
│   └── smoke_test.py
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_artifacts.py
│   ├── test_sessions.py
│   ├── test_scheduler.py
│   ├── test_release.py
│   └── test_concurrency.py
│
├── .gitignore
├── requirements.txt
└── requirements-dev.txt
```

主要模块关系：

| 路径 | 职责 | 主要关系 |
| --- | --- | --- |
| `app/main.py` | 创建 FastAPI application，组装服务并管理启动/停止生命周期 | 初始化 Database、Artifact Store、Scheduler、Session Manager、Worker Client 与 Worker Registry，并挂载 API router |
| `app/config.py` | 读取环境变量和默认路径 | 为数据库、Artifact、Worker 配置、超时与文件大小限制提供统一 Settings |
| `app/database.py` | 创建 SQLAlchemy engine 和 session factory | 初始化 `db_models.py` 定义的 SQLite 表 |
| `app/db_models.py` | 定义 Artifact、Worker、Session 的持久化模型和状态枚举 | 被 Store、Scheduler、Registry、Session Manager 和 API 查询使用 |
| `app/schemas.py` | 定义 Pydantic 请求/响应模型 | 约束 Artifact、Session、predict 与 Worker API 数据 |
| `app/artifact_store.py` | 接收、校验和原子保存 bit/hwh | 计算 SHA-256、解析 HWH XML，并写入 Artifact metadata |
| `app/scheduler.py` | 执行 Session 资源分配与 FIFO 队列查询 | 在全局 asyncio lock 内随机选择 `IDLE` Worker，并原子执行 reservation |
| `app/session_manager.py` | 编排 Session / Lease 完整生命周期 | 调用 Scheduler 和 Worker Client，负责 deploy once、固定路由、predict 串行化、release 与排队唤醒 |
| `app/worker_registry.py` | 同步 Worker 配置、恢复状态并周期健康检查 | 通过 Worker Client 调用 `/health`、`/status`，维护 `IDLE`、`ERROR`、`OFFLINE` 与活动 Session 状态 |
| `app/worker_client.py` | Central 到 Worker 的异步 HTTP client | 使用 httpx 调用 deploy、predict、release、health 和 status 接口 |
| `app/api/` | 对外 FastAPI router | 暴露 Artifact、Session、Worker 和 Central health API |
| `config/workers.json` | 真实 KV260 Worker Registry 初始配置 | 提供 `kv2601` 至 `kv26020` 的地址 |
| `config/workers.mock.json` | 本地 Mock Cluster 配置 | 提供 loopback Mock Worker 地址 |
| `testbed/` | 本地集成测试环境 | 提供 Mock Worker launcher 和端到端 Smoke Test |
| `tests/` | pytest 自动化测试 | 覆盖 Artifact、Session、调度、release、队列和并发行为 |
| `requirements.txt` | Central Server 运行依赖 | FastAPI、Uvicorn、httpx、SQLAlchemy、Pydantic 和 multipart 支持 |
| `requirements-dev.txt` | 开发与测试依赖 | 复用运行依赖并增加 pytest、pytest-asyncio |

模块调用主路径为：

```text
FastAPI API
    ↓
Artifact Store / Session Manager
                       ↓
             Scheduler + Worker Registry
                       ↓
                  Worker Client
                       ↓
              KV260 Worker HTTP API
                       ↓
             PYNQ Overlay / DMA / MMIO
```
