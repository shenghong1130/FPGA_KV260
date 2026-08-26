# KV260 Central Server 使用说明

## 1. 文档用途

本文只介绍 Central Server 的安装、启动、Sessionless HTTP API、Mock Cluster 和测试方法。总体架构见 [KV260_PYNQ_Framework.md](KV260_PYNQ_Framework.md)，PYNQ/FPGA 原理见 [KV260_PYNQ_Architecture_Notes.md](KV260_PYNQ_Architecture_Notes.md)，板卡部署见 [KV260_SD_Card_Setup_Guide.md](KV260_SD_Card_Setup_Guide.md)。

Central Server V1 与 Mock Worker 已实现；真实 KV260 PYNQ 业务 Worker仍是下一阶段。V1 依靠 asyncio lock 保证调度原子性，只能使用单 Uvicorn process。

## 2. 安装开发环境

需要 Python 3.12：

```bash
cd <repo>/server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

只运行服务可安装 `requirements.txt`。若提示符已经在 `<repo>/server`，不要再次 `cd server`。

## 3. 启动 Central Server

### 3.1 使用真实 KV260 Worker

默认读取 `config/workers.json`：

```bash
cd <repo>/server
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

远程客户端使用 `http://<CENTRAL_IP>:8000`，不能使用指向客户端自身的 `127.0.0.1`。禁止添加 `--workers 4`。

### 3.2 使用 Mock Worker

```bash
WORKERS_CONFIG=config/workers.mock.json \
DATABASE_URL=sqlite:///data/smoke.db \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`workers.mock.json` 只配置地址，不启动进程；Mock Cluster 需按第 6 节单独启动。

### 3.3 确认服务器启动成功

看到 `Application startup complete.` 和 `Uvicorn running on ...` 后检查：

```bash
curl http://127.0.0.1:8000/health
```

交互式 API 页面：`http://127.0.0.1:8000/docs`。

Admin Dashboard：`http://127.0.0.1:8000/ui/`。局域网远程访问时，将
`127.0.0.1` 替换为 Central Server 的实际 IP。

## 4. HTTP API 使用

### 4.1 API 使用关系

```text
Student / Client → Central Server :8000 → KV260 Worker :8080
Admin            → Central Server :8000
```

Student 不直接访问 Worker，不创建 Session、不保存 `session_id/lease_id`、不主动 release。

### 4.2 学生上传 Artifact：Student → Central

```text
POST /fpga/artifacts
Content-Type: multipart/form-data
```

字段只有 `student_id`、`bit`、`hwh`：

```bash
curl -X POST http://127.0.0.1:8000/fpga/artifacts \
  -F student_id=student01 \
  -F bit=@design.bit \
  -F hwh=@design.hwh
```

Central 按学生自动生成版本：同一学生依次为 `v1/v2/v3`，不同学生各自从 `v1` 开始。典型 `201` 响应：

```json
{
  "artifact_id": "art_xxxxx",
  "student_id": "student01",
  "version": "v1",
  "status": "ready",
  "bit_sha256": "...",
  "hwh_sha256": "...",
  "bit_size": 123456,
  "hwh_size": 12345,
  "created_at": "..."
}
```

### 4.3 查询 Artifact：Student / Admin → Central

```bash
curl http://127.0.0.1:8000/fpga/artifacts
curl http://127.0.0.1:8000/fpga/artifacts/art_xxxxx
```

只返回 metadata，不返回 bit/hwh 内容；不存在返回 `404`。

### 4.4 提交计算：Student → Central

```bash
curl -X POST http://127.0.0.1:8000/predict \
  -H 'Content-Type: application/json' \
  -d '{
    "student_id": "student01",
    "payload": {"value": 123}
  }'
```

Central 自动选择该学生提交时的最新 Artifact，并生成 `request_id`。有可用资源并完成计算时返回 `200`：

```json
{
  "request_id": "req_xxxxx",
  "student_id": "student01",
  "status": "completed",
  "artifact_id": "art_xxxxx",
  "version": "v3",
  "result": {"value": 456},
  "error": null
}
```

无可用 Worker 时返回 `202`：

```json
{
  "request_id": "req_xxxxx",
  "student_id": "student01",
  "status": "queued",
  "artifact_id": "art_xxxxx",
  "version": "v3",
  "result": null,
  "error": null
}
```

学生响应不暴露具体 Worker 或内部 `lease_id`。没有可用 Artifact 返回 `404`。

### 4.5 查询 Request：Student → Central

收到 `202` 后保存 `request_id`：

```bash
curl http://127.0.0.1:8000/requests/req_xxxxx
```

状态可能为 `queued/running/completed/failed`。Central 分配 Worker 后会自动执行，Student 不需要重新 POST。

### 4.6 查询学生状态：Student / Admin → Central

```bash
curl http://127.0.0.1:8000/students/student01/status
```

```json
{
  "student_id": "student01",
  "latest_artifact_id": "art_xxxxx",
  "latest_version": "v4",
  "lease_state": "ready",
  "worker_assigned": true,
  "queued_requests": 0,
  "running_requests": 0,
  "last_activity_at": "..."
}
```

不会返回具体 Worker。

### 4.7 查看 Worker：Admin → Central

```bash
curl -s http://127.0.0.1:8000/workers | python3 -m json.tool
```

Admin 响应包含 `board/state/lease_id/student_id/artifact_id/fpga_ready/last_seen/last_error`。

### 4.8 查看 Central 健康状态：Admin → Central

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

响应统计 `workers`、`leases` 和 `requests`，具体数字取决于实时数据库状态。

### 4.9 Central → Worker 内部接口

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 检查 Worker 是否存活 |
| `GET /status` | 查询 `lease_id/artifact_id/fpga_ready` |
| `POST /internal/deploy` | 下发 Lease 对应 bit/hwh |
| `POST /predict` | 执行一次计算 |
| `POST /internal/release` | 解除 Lease ownership |

这些接口只由 Central 调用。Mock 实现该 contract，但不执行 PYNQ、Overlay、DMA 或 MMIO。

## 5. 学生完整使用示例

```bash
CENTRAL=http://127.0.0.1:8000

curl -X POST "$CENTRAL/fpga/artifacts" \
  -F student_id=student01 \
  -F bit=@design.bit \
  -F hwh=@design.hwh

curl -X POST "$CENTRAL/predict" \
  -H 'Content-Type: application/json' \
  -d '{"student_id":"student01","payload":{"value":123}}'
```

若响应是 `completed`，直接读取 `result`；若为 `queued`，复制 `request_id` 并查询：

```bash
curl "$CENTRAL/requests/req_xxxxx"
```

完整流程只有 upload → predict → 必要时查询 request。没有 Session 创建或 Release。

## 6. Mock Cluster

### 6.1 Mock Cluster 是什么

它是一组模拟 KV260 Worker contract 的本地 HTTP 服务，不 import PYNQ、不访问 FPGA。

### 6.2 推荐启动顺序

```text
1. Mock Cluster
2. 使用 workers.mock.json 的 Central
3. GET /workers
4. Smoke Test
```

Central 先启动也可以，Health Monitor 会在 Mock 上线后重新发现它。

### 6.3 启动 Mock Cluster

```bash
cd <repo>/server
source .venv/bin/activate
python -m testbed.run_mock_cluster --workers 3
```

默认地址为 `mock-kv2601:18081`、`mock-kv2602:18082`、`mock-kv2603:18083`。模拟 20 台使用 `--workers 20`。

### 6.4 启动测试用 Central

```bash
WORKERS_CONFIG=config/workers.mock.json \
DATABASE_URL=sqlite:///data/smoke.db \
LEASE_RECLAIM_GRACE_SECONDS=1 \
LEASE_IDLE_TIMEOUT_SECONDS=3 \
LEASE_REAPER_INTERVAL_SECONDS=0.2 \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

短 timeout 仅用于 Smoke Test。生产默认分别为 300、1800、10 秒。

### 6.5 查看 Mock Worker 是否可用

```bash
curl -s http://127.0.0.1:8000/workers | python3 -m json.tool
```

只启动 3 个 Mock 时前三个应为 `idle`，配置中的其余地址为 `offline` 属正常现象。

### 6.6 运行 Smoke Test

```bash
python -m testbed.smoke_test
```

它验证 Artifact、Lazy Allocation、固定 Worker、deploy once、202 Queue、LRU reclaim、自动执行和 Session API 移除。

## 7. 自动化测试

```bash
cd <repo>/server
source .venv/bin/activate
python -m compileall app testbed tests
pytest -v
```

pytest 是自动化服务测试；Mock Cluster + smoke_test 是完整 HTTP 集成测试。

## 8. 常用配置

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SERVER_HOST` | `127.0.0.1` | 服务地址配置 |
| `SERVER_PORT` | `8000` | 服务端口配置 |
| `DATABASE_URL` | `server/data/central.db` | SQLite |
| `ARTIFACT_ROOT` | `server/data/artifacts` | Artifact Store |
| `WORKERS_CONFIG` | `server/config/workers.json` | Worker 地址 |
| `WORKER_CONNECT_TIMEOUT` | `2.0` | 连接超时 |
| `WORKER_REQUEST_TIMEOUT` | `30.0` | 普通请求超时 |
| `WORKER_DEPLOY_TIMEOUT` | `120.0` | deploy 超时 |
| `HEALTH_INTERVAL_SECONDS` | `5.0` | 健康检查周期 |
| `HEALTH_FAILURE_THRESHOLD` | `3` | offline 失败阈值 |
| `LEASE_IDLE_TIMEOUT_SECONDS` | `1800` | 正常空闲回收 |
| `LEASE_RECLAIM_GRACE_SECONDS` | `300` | 资源紧张回收门槛 |
| `LEASE_REAPER_INTERVAL_SECONDS` | `10` | Reaper 周期 |
| `MAX_BIT_SIZE` | `134217728` | bit 上限 |
| `MAX_HWH_SIZE` | `16777216` | hwh 上限 |

未设置新 idle 变量时，代码临时读取旧 `SESSION_IDLE_TIMEOUT_SECONDS` 作为兼容 fallback；新部署只使用 `LEASE_IDLE_TIMEOUT_SECONDS`。

## 9. 常见使用问题

### 9.1 为什么没有 Session API？

Lease 完全由 Central 管理，旧 `/sessions` 返回 `404`。

### 9.2 没有 Worker 怎么办？

`POST /predict` 返回 `202 queued`，使用 `GET /requests/{request_id}` 等待结果。

### 9.3 学生忘记 Release 怎么办？

学生不需要 Release。Central 使用 idle timeout 和 pressure reclaim 自动回收。

### 9.4 为什么同一学生下一次可能换 Worker？

Lease 因 idle timeout、LRU reclaim 或故障解除后，下一次 predict 会自动分配新 Worker。

### 9.5 为什么 Mock Worker 是 `offline`？

检查 Mock Cluster 是否启动、Central 是否使用 `workers.mock.json`、端口是否一致，并等待 Health Monitor。

### 9.6 为什么远程访问不了 `127.0.0.1`？

远程使用 `http://<CENTRAL_IP>:8000`，Central 使用 `--host 0.0.0.0`。
