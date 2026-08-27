# KV260 PYNQ 共享计算平台总体架构

## 1. 文档定位与核心设计原则

本文定义 20 × KV260 共享 PYNQ 计算平台的总体架构。操作文档见 [KV260_Server_Usage_Guide.md](KV260_Server_Usage_Guide.md)，板卡部署见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)，FPGA 技术基础见 [KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)。

核心原则：Student 只上传 Artifact 和提交 `/predict`；Central 内部维护 Student Lease；Worker 在首次 predict 时 Lazy Allocation；同一有效 Lease 固定一块 Worker；单 Student 最多占一块板，单 Worker 最多属于一个 Lease，FPGA concurrency = 1。无资源时请求持久化排队，Central 通过 idle timeout 或 LRU pressure reclaim 自动回收；`BUSY` Worker 永不回收。

## 2. 系统整体框架

```text
                     Student / Client
                           │
                 upload Artifact / predict
                           ▼
                  ┌──────────────────┐
                  │  Central Server  │
                  │ Artifact Store   │
                  │ Lease Manager    │
                  │ Request Queue    │
                  │ Scheduler        │
                  │ Worker Registry  │
                  └────────┬─────────┘
                           │ Worker Allocation
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       kv2601           kv2602          ... kv26020
          │
      PYNQ Worker → Overlay → DMA / MMIO → FPGA → result
```

## 3. 平台角色与职责边界

- Student：上传 `design.bit/design.hwh`，调用 `POST /predict`，在得到 `202` 时按 `request_id` 查询结果。
- Central：保存 Artifact，维护 Lease/Request，原子分配 Worker，部署、转发、排队、回收和健康检查。
- Worker：实现 deploy、predict、release，并以本地 hardware lock 串行访问 Overlay、DMA 和 MMIO。

Student 不创建 Session、不选择 Worker、不保存 `lease_id`，也不主动 release。

## 4. Central Server 总体框架

Central V1 使用 FastAPI、SQLAlchemy、SQLite、httpx 和 asyncio，保持单 Uvicorn process。其持久化对象为 Artifact、StudentLease、PredictRequest 和 Worker；内存锁只用于单进程并发保护，不代替数据库持久化。

```text
API → Lease Manager → Scheduler → Worker Client
 │         │              │            │
 │         └─ Request DB  └─ Worker DB └─ Worker HTTP
 └─ Artifact Store
```

## 5. Central Server 职责

### 5.1 Artifact Store

接收 `student_id/bit/hwh`，校验文件，自动生成每名学生独立的 `v1/v2/...`，保存 SHA-256、manifest 和 metadata。

### 5.2 Lease Manager

维护每名学生唯一的当前 Lease、per-student lock、Request FIFO、Artifact 切换、自动 release、故障处理和恢复。

### 5.3 Scheduler

只负责 Worker allocation。它在全局 `allocation_lock` 内从 `IDLE` 且无 ownership 的 Worker 中随机选择一块，并原子写入 Worker 与 Lease。统一锁顺序是 student lock → allocation lock。

### 5.4 Worker Registry

加载 Worker 配置，通过 `/health` 和 `/status` 恢复及监控状态；未知远端 ownership 标记为 `ERROR`，不会误判为 `IDLE`。每轮检查分为短 DB 快照、无 SQLAlchemy Session 的并发 Worker HTTP I/O、以及新 DB Session 状态合并三个阶段；慢板或不可达板不会长时间占用 SQLite transaction 或阻塞 FastAPI。

### 5.5 WorkerClient

使用内部 `lease_id` 调用 Worker 的 deploy、predict、release、health 和 status。Central 不执行 PYNQ、DMA 或 MMIO。

## 6. Student / Client 职责

正常接口只有：

```text
POST /fpga/artifacts
POST /predict
GET  /requests/{request_id}       # 收到 202 时
GET  /students/{student_id}/status # 可选
```

Student 看不到 Worker ID 和 `lease_id`。

## 7. Artifact 生命周期

上传只保存 Artifact，不占用 FPGA。`version` 由 Central 按 `student_id` 自动递增。每个 PredictRequest 在提交时固定当时最新 Artifact；之后上传 v4 不会改变已经绑定 v3 的旧请求。Lease 复用同一 Artifact 时不重复 deploy；Request 切换到新 Artifact 时在同一 Worker 上重新 deploy 一次。

## 8. Central-managed Student Lease 完整流程

```text
Student has Artifact → POST /predict → create persistent Request
                                  ↓
                         active Lease exists?
                         ├─ yes → ensure Artifact → predict
                         └─ no  → IDLE Worker?
                                  ├─ yes → reserve → deploy → predict
                                  └─ no  → Lease QUEUED / Request QUEUED
```

立即完成返回 `200 completed`；无资源返回 `202 queued + request_id`。后续分配成功后 Central 自动执行，不要求 Student 重发。

## 9. Waiting Queue 与 Worker 分配

Queue 主体是 StudentLease，按首次 `queued_at` FIFO；同一 Student 只出现一次。该 Student 下可有多个 PredictRequest，按 `created_at` FIFO。新增请求不会重置 Lease 的 `queued_at`。

## 10. Student 固定路由与并发模型

```text
student_id → StudentLease → fixed worker_id
```

绑定持续到 idle timeout、pressure reclaim 或故障。同一 Student 的 allocation/deploy/predict/reclaim 由 per-student lock 串行；Worker 本地 hardware lock 提供最后一道保护。

## 11. KV260 Worker 完整流程

Worker 启动后为 `IDLE`。Central `/internal/deploy` 下发 `lease_id`、Artifact metadata、bit/hwh；Worker加载 Overlay 后进入 `READY`。`/predict` 验证 ownership 后串行计算；`/internal/release` 解除 ownership 并回到 `IDLE`。

### 11.1 Student FPGA Hardware ABI

当 Student 以 multipart 上传 JPEG/PNG 时，Central 将其转为 `image_base64/content_type` 内部 payload。Worker 严格解码后执行 `RGB → 28×28 → HWC-to-CHW → 逐通道 z-score`，常量通道置零。硬件 ABI 固定为 Simple DMA `axi_dma_0`：输入 `(3,28,28)` `float32`/9408 bytes，输出 `(12,)` `float32`/48 bytes。DMA 顺序为 `recv.transfer → send.transfer → send.wait → recv.wait`，输入 `flush()`，输出 `invalidate()`，最终使用 `argmax`，不做 softmax。

学生提交的 `design.bit/design.hwh` 必须来自同一次 build，并严格符合上述 ABI。Python Worker 不猜测 IP 名称、tensor layout 或输出语义；不符合时 deploy 或 predict 明确失败。单 Worker hardware concurrency 为 1，Overlay 遵循 deploy once、predict many。

## 12. Overlay 生命周期

Overlay 不随每次 predict 重载。正常路径是 deploy once → predict many；只有当前 Request 固定的 Artifact 与 Worker 已加载 Artifact 不同才重新 deploy。Release 不要求擦除 PL，下一次 deploy 会覆盖旧设计。

## 13. Student Lease 状态机

```text
UNASSIGNED → QUEUED → RESERVED → DEPLOYING → READY
                                      READY ↔ BUSY
READY → RELEASING → UNASSIGNED
故障：ERROR / LOST
```

`READY` 表示仍由 Student 独占，不等于 Worker `IDLE`。

## 14. Worker 状态机

```text
IDLE → RESERVED → DEPLOYING → READY ↔ BUSY
READY → IDLE（自动 release）
故障：ERROR / OFFLINE
```

Scheduler 只选择 `state=IDLE` 且 `lease_id=None` 的 Worker。

## 15. Worker Registry 与 Health Check

Registry 周期检查 `/health` 和 `/status`，核对 `lease_id/artifact_id/fpga_ready`。活动 ownership 一致时恢复 `READY`；远端报告未知 Lease 时标记 `ERROR`。管理员通过 `/workers` 查看具体板卡与 Student ownership。

20 块 Worker 的 HTTP 检查并发执行。合并远端结果时必须重读最新持久化状态；普通 monitor 不得用旧 `/status` 结果覆盖 `RESERVED/DEPLOYING/BUSY/RELEASING` 过渡。

## 16. 故障、自动回收与恢复

- Idle Timeout：`READY`、无 queued/running Request 且空闲超过 `LEASE_IDLE_TIMEOUT_SECONDS`（默认 1800）后释放。
- Pressure Reclaim：存在 queued Student 且无 IDLE Worker时，在空闲超过 `LEASE_RECLAIM_GRACE_SECONDS`（默认 300）的 READY Lease 中按 LRU 回收。
- Reaper：默认每 `LEASE_REAPER_INTERVAL_SECONDS=10` 检查。
- `BUSY` 或仍有 queued/running Request 的 Lease绝不回收。
- Worker 在 RUNNING Request 中故障：当前 Request `FAILED`，不自动 replay；尚未运行的 queued Request 重新等待新 Worker。
- 后续新请求可自动获得新 Lease，无需 Student 干预。

## 17. 数据持久化与 Central 重启恢复

SQLite 保存 Artifact、StudentLease、PredictRequest 和 Worker。PredictRequest 状态为 `QUEUED/RUNNING/COMPLETED/FAILED`，payload/result 均持久化。Central 重启时 `QUEUED` 继续调度；无法安全确认结果的 `RUNNING` 转为 `FAILED`，错误为 `Central restarted while request was running`。旧 `sessions` 表可留在既有 SQLite 中，但新业务不使用。

## 18. Central / Client / Worker API 边界

Student → Central：

```text
POST /fpga/artifacts
GET  /fpga/artifacts
GET  /fpga/artifacts/{artifact_id}
POST /predict
GET  /requests/{request_id}
GET  /students/{student_id}/status
```

Admin → Central：`GET /workers`、`GET /health`。

Central → Worker：`GET /health`、`GET /status`、`POST /internal/deploy`、`POST /predict`、`POST /internal/release`。公开 `/sessions` API 已移除。

## 19. 20 板共享资源与并行模型

20 台健康 KV260 最多支持约 20 个活跃 Student Lease。第 21 名学生的 Lease 与 Request 进入 Queue，等待 IDLE Worker或 LRU reclaim。每个 Lease 内可以连续执行大量请求，但单板 concurrency 始终为 1。

## 20. 当前实现、测试与后续工作

### 20.1 当前已经实现

Artifact 自动版本、SQLite 持久化、StudentLease、PredictRequest、Lazy Allocation、随机 IDLE 分配、固定 Worker、Artifact 切换、FIFO、全局/per-student lock、idle timeout、LRU reclaim、并发 Worker health、Mock Worker、真实 KV260 Worker HTTP contract、PYNQ Overlay 部署、花卉分类图像预处理与 AXI DMA adapter、pytest 和 Smoke Test。

### 20.2 测试边界

Mock 验证 HTTP contract、ownership、deploy count、串行、Queue 和 reclaim，不导入 PYNQ，不操作真实 FPGA。

### 20.3 两层生命周期

基础板卡：SD Card → Runtime Factory → Worker Service → IDLE。

学生计算：Upload Artifact → POST /predict → Central Lease → deploy → predict many → automatic release。

### 20.4 当前尚未实现

其他算法的应用专用 payload/DMA/MMIO adapter、生产身份认证、TLS、HA 和多进程分布式锁。当前花卉 adapter 已在代码中实现并由 fake DMA 单元测试覆盖；本次工作区验证不等于在真实 KV260 上重新完成 hardware verification。V1 只能运行单 Uvicorn process，禁止 `--workers 4`。

## 21. Runtime Factory

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

### 21.2 KV260 端文件系统布局

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

### 21.3 Central Server V1 目录与模块关系

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
