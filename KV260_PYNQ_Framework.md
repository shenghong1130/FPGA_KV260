# KV260 PYNQ 共享计算平台总体架构

## 1. 总体架构与核心原则

本文以当前代码为准，说明 20 × KV260 共享计算平台从 Student/Robot 上传 Artifact、提交 Predict，到 Central 持久化排队、分配 Worker、部署 PYNQ Overlay、通过 AXI DMA 执行 FPGA、返回结果以及最终释放 Lease 的完整架构。操作见 [KV260_Server_Usage_Guide.md](KV260_Server_Usage_Guide.md)，FPGA/PYNQ 原理见 [KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)，板卡部署见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

```text
Student / Robot ── public HTTP ──→ Central Server
                                      │ internal HTTP + lease_id
                                      ▼
                                  KV260 Worker
                                      │ PYNQ
                                      ▼
                              Overlay → DMA → FPGA → result
```

```text
                              Student / Robot
                                    │
                 ┌──────────────────┴──────────────────┐
                 │                                     │
          Upload Artifact                         POST /predict
   student_id + password + bit/hwh       student_id + password + image
                 │                                     │
                 └──────────────────┬──────────────────┘
                                    ▼
                         ┌───────────────────────┐
                         │ Central Server/FastAPI│
                         └───────────┬───────────┘
       ┌───────────────┬─────────────┼──────────────┬───────────────┐
       ▼               ▼             ▼              ▼               ▼
 StudentAuth     ArtifactStore   LeaseManager    AuditLogger   Admin Dashboard
       │               │          │       │            │
 credentials     Artifact files Scheduler Queue    audit_events
       └───────────────┴──────────┴────────┴────────────┘
                                     │
                                   SQLite
                                     │
                               WorkerClient/httpx
                                     │
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                        ▼
         kv2601                   kv2602                ... kv26020
            │
            ▼
       WorkerState → PYNQ Overlay → axi_dma_0 → FPGA → result
            └────────────────────→ Central → Student
```

核心约束：Student 只访问 Central，不选择 Worker，也不知道内部 `lease_id`；Central 是 Artifact、Lease、Request、Worker 视图和 Audit 的权威所有者；单 Student 最多占一个 Worker，单 Worker 最多属于一个 Lease；同一 Student FIFO 串行，单板 hardware concurrency = 1；无资源时 Request 写入 SQLite 后返回 `202`；`BUSY` 不回收。V1 的 asyncio lock 仅限单进程，因此必须运行单个 Uvicorn process。

## 2. Central Server 总体框架

```text
                              FastAPI
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
           Student API       Admin API        Health/UI
                └────────────────┼────────────────┘
                                 ▼
                           Service Layer
    ┌─────────────┬──────────────┼────────────┬───────────────┐
    ▼             ▼              ▼            ▼               ▼
StudentAuth  ArtifactStore  LeaseManager  Scheduler   WorkerRegistry
                                  └────────────┴───────┬───────┘
                                                       ▼
                                                  WorkerClient
                                                       ▼
                                                  KV260 Worker

Service Layer ──→ SQLAlchemy/SQLite
      ├─────────→ Artifact filesystem
      └─────────→ AuditEvent
```

### 2.1 Composition root 与启动顺序

`main.py` 是 Central 的 composition root/application entry，创建 FastAPI、注册 Router、挂载 Dashboard，并用 lifespan 管理服务。

```text
FastAPI startup
  ↓ Settings.from_env()
Artifact root → Database.initialize()/create_all()
  ↓
WorkerClient → ArtifactStore → Scheduler → AuditLogger
  ↓
WorkerRegistry → LeaseManager → StudentAuth → ArtifactCleanupService
  ↓ app.state.services
sync_config → recover_requests → registry.recover
  ↓
start allocator + reaper + worker monitor

shutdown：monitor → allocator/reaper → httpx → DB engine
```

### 2.2 Central Server V1 模块职责

| 模块 | 职责 | 主要状态/调用 |
| --- | --- | --- |
| `main.py` | FastAPI、Services、Router、UI、lifespan | 初始化、恢复、后台 Task、关闭资源 |
| `config.py` | `Environment Variables → Settings` | 地址/端口、DB、路径、timeout、limit、Admin Token |
| `database.py` | SQLAlchemy lifecycle | Engine、sessionmaker、`Base.metadata.create_all` |
| `db_models.py` | Central Persistent Data Model | Model、Enum、UTC `utcnow()` |
| `schemas.py` | Pydantic HTTP contract | API request/response，不是数据库 |
| `datetime_utils.py` | UTC normalization | SQLite naive UTC → aware UTC API 时间 |
| `student_auth.py` | Student 注册、认证、改密 | scrypt、salt/hash、credentials |
| `admin_auth.py` | 破坏性管理认证 | `X-Admin-Token`、constant-time compare |
| `artifact_store.py` | `.bit/.hwh` 持久化 | validation、SHA、version、manifest、metadata |
| `artifact_cleanup.py` | 旧 Artifact Preview/Execute | protected set、目录删除、ARCHIVED |
| `audit.py` | best-effort 持久化事件 | 独立事务、敏感 details 过滤 |
| `scheduler.py` | 分配 IDLE Worker | oldest queued、allocation lock、RESERVED |
| `lease_manager.py` | 核心 Lease/Request 编排 | FIFO、deploy/predict/release、allocator/reaper/recovery |
| `worker_registry.py` | 配置、健康和 ownership reconcile | health/status、ONLINE/OFFLINE |
| `worker_client.py` | Central → Worker HTTP adapter | health/status/deploy/predict/release |

`config.py` 默认数据库是 `sqlite:///server/data/central.db`，Artifact Root 是 `server/data/artifacts`；还管理 `WORKERS_CONFIG`、Worker timeout、Health interval/threshold、Lease idle/reclaim/reaper、文件/图片上限和 `ADMIN_ACTION_TOKEN`。

`database.py` 管理真正的 Persistent State。`schemas.py` 则定义 HTTP contract；`UtcResponseModel` 调用 `ensure_utc()`，将 SQLite 回读的 naive datetime 按 UTC 解释并输出 aware UTC。

`db_models.py` 定义 `Artifact`、`StudentCredential`、`Worker`、legacy `SessionRecord`、`StudentLease`、`PredictRequestRecord`、`AuditEvent`，以及 `ArtifactStatus`、legacy `SessionStatus`、`LeaseStatus`、`RequestStatus`、`WorkerState`。Model 是持久化结构，Enum 是允许的状态词汇。

### 2.3 API 模块

| 文件 | 作用 |
| --- | --- |
| `api/artifacts.py` | Artifact 上传/查询 |
| `api/predict.py` | predict、单 Request 查询、Admin Request 列表/Student 筛选 |
| `api/students.py` | Student status/password |
| `api/workers.py` | Worker 查询和 Admin manual release |
| `api/events.py` | Audit Event 查询/筛选 |
| `api/admin_artifacts.py` | Cleanup preview/execute |
| `api/health.py` | Worker/Lease/Request 聚合健康状态 |

API 层负责 HTTP validation、认证、Schema 和错误码；复杂状态机留在 Service，Endpoint 不直接重写 ownership。

## 3. Central、Student 与 Worker 的职责

```text
Predict API
    ↓ LeaseManager
    ├─ latest READY Artifact
    ├─ create persistent PredictRequestRecord
    ├─ maintain StudentLease / FIFO
    ├─ Scheduler.reserve_lease()
    ├─ WorkerClient.deploy()
    ├─ WorkerClient.predict()
    └─ WorkerClient.release()
```

Scheduler 只回答“最老 queued Student 应拿到哪块 IDLE Worker”：

```text
oldest queued Student → allocation_lock
  → Worker.state=IDLE AND lease_id=None
  → random choice → generate lease_<uuid>
  → Worker RESERVED + StudentLease RESERVED
```

Scheduler 不 deploy、不 predict。WorkerClient 只做 HTTP，不决定调度、不在 Central 本机执行 PYNQ。

Student 保存自己的密码，上传同一次 build 的 bit/hwh，提交图片，并在 `202` 时用 `request_id` 查询。它只关心 `student_id/password/Artifact/predict/request_id/result`，不创建 Session、不选择板卡、不知道 `worker_id/lease_id/internal API`。

Worker 校验 Central 下发的 ownership 和 Artifact，保存本地副本，加载 Overlay，用单个 hardware lock 串行访问 FPGA，并按正确 Lease release。Student 不直连 Worker。

## 4. 端到端完整业务流程

这是从 Student/Robot 第一次上传 bit/hwh，到后续提交图片、Central 分配 KV260、部署 Overlay、执行 FPGA 推理并返回结果的完整生命周期。它串联认证、Artifact 持久化、Request、Lease、Scheduler、Worker 和 FPGA，最终将结果持久化并返回调用方。

```text
Student first upload(student_id/password/bit/hwh)
  ↓ StudentAuth: register or verify
ArtifactStore: validate → SHA/XML → version → files + metadata
  ↓
POST /predict + password + image
  ↓ authenticate → latest READY Artifact
  ↓ create PredictRequest(QUEUED, fixed Artifact)
StudentLease
  ├─ already owns Worker → reuse
  └─ no Worker → QUEUED → Scheduler
         ├─ IDLE → Lease/Worker RESERVED
         └─ none → remain QUEUED → HTTP 202 + request_id
                     ↓ wait for release/LRU → allocator resumes
  ↓
ensure Artifact
  ├─ already deployed → skip
  └─ different → DEPLOYING → /internal/deploy → PYNQ → READY
  ↓
Request RUNNING + Lease/Worker BUSY
  ↓ Worker /predict
decode → preprocess → DMA → FPGA → 12 float32 → argmax
  ↓
result
  ├─ success → Request COMPLETED + result
  │             ↓ Lease/Worker READY
  │             ├─ next FIFO Request → BUSY
  │             ├─ idle timeout → RELEASING → IDLE
  │             ├─ queue pressure/LRU → RELEASING → IDLE
  │             └─ safe Admin release → IDLE
  └─ Worker predict failure → Request FAILED + error
                              → Lease QUEUED / Worker ERROR
```

## 5. Predict 200/202 与持久化排队流程

`POST /predict` 从 Robot 提交图片开始，先由 Central 认证并持久化 Request，再根据 Worker 是否可用决定同步完成或进入后台队列。这个流程保证暂时没有空闲板卡时，请求仍有稳定的 `request_id` 和可恢复的数据库状态。

```text
Robot POST /predict
  ↓ Central authenticates + persists QUEUED Request
  ├─ Worker available → reserve/deploy/predict → terminal state → HTTP 200
  └─ no Worker → Request/Lease QUEUED → HTTP 202 + request_id
                    ↓ background allocator
                  Worker available → predict → terminal state
                    ↓
                  Robot GET /requests/{id}
```

`202` 表示“请求已经被 Central 接收并持久化，但当前还没有完成”，不是请求失败。收到 `202` 后不要重新 POST 同一个计算，而应使用返回的 `request_id` 查询；Request 的 payload、Artifact 和状态已经在 SQLite。StudentLease 按首次 `queued_at` FIFO，同一 Student 只占一个 Queue 位置；其多个 Request 按 `created_at,id` FIFO。

## 6. Worker、Lease、Queue 分配与回收流程

这个流程从一个需要计算的 Student Request 开始，由 StudentLease 表达排队与 ownership，再由 Scheduler 把最老的 queued Student 分配给 IDLE Worker。计算结束后 Lease 可以继续复用，也可以通过 idle timeout、LRU pressure 或安全的管理员操作进入统一释放路径。

```text
Student Request
      ↓
StudentLease
      ↓
QUEUED
      ↓
Scheduler
      ↓
IDLE Worker
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
┌───────────────┬─────────────────┐
│               │                 │
next Request  idle timeout    LRU / Admin
│               │                 │
BUSY         RELEASING        RELEASING
                │                 │
                └───────┬─────────┘
                        ▼
                       IDLE
```

```text
                    Central Background Tasks
          ┌───────────────────┼──────────────────┐
          ▼                   ▼                  ▼
      Allocator             Reaper          Worker Monitor
 oldest queued Student   idle/LRU release   health/status reconcile
          ▼                   ▼                  ▼
      Scheduler        release_student()     workers/leases DB
```

### 6.1 IDLE Worker

```text
Request → Lease QUEUED → Scheduler → kv2603 IDLE
 → Worker/Lease RESERVED → DEPLOYING → READY
 → Request RUNNING + Worker/Lease BUSY
```

### 6.2 20 块全部占用

```text
Student01→kv2601 ... Student20→kv26020
Student21 → Request QUEUED + Lease QUEUED → HTTP 202 → persistent waiting
```

### 6.3 Idle Timeout

```text
kv2601 READY + no queued/running request
 + idle >= LEASE_IDLE_TIMEOUT_SECONDS(default 1800)
 → Lease RELEASING → Worker /internal/release
 → Worker IDLE + Lease UNASSIGNED + ownership cleared
```

### 6.4 LRU Pressure Reclaim

```text
Student A/kv2601
├─ BUSY → never reclaim
├─ READY but idle < grace → keep
└─ READY + idle >= LEASE_RECLAIM_GRACE_SECONDS
     + another Student QUEUED
     → LRU release → kv2601 IDLE → allocator_event
     → Scheduler → oldest queued Student
```

Reaper 按 `last_activity_at` 从最久未用的 READY Lease 检查。所有来源最终复用 `release_student()`。

### 6.5 Admin Manual Release

```text
Dashboard + Admin Token → POST /workers/{board}/release
 → Central fresh Worker/Lease ownership check
 ├─ BUSY/DEPLOYING/RESERVED/OFFLINE/ERROR or pending Request → 409
 └─ READY + ownership match + no pending → Lease RELEASING
      → Worker release → IDLE → allocator_event
```

它不是 force kill，不取消 RUNNING Request；前端不能指定 owner。

### 6.6 Worker Offline

```text
health/status failures → threshold → Worker OFFLINE
 ├─ no active Lease → remain OFFLINE
 └─ active Lease → LOST
      ├─ RUNNING Request → FAILED (no replay)
      └─ QUEUED remains → Reaper requeues Lease → allocator later retries
```

## 7. KV260 Worker、PYNQ 与 FPGA 执行流程

这一层发生在 Central 已经确定某个 Worker 和 Artifact 之后。Central 本身不直接操作 FPGA，而是通过 httpx 调用 KV260 Worker；Worker 再使用 PYNQ Overlay 和 AXI DMA 驱动 FPGA，最后把推理结果沿原路径返回 Central。

```text
Central
   │
   │ POST /internal/deploy
   ▼
KV260 Worker
   │
   ▼
PYNQ Overlay
   │
   ▼
axi_dma_0
   │
   ▼
FPGA
```

```text
Central /internal/deploy
 → validate ownership/IDs/SHA-256/HWH XML
 → persist local bit/hwh
 → Overlay(design.bit), matching HWH
 → require Simple DMA axi_dma_0 + send/recv channels
 → allocate input(3,28,28) and output(12,) float32
 → fpga_ready / READY
Central /predict
 → Worker hardware asyncio.Lock
 → strict base64 + declared JPEG/PNG validation
 → RGB → 28×28 → HWC-to-CHW → per-channel z-score
 → constant channel = zero
 → copy input → flush
 → recv.transfer(output) → send.transfer(input)
 → send.wait → recv.wait → output.invalidate
 → 12 float32 → argmax
 → predicted_class/result → Central → Student
```

ABI 固定：输入 `(3,28,28)` float32 = 9408 bytes；输出 `(12,)` float32 = 48 bytes；IP 是 Simple DMA `axi_dma_0`。接收先 arm。`confidence` 是 argmax 对应原始硬件值，不做 softmax。Release 释放 CMA buffer/ownership；PL 可能保留 bitstream，下一次 deploy 覆盖。

## 8. 状态机与同步关系

### 8.1 Artifact

```text
successful upload → READY → old + safe Admin Cleanup → ARCHIVED
validation failure → staging rollback / no row
FAILED enum exists, but current upload path does not persist a failed row
```

ARCHIVED ≠ 删除 DB row；只有 READY 可被 latest selection/deploy。

### 8.2 PredictRequest

```text
create → QUEUED → RUNNING → COMPLETED
                    └─────→ FAILED
```

QUEUED 已持久化未执行；RUNNING 已进入 Worker 调用；COMPLETED 保存 result/time；FAILED 保存 error/time，RUNNING 工作不自动重放。

### 8.3 StudentLease

```text
UNASSIGNED → QUEUED → RESERVED → DEPLOYING → READY ↔ BUSY
                                                   ↓
                                              RELEASING
                                                   ↓
                                              UNASSIGNED
异常：ERROR / LOST → (有 queued Request时) QUEUED
```

READY 表示 Student 仍独占 Worker，不表示 Worker 可分配。

`UNASSIGNED` 没有 ownership；`QUEUED` 等待资源；`RESERVED` 已原子获得 board/lease_id；`DEPLOYING` 正在进行远端 Overlay 请求；`READY` 已占用且可接任务；`BUSY` 正在执行 Request；`RELEASING` 正在调用 Worker release；`ERROR/LOST` 表示操作失败或 ownership/连通性丢失。

### 8.4 Worker

```text
OFFLINE/ERROR → successful reconcile → IDLE
IDLE → RESERVED → DEPLOYING → READY ↔ BUSY
READY -- Lease=RELEASING; Worker enum has no RELEASING --> IDLE on success
异常：ERROR / OFFLINE
```

释放失败令 Lease/Worker ERROR。不要虚构 Worker `RELEASING` state。

`IDLE` 才是可分配状态；`RESERVED` 已被 Scheduler 占用；`DEPLOYING` 正在加载 Artifact；`READY` 已被某个 Lease 占用但当前不计算；`BUSY` 正在计算；`ERROR` 是 ownership/操作异常；`OFFLINE` 是健康检查确认不可达。

### 8.5 同步关系

```text
StudentLease       Worker          PredictRequest
QUEUED              —              QUEUED
RESERVED         RESERVED           QUEUED
DEPLOYING        DEPLOYING          QUEUED
READY            READY              QUEUED
BUSY             BUSY               RUNNING
READY            READY              COMPLETED
QUEUED           ERROR              FAILED (Worker predict failure)
LOST             OFFLINE            FAILED (Worker offline while running)
RELEASING        READY              —
UNASSIGNED       IDLE               —

Worker OFFLINE → Lease LOST → RUNNING Request FAILED
```

## 9. Student Password 与 Admin Action 认证

认证机制在业务状态机之外保护两类不同用途的入口：Student Password 用于学生上传、预测与查看自身数据；Admin Action Token 仅用于手动释放 Worker、Artifact Cleanup 等破坏性管理操作。二者相互独立，均不会以明文进入业务数据库或 Audit。

### 9.1 Student Password

```text
student_id + password → StudentAuth.authenticate_or_register()
   ├─ credential absent
   │    → length 8..128 → random 32-byte salt
   │    → scrypt(N=2^14,r=8,p=1,dklen=64)
   │    → store salt + hash
   └─ credential exists
        → derive candidate → hmac.compare_digest
```

第一次成功 Artifact 上传注册 credential。明文 password 不写数据库。以后上传仍用 form password；predict、单 Request、Student status/改密用 `X-Student-Password`。改密先验证旧密码，再生成新 salt/hash 并更新 `updated_at`。

### 9.2 Admin Action Token

```text
X-Admin-Token → ADMIN_ACTION_TOKEN(Settings)
      → hmac.compare_digest
      → manual Worker release / Artifact Cleanup
```

Admin Token 与 Student Password 完全分离。未配置返回 `503`，缺失/错误返回 `401`。Dashboard 仅以 `sessionStorage` 保存 token，不写死、不放入 URL/Toast/Audit。

## 10. Artifact 内容、持久化、版本与 Overlay 复用

Artifact 同时包含可部署文件、持久化 metadata 和按 Student 独立递增的版本。Request 在创建时绑定具体 Artifact；Worker 只有在当前 ownership、Artifact 和状态都匹配时才复用已经加载的 Overlay。

### 10.1 Artifact 文件组成

```text
Student Artifact
├── design.bit
│    └─ FPGA configuration bitstream
├── design.hwh
│    └─ Hardware metadata: AXI IP / address map / DMA information
├── manifest.json
│    └─ Artifact identity, version, hashes and sizes
└── Central metadata
     ├─ artifact_id / student_id / version
     ├─ bit_sha256 / hwh_sha256
     ├─ bit_size / hwh_size
     ├─ bit_path / hwh_path
     └─ created_at / status
```

`design.bit + design.hwh` 必须来自同一次 build；`manifest.json` 由 Central 在验证并分配版本后生成。

### 10.2 Artifact 持久化流程

ArtifactStore 在 staging 分块写入，验证扩展名、非空、size、SHA-256 和 HWH XML，再在 `version_allocation_lock` 内分配版本、生成 `manifest.json`、原子移动目录并提交 DB metadata。

```text
Upload → staging → validate → SHA/XML → version lock → manifest
       → atomic move to server/data/artifacts/art_<uuid>/
       → Artifact row in SQLite
```

文件系统保存可部署实体，SQLite 保存身份、版本、hash、size、路径和状态。当前 validation 失败会清理 staging，不创建 FAILED row；`FAILED` 虽在 Enum 中，但不是当前失败上传的落库路径。

### 10.3 Artifact 版本规则

```text
student01: upload #1→v1 → #2→v2 → #3→v3 → cleanup old → #4→v4
student02: v1 → v2
student03: v1
```

版本按 Student 独立单调递增；ARCHIVED row 仍参与历史最大版本计算，不重用旧号。只有 `READY` Artifact 会参与 latest selection，ARCHIVED Artifact 保留 metadata 和历史版本号，但不能再次 deploy。

### 10.4 Request 与 Artifact 绑定

```text
latest=v3 → create req_A(v3)
              ↓ Student uploads v4
req_A still v3；new req_B uses v4
```

Request 创建瞬间固定 `artifact_id + artifact_version`，后续上传新版本不会改变已存在 Request 使用的 Artifact。

### 10.5 Overlay 复用规则

```text
first v3 request → deploy v3 → predict → READY
next v3 request  → skip deploy → predict
new v4 request   → deploy v4 → predict
```

只有 StudentLease 与 Worker 的 `current_artifact_id` 同时匹配 Request 的 `artifact_id`，且 Worker 状态正确，才复用 Overlay；否则必须先通过 `/internal/deploy` 加载目标 Artifact。

## 11. Central Persistent Database

```text
FastAPI → SQLAlchemy ORM → SQLite → server/data/central.db(default)
```

`Database` 创建 Engine 和 `sessionmaker(expire_on_commit=False)`，启动执行 `Base.metadata.create_all()`。SQLite 是 Persistent State，process 结束数据不消失。

| 表 | 主要字段/内容 | 作用 |
| --- | --- | --- |
| `artifacts` | id/student/version、bit/hwh path/hash/size、time/status、legacy project_name | Artifact metadata；实体另存 filesystem |
| `student_credentials` | student_id、password_salt/hash、created/updated | 不含 plaintext password |
| `workers` | board/base_url/state、lease/current artifact、fpga_ready、seen/error | Central Worker 持久化视图；lease 映射 legacy `session_id` column |
| `student_leases` | student/lease/worker/current artifact、state、times/count/error | Student ↔ Worker ownership |
| `predict_requests` | id/student/artifact/version/status/payload/result/times/error | persistent Queue/history；202 后仍存在 |
| `audit_events` | id/type/level/actor、student/board/artifact/request、message/details/time | Persistent history |
| `sessions` | legacy SessionRecord | compatibility only，不是当前调度核心 |

各表保存的字段范围如下：

- `artifacts`：`id`、`student_id`、`version`、`bit_path`、`hwh_path`、两个 SHA-256、两个 size、`created_at`、`status`，以及只为旧 SQLite NOT NULL 兼容而保留的 `project_name` column。
- `student_credentials`：`student_id`、`password_salt`、`password_hash`、`created_at`、`updated_at`，绝无 plaintext password。
- `workers`：`board`、`base_url`、`state`、`lease_id`、`current_artifact_id`、`fpga_ready`、`last_seen`、`last_error`。
- `student_leases`：`student_id`、`lease_id`、`worker_id`、`current_artifact_id`、`state`、`created_at`、`queued_at`、`activated_at`、`last_activity_at`、`released_at`、`request_count`、`error`。
- `predict_requests`：`id/request_id`、`student_id`、`artifact_id`、`artifact_version`、`status`、`payload`、`result`、`created_at`、`started_at`、`completed_at`、`error`。
- `audit_events`：`id`、`event_type`、`level`、`actor_type/id`、`student_id`、`board`、`artifact_id`、`request_id`、`message`、`details`、`created_at`。
- `sessions`：legacy `id/student/artifact/worker/status`、生命周期时间、request count 和 error；它只为既有数据库非破坏兼容。

```text
StudentCredential     Artifact
 student_id           student_id
                           │ actual FK artifact_id
                           ▼
                    PredictRequestRecord
                           │ student_id logical
                           ▼
                      StudentLease
                           │ worker_id logical
                           ▼
                         Worker

AuditEvent logical refs: student_id / artifact_id / request_id / board
```

只有 `PredictRequestRecord.artifact_id` 和 legacy `SessionRecord.artifact_id` 声明了到 Artifact 的实际 ForeignKey；Lease/Worker/Audit 关联由代码维护，不应说成不存在的 FK。业务表说明“现在是什么状态”，Audit 说明“以前发生过什么”。

```text
Central Persistence
├─ SQLite: credentials / metadata / Requests / Leases / Workers / Audit
└─ Artifact filesystem
   └─ art_<uuid>/{design.bit, design.hwh, manifest.json}
```

Cleanup 删除安全旧目录并设 ARCHIVED，不删除 Artifact row/历史 Request。

```text
Persistent after restart       Ephemeral, rebuilt after restart
SQLite                         student_locks / allocation_lock
Artifact files                 version/cleanup/auth locks
Audit Events                   allocator_event / background Tasks
                               Worker process hardware objects/lock
```

Lock 用于 concurrency safety，Database 用于 persistence。进程内锁不支持多 Uvicorn worker。

## 12. Worker Registry、Health 与 Ownership Recovery

```text
workers.json → sync_config → workers table
  ↓ WorkerRegistry monitor
Phase A: short DB endpoint snapshot
Phase B: concurrent /health + /status, no DB Session held
Phase C: fresh DB read + reconcile
  ├─ remote empty → IDLE
  ├─ ownership match → READY/recovered
  └─ unknown/mismatch → ERROR / Lease LOST
```

普通 Monitor 不用旧 response 覆盖 RESERVED/DEPLOYING/BUSY/RELEASING transition。正常监控连续失败达到 threshold 才 OFFLINE；startup recovery 立即检查。只有实际状态变化才记录一次 WORKER_OFFLINE/ONLINE，不记录每轮成功 polling。

## 13. Audit / Event

```text
LOGGER.info/warning/error → console/journalctl

AuditLogger.record → audit_events/SQLite → Dashboard Events / GET /events
```

AuditLogger 用独立短事务，失败只 `LOGGER.exception`，不令主业务失败；details 过滤 password/hash/salt、Admin Token、secret、payload、`image_base64`。

```text
ARTIFACT_UPLOADED → REQUEST_CREATED → WORKER_ASSIGNED
 → FPGA_DEPLOYED/FAILED → REQUEST_STARTED
 → REQUEST_COMPLETED or REQUEST_FAILED
```

当前还记录 `AUTH_FAILED`、`STUDENT_PASSWORD_CHANGED`、`WORKER_OFFLINE/ONLINE/RELEASED`、`ADMIN_WORKER_RELEASE`、`ARTIFACT_ARCHIVED`、`ARTIFACT_CLEANUP_FAILED`、`ADMIN_ARTIFACT_CLEANUP`。不记录每次 health/status success、UI GET、完整 payload、图片 base64、密码或 token。

## 14. Artifact Cleanup

```text
Admin token → GET cleanup-preview → calculate protected set
  ├─ each Student latest READY
  ├─ StudentLease.current_artifact_id
  ├─ Worker.current_artifact_id
  ├─ QUEUED Request.artifact_id
  └─ RUNNING Request.artifact_id
 → show candidates → Admin confirm → POST cleanup
 → RECALCULATE protected set → student lock + candidate recheck
  ├─ protected/not READY → keep
  └─ safe → validate artifact_root/art_<uuid>, reject escape/symlink
       → delete directory → status ARCHIVED → ARTIFACT_ARCHIVED
 → ADMIN_ARTIFACT_CLEANUP summary
```

Latest READY、Active Worker/Lease、QUEUED/RUNNING Artifact 永远保护。仅 COMPLETED/FAILED 历史引用的旧实体可归档，metadata/Request 关系保留。单项失败不设 ARCHIVED且继续其他项；目录已缺失可安全归档，freed bytes=0。

## 15. Central 重启恢复

```text
restart → open SQLite/create missing tables → initialize Services
 → WorkerRegistry.sync_config()
 → LeaseManager.recover_requests()
    ├─ QUEUED stays QUEUED
    └─ RUNNING → FAILED("Central restarted while request was running")
                   + Lease LOST
 → WorkerRegistry.recover(): concurrent health/status + reconcile
 → start allocator/reaper/monitor
```

RUNNING 不自动 replay。SQLite 与 Artifact files 仍在；locks/events/tasks 在新 process 重建。

## 16. API 边界

```text
Student --public API + Student Password--> Central
Admin   --monitor/Admin Token actions----> Central
Central --internal API + lease_id--------> Worker --PYNQ--> FPGA
```

| 边界 | 当前接口 |
| --- | --- |
| Student → Central | Artifact upload/query、predict、single Request、Student status/password |
| Admin → Central | health/workers/global requests/events、manual release、cleanup |
| Central → Worker | health/status、internal deploy、predict、internal release |

单 Request 查询按所属 student 验证密码；全局 Request/Event 是 Admin Dashboard 视图。公开 Session API 已移除。

## 17. 20 板共享模型

```text
                         Central Scheduler
        ┌──────────────────────┼─────────────────────────┐
        ▼                      ▼                         ▼
     kv2601                 kv2602                  ... kv26020
   Student A/READY        Student B/BUSY             Student T/READY

Student U → no IDLE → Request/Lease QUEUED FIFO
 → Worker becomes IDLE OR LRU grace reached
 → Scheduler → oldest queued Student receives Worker
```

20 台健康板最多约 20 个活跃 Student Lease。每板可连续执行多个当前 Student 的 FIFO Request，但硬件串行。Scheduler 在 IDLE 候选中随机选，所以物理板号只在当前 Lease 生命周期内固定。

## 18. 并发与故障隔离

```text
same Student → student_lock
 → allocation path additionally allocation_lock
 → short DB transaction publishes transition
 → network await with no SQLAlchemy Session
 → fresh DB read validates lease/worker/base_url/state
 → accept or discard stale network result
```

统一锁顺序是 student lock → allocation lock。Artifact version、credential、cleanup 另有范围明确的 lock；Worker hardware lock 是最后一道保护。慢 deploy/predict/health 期间不持有 DB Session，返回后重新核对 ownership，防止旧结果复活 released/LOST Lease。Audit 故障隔离于主业务；Cleanup 和 manual release 都不能 force-kill BUSY 工作。

## 19. 当前实现、Dashboard、测试与限制

当前是 FastAPI + SQLAlchemy + SQLite + asyncio + httpx 的单进程 Central，配套原生 HTML/CSS/JavaScript Dashboard。Dashboard 有 Overview、Workers、Artifacts、Requests、Student、Events、Tools；Admin Token 只在 sessionStorage。

### 19.1 Central 软件栈关系

```text
机器人 / 浏览器 / curl
        │
        │ HTTP 请求
        ▼
┌─────────────────────┐
│       FastAPI       │
│   接收和返回 HTTP    │
└─────────┬───────────┘
          │
          │ 调用业务逻辑
          ▼
┌─────────────────────┐
│       asyncio       │
│ 并发调度 / 等待 / 锁 │
└──────┬────────┬─────┘
       │        │
       │        │
       ▼        ▼
 SQLAlchemy    httpx
     │           │
     │           │ HTTP
     ▼           ▼
   SQLite      KV260 Worker
 central.db      :8080
```

| 技术 | 当前项目中的作用 |
| --- | --- |
| FastAPI | 对外提供 HTTP API，接收 Robot、Student、Dashboard 和 curl 请求 |
| asyncio | 管理异步任务、Worker 调度、等待、Event 和 Lock |
| SQLAlchemy | Python 与 SQLite 之间的 ORM / 数据访问层 |
| SQLite | 持久化 Artifact metadata、Request、Lease、Worker、Credential 和 Audit Event |
| httpx | Central 主动访问 KV260 Worker |

FastAPI 负责“别人访问 Central”，httpx 负责“Central 访问 Worker”；SQLAlchemy 负责操作数据，SQLite 负责持久化数据，asyncio 负责并发和调度。

### 19.2 一次 `/predict` 的真实调用链

下面用一次 TonyPi Robot 请求把 Central 的 Web、调度、持久化和 Worker/FPGA 执行组件连接起来。图中以已经满足直接 predict 条件的路径为主；需要加载或切换 Artifact 时，Central 会先执行后面的 deploy 分支。

```text
TonyPi Robot
    │
    │ HTTP POST /predict
    ▼
FastAPI
    │
    │ 接收 student_id + image
    ▼
LeaseManager
    │
    │ asyncio.Lock
    ▼
SQLAlchemy
    │
    │ 查 Student / Artifact / Lease
    ▼
SQLite central.db
    │
    │ 找到：
    │ student01
    │ latest Artifact v4
    │ Worker kv2601
    ▼
LeaseManager
    │
    │ asyncio 调度
    ▼
httpx
    │
    │ POST http://kv2601:8080/predict
    ▼
KV260 Worker
    │
    ▼
PYNQ
    │
    ▼
FPGA
    │
    │ 结果
    ▼
httpx
    │
    ▼
LeaseManager
    │
    ▼
SQLAlchemy
    │
    │ 保存 Request result
    ▼
SQLite
    │
    ▼
FastAPI
    │
    │ HTTP Response
    ▼
TonyPi Robot
```

1. TonyPi 不直接连接 KV260 Worker。
2. TonyPi 只调用 Central FastAPI 的 `/predict`。
3. LeaseManager 根据 StudentLease、Artifact 和 Worker 状态选择或等待 Worker。
4. SQLAlchemy 查询、更新 SQLite 中的 Student、Artifact、Lease 和 Request 持久化状态。
5. asyncio 的 Student lock 保护同一 Student 的并发操作，后台任务负责 allocator、reaper 和 monitor。
6. httpx 是 Central 调用 KV260 Worker 的 HTTP 客户端。
7. Worker 使用 PYNQ Overlay 和 DMA 操作 FPGA。
8. FPGA 结果经 Worker → httpx → LeaseManager 返回 Central。
9. Central 通过 SQLAlchemy 把 Request result、状态和完成时间写回 SQLite。
10. FastAPI 最终把 HTTP Response 返回 TonyPi；若先返回 `202`，Robot 后续按 `request_id` 查询。

简化图中的直接 `/predict` 仅适用于 StudentLease 和 Worker 的 `current_artifact_id` 都等于 Request 的 `artifact_id`，且 Worker 状态正确的情况。否则必须先部署：

```text
LeaseManager
     ↓
httpx
     ↓
POST /internal/deploy
     ↓
KV260 Worker
     ↓
PYNQ Overlay
     ↓
FPGA Ready
     ↓
POST /predict
```

```text
StudentLease.current_artifact_id == request.artifact_id
Worker.current_artifact_id       == request.artifact_id
Worker state is correct
     ↓
skip deploy
     ↓
direct predict
```

### 19.3 当前能力、测试与限制

已实现 Student scrypt password、Artifact version、persistent Request Queue、StudentLease、Lazy Allocation、FIFO、deploy once/predict many、idle/LRU/manual release、Worker offline/recovery、Central restart recovery、Audit/Event、ARCHIVED、Artifact Cleanup 和真实花卉 DMA adapter。

Server pytest 覆盖认证、Artifact、Request、调度、并发、恢复、Audit、Cleanup、UI；Worker pytest/fake DMA 覆盖 ownership、Overlay、preprocessing、ABI；Mock Cluster 验证 HTTP contract但不等于真实 FPGA 验证。

当前没有 Redis、PostgreSQL、RabbitMQ、Celery、Kubernetes scheduler、跨进程锁、RUNNING 自动 replay、Student 直连 Worker或 Student release API；也没有 TLS、HA、完整 RBAC/SSO。其他算法必须实现专用 payload/DMA/MMIO adapter，不能自动套用花卉 ABI。

## 20. Runtime Factory

Runtime Factory 只负责建立板卡基础能力：

```text
[preflight] System and FPGA Manager
[1/8] XRT userspace
[2/8] XRT-matched ZOCL DKMS driver
[3/8] XRT-matched Python 3.12 pyxrt binding
[4/8] Minimal PYNQ 3.1.2 packages and runtime assets
[5/8] PYNQ device tree and boot services
[6/8] Minimal PYNQ functional validation
[7/8] KV260 Worker HTTP service
[8/8] Final diagnostics and worker runtime report
```

它验证 XRT、ZOCL、pyxrt、PYNQ、Device Tree、`EmbeddedDevice` 和 `allocate()`，随后安装并启用 `kv260-worker.service`。Runtime Factory 同时安装 Worker HTTP contract、Overlay 部署能力、Pillow 图像解码依赖和花卉 AXI DMA adapter；Worker 与 PYNQ 共用 `/opt/kv260-pynq` venv，固定 `fastapi==0.115.13` / `pydantic==1.10.22`。安装过程会窄化修正 pynqmetadata 0.1.9 对旧 `pydantic==1.9.1` 的包元数据约束，并以 `pip check` 和实际 import 验证 Python 3.12 运行组合。Central 仍负责 Artifact、Lease 与 Request。

### 20.1 部署 PC 端仓库布局

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
├── worker/
│   ├── app/
│   │   ├── main.py
│   │   ├── state.py
│   │   └── fpga.py
│   ├── requirements.txt
│   ├── install_worker.sh
│   └── kv260-worker.service
├── server/
└── logs/
    └── kv260N.log
```

### 20.2 KV260 端文件系统布局

```text
KV260 filesystem
│
├── /tmp/kv260-runtime/
│   ├── runtime/
│   ├── scripts/
│   └── worker/
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
├── /opt/kv260-worker/
│   ├── app/
│   └── requirements.txt
│
├── /var/lib/kv260-worker/
│   └── artifacts/
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
│       ├── kv260-pynq-clear-pl-state.service
│       └── kv260-worker.service
│
└── /lib/modules/<kernel>/updates/dkms/
    └── zocl.ko*
```

`/tmp/kv260-runtime` 是 PC launcher 通过 SSH 上传的临时 staging，不是最终安装目录。主要持久化 Runtime 位于 `/opt/kv260-pynq`，Worker 位于 `/opt/kv260-worker`，Worker Artifact 位于 `/var/lib/kv260-worker/artifacts`；系统 pyxrt 位于 `/usr/local/lib/python3.12/dist-packages`；systemd 配置位于 `/etc/systemd/system`；ZOCL 由 DKMS 和 `/lib/modules` 管理。

| 路径 | 用途 | 分类 |
| --- | --- | --- |
| `/tmp/kv260-runtime` | SSH 上传 Runtime、检查脚本与 Worker | 临时 staging |
| `/opt/kv260-pynq` | Python venv 和 Minimal PYNQ | 持久化 Runtime |
| `/opt/kv260-pynq/share/kv260-runtime` | DTS/DTBO、安装辅助和验证脚本 | 持久化 Runtime |
| `/opt/kv260-worker` | Worker FastAPI 应用 | 持久化 Worker Runtime |
| `/var/lib/kv260-worker/artifacts` | Worker 收到的 bit/hwh | 持久化 Worker 数据 |
| `/usr/local/lib/python3.12/dist-packages/pyxrt...so` | 与 XRT Debian 版本匹配的 pyxrt | 持久化系统 Python 扩展 |
| `/var/cache/kv260-runtime/xrt-source` | pyxrt 源码与构建缓存 | 可重建缓存 |
| `/etc/profile.d/kv260-pynq.sh` | Shell Runtime 环境 | 系统配置 |
| `/etc/xocl.txt` | KV260 XRT/PYNQ 板卡配置 | 系统配置 |
| `/etc/systemd/system/kv260-pynq-dt.service` | 启动时加载 PYNQ DT overlay | systemd 配置 |
| `/etc/systemd/system/kv260-pynq-clear-pl-state.service` | 启动时清理 PYNQ PL state | systemd 配置 |
| `/etc/systemd/system/kv260-worker.service` | 启动真实 Worker HTTP API | systemd 配置 |
| `/opt/fpga` | 默认应用 bit/hwh | 应用文件 |
| `/lib/modules/<kernel>/updates/dkms/zocl.ko*` | 当前 kernel 的 ZOCL module | DKMS/package 管理 |

### 20.3 Central Server V1 目录与模块关系

```text
server/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── db_models.py
│   ├── schemas.py
│   ├── artifact_store.py
│   ├── scheduler.py
│   ├── lease_manager.py
│   ├── worker_registry.py
│   ├── worker_client.py
│   └── api/
│       ├── artifacts.py
│       ├── predict.py
│       ├── students.py
│       ├── workers.py
│       └── health.py
├── config/
│   ├── workers.json
│   └── workers.mock.json
├── data/
├── testbed/
│   ├── mock_worker.py
│   ├── run_mock_cluster.py
│   └── smoke_test.py
├── tests/
│   ├── conftest.py
│   ├── test_artifacts.py
│   ├── test_sessions.py
│   ├── test_scheduler.py
│   ├── test_release.py
│   └── test_concurrency.py
├── requirements.txt
└── requirements-dev.txt
```

主路径：FastAPI API → Artifact Store / Lease Manager → Scheduler / Worker Registry → Worker Client → KV260 Worker。`session_manager.py` 与公开 `api/sessions.py` 已移除；旧数据库 `sessions` 表仅作非破坏兼容。
