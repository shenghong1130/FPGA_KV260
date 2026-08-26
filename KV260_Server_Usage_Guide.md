# KV260 Central Server 第一版使用说明

本文面向平台管理员、开发人员和测试人员，说明 Central Server V1 的安装、启动、API、Mock Cluster 与测试方法。总体架构见 [KV260_PYNQ_Framework.md](KV260_PYNQ_Framework.md)，板卡制作和基础 Runtime 部署见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

当前 `server/` 已实现 Central Server 调度框架，但尚未实现真实 KV260 业务 Worker。`server/testbed/mock_worker.py` 仅用于本地测试，不会导入 PYNQ，也不会访问 FPGA。

## 1. V1 调度模型

当前 Scheduler 的调度单位是 **Session / Lease**，不是单次 Job。

```text
学生上传 Artifact
        ↓
Central Server 保存 design.bit + design.hwh
        ↓
学生申请 Session
        ↓
Scheduler 从真正 IDLE 的 Worker 中随机选择一块
        ↓
原子 RESERVED
        ↓
向该 Worker 部署一次 Artifact 并初始化 Overlay
        ↓
Session READY
        ↓
学生连续发送多次 predict
        ↓
所有请求固定路由到同一块 Worker
        ↓
学生主动 Release
        ↓
Worker 回到 IDLE
```

Artifact 保存在 `server/data/artifacts/art_<uuid>/`，其生命周期与 Worker 解耦。SQLite 持久化 Artifact、Session 和 Worker 元数据。Scheduler 使用全局 `asyncio.Lock` 保护 `IDLE → RESERVED` 的原子分配，并使用每个 Session 独立的锁串行化 predict 和 release。

### 1.1 Artifact 每个 Session 只部署一次

学生先通过 `POST /fpga/artifacts` 上传 `design.bit` 和 `design.hwh`。Central Server 长期保存这两个文件；创建 Session 时，才将指定 Artifact 从 Artifact Store 部署到被选中的 KV260。

Session 进入 `READY` 后，无论调用 `POST /sessions/{session_id}/predict` 1 次、10 次或 10000 次，都不会再次上传 bit/hwh，也不会重新选择 Worker。

```text
POST /sessions
      ↓
deploy Artifact once
      ↓
READY
      ↓
predict many times on the same Worker
```

### 1.2 `READY` 与 `IDLE` 不同

| 状态 | 含义 | 可分配给新 Session |
| --- | --- | --- |
| `IDLE` | 没有 Session 占用 | 是 |
| `READY` | 已由一个 Session 独占，Overlay 已准备完成，当前没有执行 predict | 否 |
| `BUSY` | 所属 Session 正在执行一次 predict | 否 |

因此 `READY != IDLE`。Scheduler 只能从 `IDLE` Worker 中分配资源，不能把 `READY` Worker 再分配给其他学生。

### 1.3 Session 生命周期

正常生命周期如下：

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

- `QUEUED`：没有可用 Worker，Session 正在 FIFO 队列中等待。
- `RESERVED`：已原子占用一块 Worker。
- `DEPLOYING`：正在部署 Artifact 并初始化 Overlay。
- `READY`：Session 已独占 Worker，可以继续发送 predict。
- `BUSY`：当前有一次 FPGA 请求正在执行，完成后回到 `READY`，不是 `IDLE`。
- `RELEASING`：正在解除 Session 对 Worker 的占用。
- `CLOSED`：Session 已结束，Worker 可回到 `IDLE`。
- `FAILED` / `LOST`：部署失败或活动 Worker 严重故障，当前 Session 不做透明迁移。

### 1.4 Session 不依赖长连接

Session 不要求维持持续不断的 HTTP/TCP 连接。例如先创建 Session，隔几分钟发送一次 predict，最后再 release，完全合法。每次调用都可以是独立 HTTP 请求。

真正维持学生与 KV260 绑定关系的是 `session_id`，Central Server 持久化 `session_id → worker_id`。predict 阶段只查找这个固定 Worker，不再执行全局调度。

### 1.5 Release 与 Session Queue

`DELETE /sessions/{session_id}` 表示学生主动结束使用：

```text
Session → CLOSED
Worker  → IDLE
```

Release 不要求擦除 FPGA，Worker 可以物理保留最后一个 Overlay；下一名学生获得该 Worker 后，新 Artifact 会覆盖当前配置。

当所有 Worker 都已被 Session 占用时，新 Session 返回 `QUEUED`，而不是简单失败。任意 Session release 后，Scheduler 按 FIFO 顺序取出等待项并执行：

```text
QUEUED → RESERVED → DEPLOYING → READY
```

## 2. 安装开发环境

需要 Python 3.12。在仓库根目录执行：

```bash
cd server

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

## 3. 启动 Central Server

### 3.1 使用真实 Worker 配置

默认读取 `config/workers.json`，其中包含 20 台 KV260 的 `8080` 端口地址：

```bash
cd server
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 3.2 使用 Mock Worker 配置

本地测试时使用：

```bash
cd server
source .venv/bin/activate
WORKERS_CONFIG=config/workers.mock.json \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后可访问 FastAPI 交互文档：`http://127.0.0.1:8000/docs`。

## 4. HTTP API 使用

### 4.1 Artifact API

| 方法与路径 | 用途 |
| --- | --- |
| `POST /fpga/artifacts` | 上传并保存一组 bit/hwh Artifact |
| `GET /fpga/artifacts` | 列出 Artifact |
| `GET /fpga/artifacts/{artifact_id}` | 查询单个 Artifact 的元数据与状态 |

上传使用 `multipart/form-data`，字段为 `student_id`、`project_name`、`version`、`bit` 和 `hwh`。服务端会限制文件大小、计算 SHA-256、解析 HWH XML，并以原子方式写入 Artifact Store；用户提交的文件名不会直接作为存储路径。

示例：

```bash
curl -X POST http://127.0.0.1:8000/fpga/artifacts \
  -F student_id=student01 \
  -F project_name=demo \
  -F version=v1 \
  -F bit=@design.bit \
  -F hwh=@design.hwh
```

### 4.2 Session API

| 方法与路径 | 用途 |
| --- | --- |
| `POST /sessions` | 使用已有 Artifact 申请 Session；有空闲 Worker 时开始分配，否则进入队列 |
| `GET /sessions/{session_id}` | 查询 Session 状态、固定 Worker 和错误信息 |
| `POST /sessions/{session_id}/predict` | 将请求转发到该 Session 固定占用的 Worker |
| `DELETE /sessions/{session_id}` | 主动 release Session，并归还 Worker |

创建 Session：

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"student_id":"student01","artifact_id":"<artifact_id>"}'
```

有可用 Worker 时通常返回 `201`；没有可用 Worker 时返回 `202`，状态为 `QUEUED`。

查询、预测与释放：

```bash
curl http://127.0.0.1:8000/sessions/<session_id>

curl -X POST http://127.0.0.1:8000/sessions/<session_id>/predict \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"value":123}}'

curl -X DELETE http://127.0.0.1:8000/sessions/<session_id>
```

### 4.3 平台状态 API

| 方法与路径 | 用途 |
| --- | --- |
| `GET /workers` | 查看 Worker Registry、健康状态和 Session 占用情况 |
| `GET /health` | 查看 Central Server 健康状态 |

### 4.4 Central 到 Worker 的内部接口

真实 KV260 业务 Worker 后续需要实现以下契约：

```text
GET  /health
GET  /status
POST /internal/deploy
POST /predict
POST /internal/release
```

- `/internal/deploy`：Session 初始化时下发一次 `design.bit`、`design.hwh`、`session_id`、`artifact_id` 和哈希。
- `/predict`：Session `READY` 后可以多次调用。
- `/internal/release`：结束 Worker 对当前 Session 的 ownership。

当前 `server/testbed/mock_worker.py` 实现了测试用接口契约，但不执行真实 Overlay、DMA 或 MMIO。

## 5. 自动化测试

```bash
cd server
source .venv/bin/activate
pytest -v
```

测试覆盖 Artifact 校验与 SHA-256、原子分配、排除 `READY` Worker、FIFO 排队与 release、一次部署后在固定 Worker 上连续 100 次 predict、并发创建 Session，以及同一 Session 内 predict 串行化。

## 6. Mock Cluster

### 6.1 启动 Mock Worker

终端 1：

```bash
cd server
source .venv/bin/activate
python -m testbed.run_mock_cluster --workers 3
```

对应地址：

```text
mock-kv2601 → 127.0.0.1:18081
mock-kv2602 → 127.0.0.1:18082
mock-kv2603 → 127.0.0.1:18083
```

需要模拟 20 台 Worker 时执行：

```bash
python -m testbed.run_mock_cluster --workers 20
```

这会使用 `18081` 到 `18100`。按 `Ctrl+C` 时，launcher 会结束其子 Uvicorn 进程。

### 6.2 启动测试用 Central Server

终端 2：

```bash
cd server
source .venv/bin/activate
WORKERS_CONFIG=config/workers.mock.json \
DATABASE_URL=sqlite:///data/smoke.db \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 6.3 运行 Smoke Test

终端 3：

```bash
cd server
source .venv/bin/activate
python -m testbed.smoke_test
```

Smoke Test 使用自动生成的伪 bit 字节和最小合法 HWH XML，不会向仓库提交真实 bitstream。它验证：

- Artifact 上传；
- Session 创建与随机 Worker 分配；
- Session 固定 Worker 路由；
- Artifact 每个 Session 只 deploy 一次；
- 连续 predict；
- 第二个 Session 不会抢占 `READY` Worker；
- release 后 Worker 回到 `IDLE`；
- Session Queue 与 FIFO 自动重新分配；
- 并发 predict 串行化。

## 7. 配置

环境变量可覆盖默认设置：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SERVER_HOST` | `127.0.0.1` | 应用监听地址配置；Uvicorn 命令行参数优先 |
| `SERVER_PORT` | `8000` | 应用监听端口配置；Uvicorn 命令行参数优先 |
| `DATABASE_URL` | `<仓库>/server/data/central.db` 对应的绝对 SQLite URL | SQLite 数据库 |
| `ARTIFACT_ROOT` | `<仓库>/server/data/artifacts` | Artifact 持久化目录 |
| `WORKERS_CONFIG` | `<仓库>/server/config/workers.json` | Worker Registry 配置文件 |
| `WORKER_CONNECT_TIMEOUT` | `2.0` 秒 | 连接 Worker 的超时 |
| `WORKER_REQUEST_TIMEOUT` | `30.0` 秒 | 普通 Worker 请求超时 |
| `WORKER_DEPLOY_TIMEOUT` | `120.0` 秒 | Artifact 部署超时 |
| `HEALTH_INTERVAL_SECONDS` | `5.0` 秒 | Worker 健康检查周期 |
| `HEALTH_FAILURE_THRESHOLD` | `3` | 连续失败多少次后标记故障 |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `0` | V1 预留配置，`0` 表示禁用；当前未实现自动 TTL 回收 |
| `MAX_BIT_SIZE` | `134217728` 字节 | bit 文件大小上限 |
| `MAX_HWH_SIZE` | `16777216` 字节 | hwh 文件大小上限 |

`config/workers.json` 包含 20 台真实 KV260 的 `8080` 端口地址。`config/workers.mock.json` 包含 20 个 loopback 地址；只有由 `run_mock_cluster` 实际启动的 Mock Worker 才会健康并可分配。

## 8. 数据与日志

- Artifact：`server/data/artifacts/art_<uuid>/`
- 默认 SQLite：`server/data/central.db`
- Smoke Test SQLite：`server/data/smoke.db`
- Uvicorn 与测试输出：当前终端

测试前如需清理旧 Smoke Test 状态，应在确认没有需要保留的数据后处理 `server/data/smoke.db`；不要删除生产数据库或 Artifact Store。

## 9. V1 已实现能力与边界

当前 V1 已实现：

- Central Server 基础 REST API；
- Artifact Store 与 SHA-256/HWH 校验；
- SQLite 持久化；
- Worker Registry 与健康检查；
- Session / Lease Scheduler；
- 随机选择 `IDLE` Worker并原子占用；
- FIFO Session Queue；
- Artifact 每个 Session 部署一次；
- Session 固定 Worker 路由和 predict 串行化；
- 主动 release；
- Mock Worker、pytest 与 Smoke Test。

当前尚未实现：

- 身份认证和权限系统；
- Web 前端；
- Redis、Celery 与 HA Scheduler；
- Active Session 的透明 Worker 迁移；
- 真实 KV260 PYNQ 业务 Worker；
- 真实 design.bit/hwh 的业务级协议与 AXI DMA 算法接口；
- TLS、生产环境认证和高级监控。

基础 KV260 Runtime、PYNQ Overlay、XRT/ZOCL 和 `allocate()` 的部署与验收属于板卡层，参见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。
