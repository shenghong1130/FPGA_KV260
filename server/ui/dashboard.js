"use strict";

// Config and state
const STORAGE = { api: "kv260.centralApi", theme: "kv260.theme", view: "kv260.view" };
const DEFAULT_API = location.protocol === "file:" ? "http://127.0.0.1:8000" : location.origin;
const state = {
  apiBase: normalizeBase(localStorage.getItem(STORAGE.api) || DEFAULT_API),
  health: null, workers: [], artifacts: [], lastSuccess: null,
  view: "overview", workerState: "all", artifactLimit: 100,
  studentId: "", requestId: "", polling: null,
};
const statusText = {
  idle: "空闲", reserved: "已预留", deploying: "正在部署", ready: "已分配",
  busy: "正在计算", error: "错误", offline: "离线", queued: "排队中",
  running: "运行中", completed: "已完成", failed: "失败", unassigned: "未分配",
  releasing: "正在释放", lost: "Worker 丢失",
};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function normalizeBase(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}
function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.title) element.title = options.title;
  if (options.type) element.type = options.type;
  if (options.dataset) Object.assign(element.dataset, options.dataset);
  for (const child of children) element.append(child);
  return element;
}
function clear(element) { element.replaceChildren(); }
function safeCount(group, key) { return Number(group?.[key] || 0); }
function stateKey(value) { return String(value || "unknown").toLowerCase(); }
function stateLabel(value) { const key = stateKey(value); return `${key.toUpperCase()} / ${statusText[key] || "未知"}`; }
function naturalCompare(a, b) { return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" }); }
function versionNumber(value) { const match = /^v(\d+)$/i.exec(String(value || "")); return match ? Number(match[1]) : -1; }
function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = bytes, index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${index ? size.toFixed(size >= 10 ? 1 : 2) : size.toFixed(0)} ${units[index]}`;
}
function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
function relativeTime(value) {
  if (!value) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 5) return "刚刚 / Just now";
  if (seconds < 60) return `${seconds} 秒前 / ${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前 / ${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)} 小时前 / ${Math.floor(seconds / 3600)}h ago`;
}
function shortHash(value) {
  const text = String(value || "");
  return text.length > 18 ? `${text.slice(0, 9)}…${text.slice(-5)}` : text || "—";
}

// API client
async function apiFetch(path, options = {}) {
  const controller = new AbortController();
  const timeout = options.timeout ?? 5000;
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(`${state.apiBase}${path}`, { ...options, signal: controller.signal });
    const text = await response.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch { data = text; } }
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.detail : data;
      const error = new Error(detail || `${response.status} ${response.statusText}`);
      error.status = response.status; error.data = data; throw error;
    }
    return { data, status: response.status };
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`请求超时（${timeout / 1000}s）`);
    if (error instanceof TypeError) {
      throw new Error("无法连接 Central API。可能是网络、地址或浏览器 CORS 限制；请使用同源环境或允许访问该 API 的静态服务器。");
    }
    throw error;
  } finally { clearTimeout(timer); }
}

// Toast and clipboard
function toast(message, type = "info") {
  const item = node("div", { className: `toast ${type}`, text: message });
  $("#toast-region").append(item);
  setTimeout(() => item.remove(), type === "error" ? 7500 : 3000);
}
async function copyText(value) {
  try {
    await navigator.clipboard.writeText(String(value));
  } catch {
    const input = node("textarea"); input.value = String(value); document.body.append(input);
    input.select(); document.execCommand("copy"); input.remove();
  }
  toast("已复制 / Copied", "success");
}
function copyButton(value, label = "复制") {
  const button = node("button", { className: "copy-button", text: label, type: "button" });
  button.setAttribute("aria-label", `${label} ${value}`);
  button.addEventListener("click", () => copyText(value));
  return button;
}

// Theme and routing
function applyTheme(theme) {
  const selected = theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = selected;
  $("#theme-button").textContent = selected === "dark" ? "☀" : "◐";
}
function currentView() {
  const value = location.hash.replace(/^#\//, "") || localStorage.getItem(STORAGE.view) || "overview";
  return ["overview", "workers", "artifacts", "requests", "student", "tools"].includes(value) ? value : "overview";
}
function route() {
  state.view = currentView(); localStorage.setItem(STORAGE.view, state.view);
  $$(".view").forEach((view) => view.classList.toggle("active", view.dataset.view === state.view));
  $$('[data-nav]').forEach((link) => link.classList.toggle("active", link.dataset.nav === state.view));
  refreshCurrent(true);
}

// Health and overview
async function loadHealth() {
  try {
    state.health = (await apiFetch("/health")).data;
    markOnline(); renderOverview();
  } catch (error) { markOffline(error.message); }
}
function markOnline() {
  state.lastSuccess = new Date();
  const workers = state.health?.workers || {};
  const degraded = safeCount(workers, "error") + safeCount(workers, "offline") > 0;
  const label = $("#platform-status"); label.className = `platform-status ${degraded ? "degraded" : "healthy"}`;
  label.replaceChildren(node("span", { className: "status-dot" }), document.createTextNode(degraded ? "Degraded" : "Healthy"));
  $("#last-update").textContent = state.lastSuccess.toLocaleTimeString();
  $("#offline-banner").classList.add("hidden");
}
function markOffline(message) {
  const label = $("#platform-status"); label.className = "platform-status offline";
  label.replaceChildren(node("span", { className: "status-dot" }), document.createTextNode("Central Offline"));
  const banner = $("#offline-banner"); banner.classList.remove("hidden");
  banner.textContent = `Central 无法连接：${message}。保留最近一次成功数据${state.lastSuccess ? `（${state.lastSuccess.toLocaleTimeString()}）` : ""}。`;
}
function renderOverview() {
  const workers = state.health?.workers || {};
  const requests = state.health?.requests || {};
  const cards = [
    ["Worker 总数", "Total Workers", workers.total], ["空闲", "IDLE", workers.idle],
    ["已分配", "READY", workers.ready], ["正在计算", "BUSY", workers.busy],
    ["正在部署", "DEPLOYING", workers.deploying], ["离线", "OFFLINE", workers.offline],
    ["错误", "ERROR", workers.error], ["排队请求", "Queued Requests", requests.queued],
    ["运行请求", "Running Requests", requests.running], ["失败请求", "Failed Requests", requests.failed],
  ];
  const container = $("#summary-cards"); clear(container);
  for (const [label, secondary, count] of cards) {
    container.append(node("article", { className: "summary-card" }, [
      node("span", { className: "label", text: label }), node("strong", { text: Number(count || 0) }), node("small", { text: secondary }),
    ]));
  }
  const total = Number(workers.total || state.workers.length || 0);
  const used = ["ready", "busy", "deploying", "reserved"].reduce((sum, key) => sum + safeCount(workers, key), 0);
  const percent = total ? Math.min(100, used / total * 100) : 0;
  $("#utilization-label").textContent = `${used} / ${total} In Use`;
  $("#utilization-bar").style.width = `${percent}%`;
  $(".progress").setAttribute("aria-valuenow", String(Math.round(percent)));
  const issues = safeCount(workers, "error") + safeCount(workers, "offline");
  const alert = $("#worker-alert"); alert.classList.toggle("hidden", !issues);
  alert.textContent = issues ? `平台存在 ${issues} 个异常 Worker / Worker issues detected（点击查看）` : "";
}

// Workers
async function loadWorkers() {
  try {
    const data = (await apiFetch("/workers")).data;
    state.workers = Array.isArray(data) ? data.sort((a, b) => naturalCompare(a.board, b.board)) : [];
    renderWorkers();
  } catch (error) { toast(error.message, "error"); }
}
function workerCard(worker) {
  const key = stateKey(worker.state);
  const button = node("button", { className: `worker-card state-${key}`, type: "button" });
  const badge = node("span", { className: `state-badge status-${key}` }, [node("span", { text: key.toUpperCase() }), node("small", { text: statusText[key] || "未知" })]);
  button.append(node("div", { className: "worker-head" }, [node("strong", { text: worker.board || "—" }), badge]));
  const fields = node("dl", { className: "worker-fields" });
  const pairs = [["Student", worker.student_id], ["Artifact", worker.artifact_id], ["FPGA Ready", worker.fpga_ready ? "Yes" : "No"], ["Last Seen", relativeTime(worker.last_seen)], ["Error", worker.last_error]];
  for (const [label, value] of pairs) fields.append(node("dt", { text: label }), node("dd", { text: value || "—", title: value ? String(value) : "" }));
  button.append(fields); button.addEventListener("click", () => openWorker(worker)); return button;
}
function filteredWorkers() {
  const query = $("#worker-search").value.trim().toLowerCase();
  const filter = $("#worker-state-filter").value.toLowerCase();
  return state.workers.filter((worker) => {
    const workerState = stateKey(worker.state);
    const matchesState = filter === "all" || (filter === "issues" && ["error", "offline"].includes(workerState)) || workerState === filter;
    const haystack = [worker.board, worker.student_id, worker.artifact_id].join(" ").toLowerCase();
    return matchesState && (!query || haystack.includes(query));
  });
}
function renderWorkers() {
  renderWorkerGrid($("#overview-workers"), state.workers);
  renderWorkerGrid($("#worker-grid"), filteredWorkers());
}
function renderWorkerGrid(container, workers) {
  clear(container);
  if (!workers.length) { container.append(node("p", { className: "empty", text: "暂无 Worker / No workers registered" })); return; }
  workers.forEach((worker) => container.append(workerCard(worker)));
}
function openWorker(worker) {
  const content = $("#worker-dialog-content"); clear(content);
  const list = node("dl", { className: "detail-grid" });
  const rows = [["Board", worker.board], ["State", stateLabel(worker.state)], ["Student", worker.student_id], ["Lease ID", worker.lease_id], ["Artifact ID", worker.artifact_id], ["FPGA Ready", worker.fpga_ready ? "Yes" : "No"], ["Last Seen", formatTime(worker.last_seen)], ["Last Error", worker.last_error]];
  for (const [label, value] of rows) list.append(node("dt", { text: label }), node("dd", { text: value || "—", title: value ? String(value) : "" }));
  content.append(list); $("#worker-dialog").showModal();
}

// Artifacts
async function loadArtifacts() {
  try {
    const data = (await apiFetch("/fpga/artifacts")).data;
    state.artifacts = Array.isArray(data) ? data.sort((a, b) => new Date(b.created_at) - new Date(a.created_at)) : [];
    renderArtifacts(); renderRecentArtifacts();
  } catch (error) { toast(error.message, "error"); }
}
function latestVersions() {
  const result = new Map();
  for (const item of state.artifacts) result.set(item.student_id, Math.max(result.get(item.student_id) ?? -1, versionNumber(item.version)));
  return result;
}
function filteredArtifacts() {
  const student = $("#artifact-student-filter").value.trim().toLowerCase();
  const artifactId = $("#artifact-id-filter").value.trim().toLowerCase();
  const status = $("#artifact-status-filter").value.toLowerCase();
  const latest = latestVersions();
  return state.artifacts.filter((item) => (!student || String(item.student_id).toLowerCase().includes(student)) && (!artifactId || String(item.artifact_id).toLowerCase().includes(artifactId)) && (status === "all" || stateKey(item.status) === status) && (!$("#artifact-latest-only").checked || versionNumber(item.version) === latest.get(item.student_id)));
}
function renderArtifacts() {
  const body = $("#artifact-table"); clear(body);
  const all = filteredArtifacts(), shown = all.slice(0, state.artifactLimit), latest = latestVersions();
  if (!shown.length) { const row = node("tr"); const cell = node("td", { className: "empty", text: "暂无 Artifact / No artifacts" }); cell.colSpan = 8; row.append(cell); body.append(row); }
  for (const item of shown) {
    const row = node("tr");
    const version = node("td", { text: item.version || "—" });
    if (versionNumber(item.version) === latest.get(item.student_id)) version.append(node("span", { className: "latest", text: "Latest" }));
    const idCell = node("td", { className: "mono", text: shortHash(item.artifact_id), title: item.artifact_id }); idCell.append(copyButton(item.artifact_id, "复制"));
    const sha = node("td", { className: "mono" }); sha.append(node("span", { text: `BIT ${shortHash(item.bit_sha256)} · HWH ${shortHash(item.hwh_sha256)}` }), copyButton(item.bit_sha256, "BIT"), copyButton(item.hwh_sha256, "HWH"));
    [node("td", { text: item.student_id || "—" }), version, idCell, node("td", { className: `status-${stateKey(item.status)}`, text: stateLabel(item.status) }), node("td", { text: formatBytes(item.bit_size) }), node("td", { text: formatBytes(item.hwh_size) }), node("td", { text: formatTime(item.created_at), title: item.created_at }), sha].forEach((cell) => row.append(cell));
    body.append(row);
  }
  $("#artifact-show-more").classList.toggle("hidden", shown.length >= all.length);
}
function renderRecentArtifacts() {
  const body = $("#recent-artifacts"); clear(body);
  const recent = state.artifacts.slice(0, 5);
  if (!recent.length) { const row = node("tr"); const cell = node("td", { className: "empty", text: "暂无 Artifact" }); cell.colSpan = 4; row.append(cell); body.append(row); return; }
  for (const item of recent) { const row = node("tr"); [item.student_id, item.version, stateLabel(item.status), formatTime(item.created_at)].forEach((value) => row.append(node("td", { text: value }))); body.append(row); }
}
async function uploadArtifact(form) {
  const result = $(".form-result", form.parentElement); clear(result); result.className = "form-result";
  const student = form.elements.student_id.value.trim(), bit = form.elements.bit.files[0], hwh = form.elements.hwh.files[0];
  if (!student || !bit || !hwh) { showFormError(result, "请填写 Student ID 并选择 bit/hwh 文件。"); return; }
  if (!bit.name.toLowerCase().endsWith(".bit") || !hwh.name.toLowerCase().endsWith(".hwh")) { showFormError(result, "文件扩展名必须是 .bit 和 .hwh。"); return; }
  const button = $("button[type=submit]", form), original = button.textContent; button.disabled = true; button.textContent = "Uploading...";
  const data = new FormData(); data.append("student_id", student); data.append("bit", bit); data.append("hwh", hwh);
  try {
    const artifact = (await apiFetch("/fpga/artifacts", { method: "POST", body: data, timeout: 120000 })).data;
    result.className = "form-result success"; result.textContent = `上传成功 / Upload Successful：${artifact.student_id} ${artifact.version} ${artifact.artifact_id} · BIT ${formatBytes(artifact.bit_size)} · HWH ${formatBytes(artifact.hwh_size)}`;
    toast("Artifact 上传成功", "success"); form.reset(); await loadArtifacts();
  } catch (error) { showFormError(result, `上传失败${error.status ? ` HTTP ${error.status}` : ""}：${error.message}`); toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = original; }
}
function showFormError(element, message) { element.className = "form-result error"; element.textContent = message; }

// Request and student detail
function renderDetail(container, rows, jsonValue = undefined) {
  clear(container); container.className = "detail-output";
  const list = node("dl", { className: "detail-grid" });
  for (const [label, value] of rows) list.append(node("dt", { text: label }), node("dd", { text: value ?? "—" }));
  container.append(list);
  if (jsonValue !== undefined) container.append(node("pre", { text: JSON.stringify(jsonValue, null, 2) }));
}
async function queryRequest(requestId, target = $("#request-detail")) {
  try {
    const item = (await apiFetch(`/requests/${encodeURIComponent(requestId)}`)).data;
    renderDetail(target, [["Request ID", item.request_id], ["Student", item.student_id], ["Artifact", item.artifact_id], ["Version", item.version], ["Status", stateLabel(item.status)], ["Error", item.error]], item.result);
    return item;
  } catch (error) { target.className = "detail-output empty"; target.textContent = error.status === 404 ? "未找到 Request / Request not found" : error.message; throw error; }
}
async function queryStudent(studentId) {
  const target = $("#student-detail");
  try {
    const item = (await apiFetch(`/students/${encodeURIComponent(studentId)}/status`)).data;
    renderDetail(target, [["Student ID", item.student_id], ["Latest Artifact", item.latest_artifact_id], ["Latest Version", item.latest_version], ["Lease State", stateLabel(item.lease_state)], ["Worker Assigned", item.worker_assigned ? "Yes" : "No"], ["Queued Requests", item.queued_requests], ["Running Requests", item.running_requests], ["Last Activity", formatTime(item.last_activity_at)]]);
  } catch (error) { target.className = "detail-output empty"; target.textContent = error.message; }
}

// Predict tester
async function submitPredict(event) {
  event.preventDefault(); stopPolling();
  const student = $("#predict-student").value.trim(), raw = $("#predict-payload").value;
  let payload;
  try { payload = JSON.parse(raw); } catch { toast("Payload 不是合法 JSON / Invalid JSON payload", "error"); return; }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) { toast("Payload 顶层必须是 JSON object", "error"); return; }
  if (!student) { toast("Student ID 不能为空", "error"); return; }
  const button = $("#predict-submit"), target = $("#predict-result"); button.disabled = true;
  try {
    const response = await apiFetch("/predict", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ student_id: student, payload }), timeout: 60000 });
    const item = response.data;
    renderDetail(target, [["Request ID", item.request_id], ["Student", item.student_id], ["Artifact", item.artifact_id], ["Version", item.version], ["Status", stateLabel(item.status)], ["Error", item.error]], item.result);
    if (response.status === 202 || stateKey(item.status) === "queued") { toast("请求已排队 / Queued", "warning"); startPolling(item.request_id); } else toast("请求完成 / Completed", "success");
  } catch (error) { target.className = "detail-output empty"; target.textContent = error.message; toast(error.message, "error"); }
  finally { button.disabled = false; }
}
function startPolling(requestId) {
  stopPolling(); let attempts = 0; $("#stop-polling").classList.remove("hidden");
  state.polling = setInterval(async () => {
    attempts += 1;
    try {
      const item = await queryRequest(requestId, $("#predict-result"));
      if (["completed", "failed"].includes(stateKey(item.status))) { stopPolling(); toast(item.status === "completed" ? "请求完成" : "请求失败", item.status === "completed" ? "success" : "error"); }
    } catch { /* The detail panel already contains the error. */ }
    if (attempts >= 150) { stopPolling(); toast("自动查询已停止，请手动查询 Request ID。", "warning"); }
  }, 2000);
}
function stopPolling() { if (state.polling) clearInterval(state.polling); state.polling = null; $("#stop-polling").classList.add("hidden"); }

// Unified refresh manager
let refreshTimer = null;
async function refreshCurrent(force = false) {
  clearTimeout(refreshTimer);
  const tasks = [];
  if (state.view === "overview") tasks.push(loadHealth(), loadWorkers(), loadArtifacts());
  else if (state.view === "workers") tasks.push(loadHealth(), loadWorkers());
  else if (state.view === "artifacts") tasks.push(loadArtifacts());
  else if (state.view === "student" && state.studentId) tasks.push(queryStudent(state.studentId));
  if (force) await Promise.allSettled(tasks);
  const visible = document.visibilityState === "visible";
  const normal = state.view === "artifacts" ? 10000 : state.view === "student" ? 5000 : 2000;
  refreshTimer = setTimeout(() => refreshCurrent(true), visible ? normal : 15000);
}

// Event wiring and initialization
function setApiBase(value) {
  const normalized = normalizeBase(value);
  try { new URL(normalized); } catch { toast("Central API URL 无效", "error"); return; }
  state.apiBase = normalized; localStorage.setItem(STORAGE.api, normalized);
  $("#api-base").value = normalized; $("#footer-api").textContent = normalized;
  $("#api-docs").href = `${normalized}/docs`; state.health = null; state.workers = []; state.artifacts = [];
  toast("Central API 地址已保存", "success"); refreshCurrent(true);
}
function init() {
  applyTheme(localStorage.getItem(STORAGE.theme));
  $("#api-base").value = state.apiBase; $("#footer-api").textContent = state.apiBase; $("#api-docs").href = `${state.apiBase}/docs`;
  if (location.protocol === "file:" && !localStorage.getItem(STORAGE.api)) toast("当前页面以本地文件打开，请确认 Central API 地址。", "warning");
  $("#save-api").addEventListener("click", () => setApiBase($("#api-base").value));
  $("#api-base").addEventListener("keydown", (event) => { if (event.key === "Enter") setApiBase(event.target.value); });
  $("#theme-button").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; localStorage.setItem(STORAGE.theme, theme); applyTheme(theme); });
  $("#refresh-button").addEventListener("click", () => refreshCurrent(true));
  addEventListener("hashchange", route); document.addEventListener("visibilitychange", () => refreshCurrent(true));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => { location.hash = `#/${button.dataset.go}`; }));
  $("#worker-alert").addEventListener("click", () => { $("#worker-state-filter").value = "issues"; location.hash = "#/workers"; renderWorkers(); });
  $("#worker-search").addEventListener("input", renderWorkers); $("#worker-state-filter").addEventListener("change", renderWorkers);
  ["#artifact-student-filter", "#artifact-id-filter"].forEach((id) => $(id).addEventListener("input", renderArtifacts));
  $("#artifact-status-filter").addEventListener("change", renderArtifacts); $("#artifact-latest-only").addEventListener("change", renderArtifacts);
  $("#artifact-show-more").addEventListener("click", () => { state.artifactLimit += 100; renderArtifacts(); });
  $$('[data-upload-form]').forEach((form) => form.addEventListener("submit", (event) => { event.preventDefault(); uploadArtifact(form); }));
  $("#request-query-form").addEventListener("submit", (event) => { event.preventDefault(); state.requestId = $("#request-id-input").value.trim(); if (state.requestId) queryRequest(state.requestId).catch(() => {}); });
  $("#student-query-form").addEventListener("submit", (event) => { event.preventDefault(); state.studentId = $("#student-id-input").value.trim(); if (state.studentId) queryStudent(state.studentId); });
  $("#predict-form").addEventListener("submit", submitPredict); $("#stop-polling").addEventListener("click", stopPolling);
  $("#close-dialog").addEventListener("click", () => $("#worker-dialog").close());
  $("#worker-dialog").addEventListener("click", (event) => { if (event.target === $("#worker-dialog")) $("#worker-dialog").close(); });
  route();
}
document.addEventListener("DOMContentLoaded", init);
