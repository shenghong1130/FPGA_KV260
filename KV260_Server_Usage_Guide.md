# KV260 Central Server 使用说明

## 1. 文档用途

本文只介绍 Central Server 的实际安装、启动、HTTP API、Mock Cluster 和测试方法。

相关文档：

- 总体系统架构：[KV260_PYNQ_Framework.md](KV260_PYNQ_Framework.md)
- PYNQ / FPGA 基础原理：[KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)
- SD 卡制作和 KV260 Runtime 部署：[KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)

当前 Central Server V1 和 Mock Worker 已经实现。真实 KV260 PYNQ 业务 Worker仍属于下一阶段；Mock Worker 只用于验证 Central Server 的 HTTP contract 和调度流程，不执行真实 FPGA 计算。

操作时只需记住：Session 创建后会固定使用一块 Worker，直到显式 Release。完整调度原理和状态机见 Framework。

## 2. 安装开发环境

需要 Python 3.12。若当前位于仓库根目录，进入 `server/`：

```bash
cd <repo>/server
```

如果提示符已经类似：

```text
~/kv260/server$
```

就不要再次执行 `cd server`，否则会尝试进入不存在的 `server/server/`。

创建并启用 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` 会先包含 `requirements.txt` 中的运行依赖，再增加 pytest 和 pytest-asyncio。只需要运行 Central Server 时可以安装：

```bash
python -m pip install -r requirements.txt
```

## 3. 启动 Central Server

### 3.1 使用真实 KV260 Worker

默认 Worker 配置是 `server/config/workers.json`，包含 `kv2601` 至 `kv26020` 的 `8080` 端口地址。

```bash
cd <repo>/server
source .venv/bin/activate

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`0.0.0.0` 表示监听 Central Server 的所有网络接口。其他电脑不能使用 `127.0.0.1` 访问它，应使用 Central Server 的实际 IP：

```text
http://<CENTRAL_IP>:8000
```

例如 Central Server 实际地址为 `192.168.31.10` 时，客户端使用：

```text
http://192.168.31.10:8000
```

该地址仅为示例，并不是项目规定的固定 Central IP。当前真实 KV260 业务 Worker 尚未实现，因此使用真实配置启动 Central 后，未提供 Worker HTTP 服务的板卡会显示为 `offline`。

### 3.2 使用 Mock Worker

本地测试使用 `config/workers.mock.json`：

```bash
cd <repo>/server
source .venv/bin/activate

WORKERS_CONFIG=config/workers.mock.json \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

需要隔离测试数据库时推荐：

```bash
WORKERS_CONFIG=config/workers.mock.json \
DATABASE_URL=sqlite:///data/smoke.db \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`workers.mock.json` 只提供 Mock Worker 地址，不会创建任何 Worker 进程。真正的 Mock Worker 需要另一个终端运行：

```bash
python -m testbed.run_mock_cluster
```

完整启动顺序见第 6 节。

### 3.3 确认服务器启动成功

终端出现类似内容表示 Uvicorn 与应用 startup 已完成：

```text
Application startup complete.
Uvicorn running on http://...
```

检查 Central 健康接口：

```bash
curl http://127.0.0.1:8000/health
```

打开 FastAPI 自动生成的交互式 API 页面：

```text
http://127.0.0.1:8000/docs
```

远程访问时，将 `127.0.0.1` 换成 Central Server 的实际 IP。

## 4. HTTP API 使用

### 4.1 API 使用关系

```text
Student / Client
       │
       │ HTTP
       ▼
Central Server :8000
       │
       │ internal HTTP
       ▼
KV260 Worker :8080
```

API 分为三类：

| 调用方向 | 用途 |
| --- | --- |
| Student / Client → Central | 上传 Artifact、创建和使用 Session |
| Admin → Central | 查看 Worker 与 Central 状态 |
| Central → Worker | 部署 Artifact、执行计算和释放 ownership |

学生正常只访问 Central Server，不直接调用 KV260 Worker。

以下示例默认：

```text
Central URL = http://127.0.0.1:8000
```

从其他电脑访问时替换为 `http://<CENTRAL_IP>:8000`。

### 4.2 学生上传 Artifact：Student → Central

发送方是 Student / Client，接收方是 Central Server。在创建 Session 前调用：

```text
POST /fpga/artifacts
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 当前限制 |
| --- | --- | --- |
| `student_id` | text | 1～128 字符 |
| `bit` | file | 非空 `.bit` 文件，默认最大 128 MiB |
| `hwh` | file | 非空 `.hwh` 文件，默认最大 16 MiB，必须是可解析 XML |

Artifact 版本由 Central Server 按 `student_id` 自动递增生成；同一学生第一次上传为 `v1`，后续依次为 `v2`、`v3`……不同学生分别从 `v1` 开始。

```bash
curl -X POST http://127.0.0.1:8000/fpga/artifacts \
  -F student_id=student01 \
  -F bit=@design.bit \
  -F hwh=@design.hwh
```

典型 HTTP `201 Created` 响应：

```json
{
  "artifact_id": "art_xxxxxxxxx",
  "student_id": "student01",
  "version": "v1",
  "status": "ready",
  "bit_sha256": "...",
  "hwh_sha256": "...",
  "bit_size": 123456,
  "hwh_size": 12345,
  "created_at": "2026-08-26T10:00:00+00:00"
}
```

保存返回的 `artifact_id`，后续创建 Session 需要使用它。上传内容校验失败返回 HTTP `422`。

### 4.3 查询 Artifact：Student / Admin → Central

列出全部 Artifact：

```text
GET /fpga/artifacts
```

```bash
curl http://127.0.0.1:8000/fpga/artifacts
```

成功时返回 HTTP `200` 和 Artifact metadata 数组。查询单个 Artifact：

```text
GET /fpga/artifacts/{artifact_id}
```

```bash
curl http://127.0.0.1:8000/fpga/artifacts/art_xxxxx
```

响应字段与上传成功响应相同。这两个接口只返回 metadata，不返回 bit/hwh 文件内容；不存在的 `artifact_id` 返回 HTTP `404`。

### 4.4 创建 Session：Student → Central

发送方是 Student，接收方是 Central Server。Artifact 上传成功后调用：

```text
POST /sessions
Content-Type: application/json
```

请求 body：

```json
{
  "student_id": "student01",
  "artifact_id": "art_xxxxx"
}
```

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "student_id": "student01",
    "artifact_id": "art_xxxxx"
  }'
```

立即分配成功时返回 HTTP `201 Created`：

```json
{
  "session_id": "sess_xxxxx",
  "student_id": "student01",
  "artifact_id": "art_xxxxx",
  "status": "ready",
  "worker": "mock-kv2602",
  "request_count": 0,
  "error": null
}
```

没有可用 Worker 时返回 HTTP `202 Accepted`：

```json
{
  "session_id": "sess_xxxxx",
  "student_id": "student01",
  "artifact_id": "art_xxxxx",
  "status": "queued",
  "worker": null,
  "request_count": 0,
  "error": null
}
```

保存 `session_id`；后续 query、predict 和 release 都使用它。

当前明确错误包括：Artifact 不存在返回 `404`，Artifact 属于其他 `student_id` 返回 `403`，Session 冲突返回 `409`，Worker deploy 失败返回 `502`。完整 Session 调度逻辑见 Framework。

### 4.5 查询 Session：Student → Central

```text
GET /sessions/{session_id}
```

```bash
curl http://127.0.0.1:8000/sessions/sess_xxxxx
```

成功时返回 HTTP `200`。响应字段：

| 字段 | 含义 |
| --- | --- |
| `session_id` | Session ID |
| `student_id` | 创建 Session 的学生 ID |
| `artifact_id` | Session 使用的 Artifact |
| `status` | 当前 Session 状态 |
| `worker` | 已分配的 Worker；排队时为 `null` |
| `request_count` | 已成功完成的 predict 数量 |
| `error` | 错误信息；正常时为 `null` |

不存在的 Session 返回 HTTP `404`。如果创建时得到 `queued`，客户端可以周期查询该接口，直到 `status` 变为 `ready` 后再发送 predict；不需要新的 polling API。

### 4.6 执行计算：Student → Central → Worker

Student 发送给 Central：

```text
POST /sessions/{session_id}/predict
Content-Type: application/json
```

当前 `payload` 必须是 JSON object，其内部字段由业务协议决定：

```bash
curl -X POST \
  http://127.0.0.1:8000/sessions/sess_xxxxx/predict \
  -H 'Content-Type: application/json' \
  -d '{
    "payload": {
      "value": 123
    }
  }'
```

Mock Worker 典型响应：

```json
{
  "ok": true,
  "board": "mock-kv2602",
  "session_id": "sess_xxxxx",
  "artifact_id": "art_xxxxx",
  "predict_index": 1,
  "input": {
    "value": 123
  }
}
```

成功时 Central 将 Worker 返回的 JSON 以 HTTP `200` 返回给 Student。

实际调用方向：

```text
Student
   │ POST /sessions/{id}/predict
   ▼
Central Server
   │ POST /predict
   ▼
Session 固定的 KV260 Worker
   │
   ▼
result → Central → Student
```

学生不应直接调用 `http://kv260N:8080/predict`，因为 ownership 和固定路由由 Central 管理。同一 Session 可以使用相同 `session_id` 连续调用多次 predict，不需要每次重新创建 Session。

Session 不存在返回 `404`；Session 还未 `ready`、已经关闭或状态冲突时返回 `409`；Worker 调用失败返回 `502`。真实 Worker 的业务请求和响应格式尚未最终实现，上例响应是当前 Mock Worker contract。

### 4.7 Release Session：Student → Central

确定不再使用该 Worker 时调用：

```text
DELETE /sessions/{session_id}
```

```bash
curl -X DELETE \
  http://127.0.0.1:8000/sessions/sess_xxxxx
```

正常 Release 返回 HTTP `200`。典型响应：

```json
{
  "session_id": "sess_xxxxx",
  "student_id": "student01",
  "artifact_id": "art_xxxxx",
  "status": "closed",
  "worker": "mock-kv2602",
  "request_count": 3,
  "error": null
}
```

Release 后不能继续 predict。关闭 curl、关闭浏览器或一次 HTTP 连接结束都不等于 Release；显式 `DELETE` 才是正常结束方式。重复 release 已关闭的 Session 会返回其当前 `closed` 状态；不存在的 Session 返回 `404`。

### 4.8 查看 Worker：Admin → Central

```text
GET /workers
```

```bash
curl http://127.0.0.1:8000/workers
```

成功时返回 HTTP `200`。

格式化输出，有 jq 时：

```bash
curl -s http://127.0.0.1:8000/workers | jq
```

没有 jq 时：

```bash
curl -s http://127.0.0.1:8000/workers | python3 -m json.tool
```

响应是数组，每项字段为：

```text
board
state
session_id
artifact_id
fpga_ready
last_seen
last_error
```

示例：

```json
[
  {
    "board": "mock-kv2601",
    "state": "idle",
    "session_id": null,
    "artifact_id": null,
    "fpga_ready": false,
    "last_seen": "2026-08-26T10:00:00+00:00",
    "last_error": null
  }
]
```

管理员可用它确认实际启动的 Mock 或真实 Worker 是否已经变为 `idle`。

### 4.9 查看 Central 健康状态：Admin → Central

```text
GET /health
```

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

成功时返回 HTTP `200`。典型格式：

```json
{
  "ok": true,
  "workers": {
    "total": 20,
    "idle": 3,
    "offline": 17
  },
  "sessions": {}
}
```

`workers` 和 `sessions` 中只出现当前数据库实际存在的状态计数，具体字段和数字会随运行状态变化，不能写死。

### 4.10 Central → Worker 内部接口

以下接口不是学生正常调用的。调用者是 Central Server，接收者是 KV260 Worker：

| 接口 | 谁发送 | 谁接收 | 用途 |
| --- | --- | --- | --- |
| `GET /health` | Central | Worker | 检查 Worker HTTP 服务是否存活 |
| `GET /status` | Central | Worker | 查询当前 Session、Artifact 和 FPGA ready 状态 |
| `POST /internal/deploy` | Central | Worker | 向选中 Worker 部署 Session 对应的 bit/hwh |
| `POST /predict` | Central | Worker | 执行当前 Session 的一次计算 |
| `POST /internal/release` | Central | Worker | 解除当前 Session ownership |

当前内部请求 contract：

- `/internal/deploy` 使用 `multipart/form-data`，包含 `session_id`、`artifact_id`、`bit_sha256`、`hwh_sha256`、`bit` 和 `hwh`。
- `/predict` 使用 JSON：`{"session_id":"...","payload":{...}}`。
- `/internal/release` 使用 JSON：`{"session_id":"..."}`。

WorkerClient 会检查 deploy 响应中的 `ok`、`fpga_ready`、`session_id` 和 `artifact_id`。当前 Mock Worker实现了这些接口；真实 PYNQ Worker 尚未实现。系统关系见 Framework，Worker 内部 FPGA 原理见 Architecture Notes。

## 5. 学生完整使用示例

下面使用手动复制 ID 的方式，不要求安装 jq。首先设置 Central URL：

```bash
CENTRAL=http://127.0.0.1:8000
```

远程使用时改为：

```bash
CENTRAL=http://<CENTRAL_IP>:8000
```

### Step 1：上传 Artifact

```bash
curl -X POST "$CENTRAL/fpga/artifacts" \
  -F student_id=student01 \
  -F bit=@design.bit \
  -F hwh=@design.hwh
```

从响应中复制 `artifact_id`，然后设置：

```bash
ARTIFACT_ID=art_xxxxx
```

### Step 2：创建 Session

```bash
curl -X POST "$CENTRAL/sessions" \
  -H 'Content-Type: application/json' \
  -d "{\"student_id\":\"student01\",\"artifact_id\":\"$ARTIFACT_ID\"}"
```

从响应中复制 `session_id`：

```bash
SESSION_ID=sess_xxxxx
```

### Step 3：等待 Session ready

如果创建响应的 `status` 是 `queued`，周期查询：

```bash
curl -s "$CENTRAL/sessions/$SESSION_ID" | python3 -m json.tool
```

看到 `"status": "ready"` 后继续。

### Step 4：连续执行 predict

```bash
curl -X POST "$CENTRAL/sessions/$SESSION_ID/predict" \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"value":1}}'

curl -X POST "$CENTRAL/sessions/$SESSION_ID/predict" \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"value":2}}'

curl -X POST "$CENTRAL/sessions/$SESSION_ID/predict" \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"value":3}}'
```

三次请求使用同一个 `SESSION_ID`。

### Step 5：主动 Release

```bash
curl -X DELETE "$CENTRAL/sessions/$SESSION_ID"
```

确认响应的 `status` 为 `closed`。

## 6. Mock Cluster

### 6.1 Mock Cluster 是什么

Mock Cluster 是一组模拟 KV260 Worker 的本地 HTTP 服务。例如：

```text
mock-kv2601 → 127.0.0.1:18081
mock-kv2602 → 127.0.0.1:18082
mock-kv2603 → 127.0.0.1:18083
```

它们实现 `/health`、`/status`、`/internal/deploy`、`/predict` 和 `/internal/release`，但不 import PYNQ、不加载真实 Overlay、不访问 FPGA，也不执行 DMA/MMIO。

Mock Cluster 用于在真实 KV260 Worker 尚未完成时测试 Central Server 的 Artifact、Session、Queue、固定路由、Release 和 HTTP 流程。

`workers.mock.json` 只是地址配置，不会启动 Worker。真正启动进程的是：

```bash
python -m testbed.run_mock_cluster
```

### 6.2 推荐启动顺序

```text
1. 启动 Mock Cluster
2. 启动使用 workers.mock.json 的 Central Server
3. 查看 /workers
4. 运行 Smoke Test
```

Central 启动时会立即对配置中的 Worker 执行 `/health` 和 `/status` recovery，因此推荐先启动 Mock Worker。

如果已经先启动 Central 也不是错误。只要 Central 使用 `config/workers.mock.json`，之后启动 Mock Cluster 后，后台 Health Monitor 会按 `HEALTH_INTERVAL_SECONDS` 周期重新检查；请求成功后，Mock Worker 会重新被识别为可用 Worker。

### 6.3 启动 Mock Cluster

终端 1：

```bash
cd <repo>/server
source .venv/bin/activate

python -m testbed.run_mock_cluster --workers 3
```

默认 `--base-port` 是 `18081`，因此三个 Worker 是：

```text
mock-kv2601 → http://127.0.0.1:18081
mock-kv2602 → http://127.0.0.1:18082
mock-kv2603 → http://127.0.0.1:18083
```

模拟 20 台：

```bash
python -m testbed.run_mock_cluster --workers 20
```

这会占用 `18081` 至 `18100`。`--workers` 只接受 1～20；按 `Ctrl+C` 时 launcher 会结束其子进程。

### 6.4 启动使用 Mock 配置的 Central

终端 2：

```bash
cd <repo>/server
source .venv/bin/activate

WORKERS_CONFIG=config/workers.mock.json \
DATABASE_URL=sqlite:///data/smoke.db \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如果当前已经位于 `<repo>/server`，不要再次执行 `cd server`。

### 6.5 查看 Mock Worker 是否可用

终端 3：

```bash
curl -s http://127.0.0.1:8000/workers | python3 -m json.tool
```

实际启动的 Mock Worker 应逐渐显示为 `idle`。`workers.mock.json` 配置了 20 个地址，但只运行 `--workers 3` 时，前三个会变为 `idle`，其余通常保持 `offline`，这是正常现象。

### 6.6 运行 Smoke Test

终端 3：

```bash
cd <repo>/server
source .venv/bin/activate

python -m testbed.smoke_test
```

Smoke Test 会通过真实 HTTP 调用验证 Artifact upload、Session allocation、multiple predict、fixed Worker、deploy once、Release、Queue 和 concurrency。成功结束时输出：

```text
ALL TESTS PASSED
```

## 7. 自动化测试

```bash
cd <repo>/server
source .venv/bin/activate

pytest -v
```

区别：

```text
pytest
= 自动执行单元/服务级测试代码

Mock Cluster + smoke_test
= 运行完整 Central / Worker HTTP 集成流程
```

## 8. 常用配置

环境变量由 `server/app/config.py` 读取：

| 变量 | 默认值 | 实际用途 |
| --- | --- | --- |
| `SERVER_HOST` | `127.0.0.1` | Central host 设置；本文启动命令中的 Uvicorn `--host` 参数直接决定监听地址 |
| `SERVER_PORT` | `8000` | Central port 设置；本文启动命令中的 Uvicorn `--port` 参数直接决定监听端口 |
| `DATABASE_URL` | `<repo>/server/data/central.db` 对应的绝对 SQLite URL | Artifact、Worker 和 Session metadata 数据库 |
| `ARTIFACT_ROOT` | `<repo>/server/data/artifacts` | Artifact 文件存储目录 |
| `WORKERS_CONFIG` | `<repo>/server/config/workers.json` | Worker 地址配置 |
| `WORKER_CONNECT_TIMEOUT` | `2.0` 秒 | 连接 Worker 超时 |
| `WORKER_REQUEST_TIMEOUT` | `30.0` 秒 | 普通 Worker HTTP 请求超时 |
| `WORKER_DEPLOY_TIMEOUT` | `120.0` 秒 | `/internal/deploy` 超时 |
| `HEALTH_INTERVAL_SECONDS` | `5.0` 秒 | 后台 Worker 健康检查周期 |
| `HEALTH_FAILURE_THRESHOLD` | `3` | 正常监控中连续失败达到该值后标记 `offline` |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `0` | V1 预留配置；当前尚未实现自动 TTL 回收 |
| `MAX_BIT_SIZE` | `134217728` 字节 | bit 上传上限（128 MiB） |
| `MAX_HWH_SIZE` | `16777216` 字节 | hwh 上传上限（16 MiB） |

相对的 `ARTIFACT_ROOT` 和 `WORKERS_CONFIG` 会相对于 `server/` 解析。`DATABASE_URL=sqlite:///data/smoke.db` 在从 `server/` 启动时使用 `server/data/smoke.db`。

## 9. 常见使用问题

### 9.1 已经在 `server/`，为什么 `cd server` 报错？

如果提示符已经类似：

```text
~/kv260/server$
```

就直接执行 venv、Uvicorn 或测试命令，不要再次 `cd server`。

### 9.2 Uvicorn 出现什么表示启动成功？

```text
Application startup complete.
Uvicorn running on http://...
```

然后使用 `/health` 和 `/docs` 验证 HTTP 服务。

### 9.3 为什么 `/workers` 里的 Mock Worker 是 `offline`？

依次检查：

1. 是否运行了 `python -m testbed.run_mock_cluster`；
2. Central 是否设置了 `WORKERS_CONFIG=config/workers.mock.json`；
3. Mock 端口是否与配置一致；
4. 是否等待了下一次 Health Monitor 检查。

只启动 3 个 Mock Worker 时，其余 17 个配置地址保持 `offline` 是正常状态。

### 9.4 为什么另一台电脑访问不了 `127.0.0.1`？

`127.0.0.1` 只表示请求发起设备自身。远程客户端应访问：

```text
http://<CENTRAL_IP>:8000
```

并使用以下方式启动 Central：

```text
--host 0.0.0.0
```

同时确认主机防火墙和网络允许访问 `8000` 端口。

### 9.5 Mock Cluster 是不是 Central Server？

不是。Mock Cluster 模拟 Worker；Central Server 是 Scheduler / API Server。

```text
本地测试：Student → Central → Mock Worker
真实系统：Student → Central → KV260 Worker
```

### 9.6 为什么 Session 是 `queued`，不能 predict？

当前没有可分配 Worker。使用 `GET /workers` 检查 Worker 状态，并通过 `GET /sessions/{session_id}` 等待该 Session 变为 `ready`。不要在 `queued` 状态调用 predict。
