"use strict";

// Config and state
const STORAGE = { api: "kv260.centralApi", theme: "kv260.theme", view: "kv260.view" };
const SESSION_STORAGE = { adminToken: "kv260.adminActionToken" };
const DEFAULT_API = location.protocol === "file:" ? "http://127.0.0.1:8000" : location.origin;
const state = {
  apiBase: normalizeBase(localStorage.getItem(STORAGE.api) || DEFAULT_API),
  health: null, workers: [], artifacts: [], requests: [], students: [], studentRequests: [], workerRequests: [], events: [], eventTypes: [], cleanupPreview: null, lastSuccess: null,
  view: "overview", workerState: "all", artifactLimit: 100,
  studentSearch: "", artifactStudentFilter: "", studentRequestStudentId: "",
  studentRequestRange: "5", workerRequestWorkerId: "", workerRequestRange: "5",
  requestStatusFilter: "all", polling: null,
};
const statusText = {
  idle: "空闲", reserved: "已预留", deploying: "正在部署", ready: "已分配",
  busy: "正在计算", error: "错误", offline: "离线", queued: "排队中",
  running: "运行中", completed: "已完成", failed: "失败", unassigned: "未分配",
  releasing: "正在释放", lost: "Worker 丢失", archived: "已归档",
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
  return ["overview", "workers", "artifacts", "requests", "student", "events", "tools"].includes(value) ? value : "overview";
}
function route() {
  state.view = currentView(); localStorage.setItem(STORAGE.view, state.view);
  $$(".view").forEach((view) => view.classList.toggle("active", view.dataset.view === state.view));
  $$('[data-nav]').forEach((link) => link.classList.toggle("active", link.dataset.nav === state.view));
  refreshCurrent(true);
}
function navigateToStudent(studentId) {
  if (!studentId) return;
  state.studentSearch = studentId;
  $("#student-search-input").value = studentId;
  for (const selector of ["#worker-dialog", "#request-dialog"]) {
    const dialog = $(selector); if (dialog.open) dialog.close();
  }
  if (state.view === "student") renderStudents();
  location.hash = "#/student";
}
function navigateToStudentRequests(studentId) {
  state.studentRequestStudentId = studentId;
  state.studentRequestRange = "5";
  $("#student-request-id-input").value = studentId;
  $("#student-request-range").value = "5";
  if (state.view === "requests") queryStudentRequests(studentId, "5");
  location.hash = "#/requests";
}
function navigateToStudentArtifacts(studentId) {
  state.artifactStudentFilter = studentId;
  $("#artifact-student-filter").value = studentId;
  if (state.view === "artifacts") renderArtifacts();
  location.hash = "#/artifacts";
}
function navigateToStudentEvents(studentId) {
  $("#event-student-filter").value = studentId;
  if (state.view === "events") loadEvents();
  location.hash = "#/events";
}
function navigateToWorker(board) {
  if (!board) return;
  $("#worker-search").value = board;
  const dialog = $("#request-dialog"); if (dialog.open) dialog.close();
  if (state.view === "workers") renderWorkers();
  location.hash = "#/workers";
}
function studentLink(studentId) {
  if (!studentId) return document.createTextNode("—");
  const button = node("button", { className: "link-button student-link", text: studentId, type: "button" });
  button.addEventListener("click", (event) => { event.stopPropagation(); navigateToStudent(studentId); });
  button.addEventListener("keydown", (event) => event.stopPropagation());
  return button;
}
function workerLink(board) {
  if (!board) return document.createTextNode("—");
  const button = node("button", { className: "link-button worker-link", text: board, type: "button" });
  button.addEventListener("click", (event) => { event.stopPropagation(); navigateToWorker(board); });
  button.addEventListener("keydown", (event) => event.stopPropagation());
  return button;
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
    ["运行请求", "Running Requests", requests.running], ["已完成请求", "Completed Requests", requests.completed],
    ["失败请求", "Failed Requests", requests.failed],
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
    renderWorkers(); renderWorkerRequestOptions();
  } catch (error) { toast(error.message, "error"); }
}
function renderWorkerRequestOptions() {
  const select = $("#worker-request-id-select");
  const selected = state.workerRequestWorkerId || select.value;
  clear(select);
  if (!state.workers.length) {
    const option = node("option", { text: "暂无 Worker / No workers" }); option.value = ""; select.append(option); return;
  }
  for (const worker of state.workers) {
    const option = node("option", { text: `${worker.board} (${stateKey(worker.state).toUpperCase()})` });
    option.value = worker.board; select.append(option);
  }
  if (state.workers.some((worker) => worker.board === selected)) select.value = selected;
}
function workerCard(worker) {
  const key = stateKey(worker.state);
  const card = node("div", { className: `worker-card state-${key}` });
  card.tabIndex = 0; card.setAttribute("role", "button");
  const badge = node("span", { className: `state-badge status-${key}` }, [node("span", { text: key.toUpperCase() }), node("small", { text: statusText[key] || "未知" })]);
  card.append(node("div", { className: "worker-head" }, [node("strong", { text: worker.board || "—" }), badge]));
  const fields = node("dl", { className: "worker-fields" });
  const pairs = [["Student", worker.student_id], ["Artifact", worker.artifact_id], ["FPGA Ready", worker.fpga_ready ? "Yes" : "No"], ["Last Seen", relativeTime(worker.last_seen)], ["Error", worker.last_error]];
  for (const [label, value] of pairs) {
    const detail = node("dd", { title: value ? String(value) : "" });
    detail.append(label === "Student" ? studentLink(value) : document.createTextNode(value || "—"));
    fields.append(node("dt", { text: label }), detail);
  }
  card.append(fields);
  card.addEventListener("click", () => openWorker(worker));
  card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openWorker(worker); } });
  return card;
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
  for (const [label, value] of rows) {
    const detail = node("dd", { title: value ? String(value) : "" });
    detail.append(label === "Student" ? studentLink(value) : document.createTextNode(value || "—"));
    list.append(node("dt", { text: label }), detail);
  }
  content.append(list);
  if (stateKey(worker.state) === "ready" && worker.student_id && worker.lease_id) {
    const releaseButton = node("button", { className: "button danger", text: "释放 Worker / Release Worker", type: "button" });
    releaseButton.addEventListener("click", () => releaseWorker(worker, releaseButton));
    content.append(node("div", { className: "worker-actions" }, [releaseButton]));
  }
  $("#worker-dialog").showModal();
}
function adminToken() {
  try { return sessionStorage.getItem(SESSION_STORAGE.adminToken) || ""; }
  catch { return ""; }
}
function renderAdminTokenStatus() {
  const status = $("#admin-token-status");
  const saved = Boolean(adminToken());
  status.className = `form-result ${saved ? "success" : ""}`;
  status.textContent = saved ? "已为当前会话保存 / Saved for this session" : "当前会话未设置令牌 / No token set for this session";
}
function saveAdminToken(event) {
  event.preventDefault();
  const input = $("#admin-token-input"), token = input.value;
  if (!token) return;
  try { sessionStorage.setItem(SESSION_STORAGE.adminToken, token); }
  catch { toast("无法保存 Admin Action Token", "error"); return; }
  input.value = "";
  renderAdminTokenStatus();
  toast("Admin Action Token 已保存到当前会话", "success");
}
function clearAdminToken() {
  try { sessionStorage.removeItem(SESSION_STORAGE.adminToken); }
  catch { /* The status below remains authoritative for this page. */ }
  $("#admin-token-input").value = "";
  renderAdminTokenStatus();
  toast("Admin Action Token 已清除", "success");
}
async function releaseWorker(worker, button) {
  const token = adminToken();
  if (!token) { toast("请先在 Tools 中设置 Admin Action Token", "warning"); return; }
  if (!window.confirm(`确认释放 ${worker.board} 当前属于 ${worker.student_id} 的 Lease？`)) return;
  const original = button.textContent; button.disabled = true; button.textContent = "Releasing...";
  try {
    await apiFetch(`/workers/${encodeURIComponent(worker.board)}/release`, {
      method: "POST", headers: { "X-Admin-Token": token }, timeout: 60000,
    });
    $("#worker-dialog").close();
    await Promise.allSettled([loadWorkers(), loadHealth()]);
    toast("Worker 已释放 / Worker released", "success");
  } catch (error) {
    button.disabled = false; button.textContent = original;
    toast(error.message, "error");
  }
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
  for (const item of state.artifacts) {
    if (stateKey(item.status) !== "ready") continue;
    result.set(item.student_id, Math.max(result.get(item.student_id) ?? -1, versionNumber(item.version)));
  }
  return result;
}
function filteredArtifacts() {
  const student = state.artifactStudentFilter.trim().toLowerCase();
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
    const student = node("td"); student.append(studentLink(item.student_id));
    [student, version, idCell, node("td", { className: `status-${stateKey(item.status)}`, text: stateLabel(item.status) }), node("td", { text: formatBytes(item.bit_size) }), node("td", { text: formatBytes(item.hwh_size) }), node("td", { text: formatTime(item.created_at), title: item.created_at }), sha].forEach((cell) => row.append(cell));
    body.append(row);
  }
  $("#artifact-show-more").classList.toggle("hidden", shown.length >= all.length);
}
function renderRecentArtifacts() {
  const body = $("#recent-artifacts"); clear(body);
  const recent = state.artifacts.slice(0, 5);
  if (!recent.length) { const row = node("tr"); const cell = node("td", { className: "empty", text: "暂无 Artifact" }); cell.colSpan = 4; row.append(cell); body.append(row); return; }
  for (const item of recent) {
    const row = node("tr"), student = node("td"); student.append(studentLink(item.student_id)); row.append(student);
    [item.version, stateLabel(item.status), formatTime(item.created_at)].forEach((value) => row.append(node("td", { text: value })));
    body.append(row);
  }
}
async function uploadArtifact(form) {
  const result = $(".form-result", form.parentElement); clear(result); result.className = "form-result";
  const student = form.elements.student_id.value.trim(), password = form.elements.password.value, bit = form.elements.bit.files[0], hwh = form.elements.hwh.files[0];
  if (!student || !password || !bit || !hwh) { showFormError(result, "请填写 Student ID、Password 并选择 bit/hwh 文件。"); return; }
  if (!bit.name.toLowerCase().endsWith(".bit") || !hwh.name.toLowerCase().endsWith(".hwh")) { showFormError(result, "文件扩展名必须是 .bit 和 .hwh。"); return; }
  const button = $("button[type=submit]", form), original = button.textContent; button.disabled = true; button.textContent = "Uploading...";
  const data = new FormData(); data.append("student_id", student); data.append("password", password); data.append("bit", bit); data.append("hwh", hwh);
  try {
    const artifact = (await apiFetch("/fpga/artifacts", { method: "POST", body: data, timeout: 120000 })).data;
    result.className = "form-result success"; result.textContent = `上传成功 / Upload Successful：${artifact.student_id} ${artifact.version} ${artifact.artifact_id} · BIT ${formatBytes(artifact.bit_size)} · HWH ${formatBytes(artifact.hwh_size)}`;
    toast("Artifact 上传成功", "success"); form.reset(); await loadArtifacts();
  } catch (error) { showFormError(result, `上传失败${error.status ? ` HTTP ${error.status}` : ""}：${error.message}`); toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = original; }
}
function showFormError(element, message) { element.className = "form-result error"; element.textContent = message; }

function renderCleanupPreview() {
  const preview = state.cleanupPreview;
  const container = $("#cleanup-preview"), body = $("#cleanup-table"); clear(body);
  if (!preview) { container.classList.add("hidden"); return; }
  $("#cleanup-candidate-count").textContent = preview.candidates;
  $("#cleanup-reclaimable").textContent = formatBytes(preview.reclaimable_bytes);
  for (const item of preview.artifacts) {
    const row = node("tr");
    const student = node("td"); student.append(studentLink(item.student_id)); row.append(student);
    [item.version, item.artifact_id, formatBytes(item.size)].forEach((value) => row.append(node("td", { className: String(value).startsWith("art_") ? "mono" : "", text: value })));
    body.append(row);
  }
  if (!preview.artifacts.length) { const row = node("tr"); const cell = node("td", { className: "empty", text: "没有可安全清理的旧版本 / No cleanup candidates" }); cell.colSpan = 4; row.append(cell); body.append(row); }
  $("#cleanup-execute-button").disabled = preview.candidates === 0;
  container.classList.remove("hidden");
}
async function previewCleanup() {
  const token = adminToken();
  if (!token) { toast("请先在 Tools 中设置 Admin Action Token", "warning"); return; }
  const button = $("#cleanup-preview-button"), original = button.textContent; button.disabled = true; button.textContent = "Scanning...";
  try {
    state.cleanupPreview = (await apiFetch("/admin/artifacts/cleanup-preview", { headers: { "X-Admin-Token": token } })).data;
    $("#cleanup-result").className = "form-result success";
    $("#cleanup-result").textContent = `可清理 Artifact：${state.cleanupPreview.candidates} · 预计释放：${formatBytes(state.cleanupPreview.reclaimable_bytes)}`;
    renderCleanupPreview();
  } catch (error) { showFormError($("#cleanup-result"), error.message); toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = original; }
}
async function executeCleanup() {
  const token = adminToken();
  if (!token) { toast("请先在 Tools 中设置 Admin Action Token", "warning"); return; }
  if (!window.confirm("将删除这些旧 Artifact 的 bit/hwh 实体文件。\nMetadata 和历史 Request 将保留。\n每个学生最新 Artifact 和正在使用的 Artifact 不会删除。\n是否继续？")) return;
  const button = $("#cleanup-execute-button"), original = button.textContent; button.disabled = true; button.textContent = "Cleaning...";
  try {
    const result = (await apiFetch("/admin/artifacts/cleanup", { method: "POST", headers: { "X-Admin-Token": token }, timeout: 120000 })).data;
    $("#cleanup-result").className = `form-result ${result.failed_count ? "error" : "success"}`;
    $("#cleanup-result").textContent = `已归档 ${result.archived_count} 个 Artifact · 释放 ${formatBytes(result.freed_bytes)} · 失败 ${result.failed_count}`;
    state.cleanupPreview = null; renderCleanupPreview();
    await Promise.allSettled([loadArtifacts(), loadEvents()]);
    toast("Artifact 清理完成 / Cleanup completed", result.failed_count ? "warning" : "success");
  } catch (error) { showFormError($("#cleanup-result"), error.message); toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = original; }
}

function eventFilters() {
  return {
    level: $("#event-level-filter").value.trim(), event_type: $("#event-type-filter").value.trim(),
    student_id: $("#event-student-filter").value.trim(), board: $("#event-board-filter").value.trim(),
    request_id: $("#event-request-filter").value.trim(),
  };
}
async function loadEventTypes() {
  try {
    const data = (await apiFetch("/events/types")).data;
    state.eventTypes = Array.isArray(data) ? data : [];
    const select = $("#event-type-filter"), selected = select.value;
    clear(select);
    const all = node("option", { text: "全部 / All" }); all.value = ""; select.append(all);
    for (const item of state.eventTypes) {
      const option = node("option", { text: `${item.value} / ${item.label}` });
      option.value = item.value; select.append(option);
    }
    if (state.eventTypes.some((item) => item.value === selected)) select.value = selected;
  } catch (error) {
    toast(`加载 Event Type 失败：${error.message}`, "error");
  }
}
async function loadEvents() {
  const params = new URLSearchParams({ limit: "100" });
  for (const [key, value] of Object.entries(eventFilters())) if (value) params.set(key, value);
  const errorBox = $("#event-list-error");
  try {
    const data = (await apiFetch(`/events?${params}`)).data;
    state.events = Array.isArray(data) ? data : [];
    errorBox.classList.add("hidden"); renderEvents();
  } catch (error) {
    errorBox.textContent = `加载系统事件失败 / Failed to load events：${error.message}`;
    errorBox.classList.remove("hidden"); if (!state.events.length) renderEvents();
  }
}
function renderEvents() {
  const body = $("#event-table"); clear(body);
  if (!state.events.length) { const row = node("tr"); const cell = node("td", { className: "empty", text: "暂无系统事件 / No events" }); cell.colSpan = 8; row.append(cell); body.append(row); return; }
  for (const item of state.events) {
    const row = node("tr");
    const level = stateKey(item.level);
    const student = node("td"); student.append(studentLink(item.student_id));
    [node("td", { text: formatTime(item.created_at), title: item.created_at }), node("td", { className: `status-${level}`, text: item.level }), node("td", { className: "mono", text: item.event_type }), student, node("td", { text: item.board || "—" }), node("td", { className: "mono", text: shortHash(item.artifact_id), title: item.artifact_id || "" }), node("td", { className: "mono", text: shortHash(item.request_id), title: item.request_id || "" }), node("td", { text: item.message, title: item.details ? JSON.stringify(item.details) : "" })].forEach((cell) => row.append(cell));
    body.append(row);
  }
}

// Request list, request detail, and student detail
function renderDetail(container, rows, jsonValue = undefined) {
  clear(container); container.className = "detail-output";
  const list = node("dl", { className: "detail-grid" });
  for (const [label, value] of rows) list.append(node("dt", { text: label }), node("dd", { text: value ?? "—" }));
  container.append(list);
  if (jsonValue !== undefined) container.append(node("pre", { text: JSON.stringify(jsonValue, null, 2) }));
}
function requestDuration(item) {
  if (!item.started_at) return "—";
  const start = new Date(item.started_at).getTime();
  const end = item.completed_at ? new Date(item.completed_at).getTime() : Date.now();
  const seconds = (end - start) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? `${seconds.toFixed(2)} s` : "—";
}
function requestResultSummary(result, status) {
  const container = node("div", { className: "result-summary" });
  if (!result || typeof result !== "object") {
    container.append(node("span", { text: stateKey(status) === "completed" ? "Completed" : "—" }));
    return container;
  }
  const chinese = result.flower_cn || result.raw_class;
  const english = result.flower || result.predicted_class || result.flower_api;
  let label = chinese && english && chinese !== english ? `${chinese} / ${english}` : chinese || english;
  if (!label) {
    const keys = Object.keys(result).filter((key) => !["ok", "status"].includes(key)).slice(0, 2);
    label = keys.length ? keys.map((key) => `${key}: ${String(result[key])}`).join(" · ") : "Completed";
  }
  container.append(node("span", { text: label }));
  const confidence = Number(result.confidence);
  if (result.confidence != null && Number.isFinite(confidence)) container.append(node("small", { text: confidence.toFixed(2) }));
  return container;
}
async function loadRequests() {
  const errorBox = $("#request-list-error");
  try {
    const data = (await apiFetch("/requests?limit=100")).data;
    state.requests = Array.isArray(data) ? data : [];
    errorBox.classList.add("hidden");
    renderRecentRequests();
  } catch (error) {
    errorBox.textContent = `加载 Request 列表失败 / Failed to load requests：${error.message}`;
    errorBox.classList.remove("hidden");
    if (!state.requests.length) renderRecentRequests();
  }
}
function renderRecentRequests() {
  const filtered = state.requestStatusFilter === "all"
    ? state.requests
    : state.requests.filter((item) => stateKey(item.status) === state.requestStatusFilter);
  $("#request-filter-count").textContent = `显示 ${filtered.length} / ${state.requests.length} 条`;
  renderRequests($("#request-table"), filtered);
}
function renderRequests(body = $("#request-table"), requests = state.requests, emptyMessage = "暂无计算请求 / No predict requests") {
  clear(body);
  if (!requests.length) {
    const row = node("tr"); const cell = node("td", { className: "empty", text: emptyMessage });
    cell.colSpan = 10; row.append(cell); body.append(row); return;
  }
  for (const item of requests) {
    const key = stateKey(item.status);
    const row = node("tr", { className: `request-row state-${key}` });
    row.tabIndex = 0;
    const requestId = node("td", { className: "mono", text: shortHash(item.request_id), title: item.request_id });
    const artifact = node("td", { className: "mono", text: `${shortHash(item.artifact_id)} / ${item.version || "—"}`, title: `${item.artifact_id || ""} / ${item.version || ""}` });
    const result = node("td"); result.append(requestResultSummary(item.result, item.status));
    const student = node("td"); student.append(studentLink(item.student_id));
    const worker = node("td"); worker.append(workerLink(item.worker));
    const detailButton = node("button", { className: "link-button", text: "详情 / Detail", type: "button" });
    detailButton.addEventListener("click", (event) => { event.stopPropagation(); openRequest(item); });
    [
      node("td", { className: `status-${key}`, text: stateLabel(item.status) }), requestId,
      student, artifact, worker,
      node("td", { text: formatTime(item.created_at), title: item.created_at }), node("td", { text: requestDuration(item) }),
      result, node("td", { className: "request-error", text: item.error || "—", title: item.error || "" }), node("td", {}, [detailButton]),
    ].forEach((cell) => row.append(cell));
    row.addEventListener("click", () => openRequest(item));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openRequest(item); } });
    body.append(row);
  }
}
async function queryWorkerRequests(board, range = state.workerRequestRange) {
  state.workerRequestWorkerId = board;
  state.workerRequestRange = range;
  $("#worker-request-id-select").value = board;
  $("#worker-request-range").value = range;
  const errorBox = $("#worker-request-error");
  try {
    const path = range === "all"
      ? `/requests?worker_id=${encodeURIComponent(board)}&all=true`
      : `/requests?worker_id=${encodeURIComponent(board)}&limit=5`;
    const data = (await apiFetch(path, { timeout: range === "all" ? 30000 : 5000 })).data;
    state.workerRequests = Array.isArray(data) ? data : [];
    errorBox.classList.add("hidden");
    const summary = $("#worker-request-summary");
    summary.textContent = range === "all"
      ? `${board} · 全部 ${state.workerRequests.length} 条`
      : `${board} · 最近 5 条`;
    summary.classList.remove("hidden");
    renderRequests(
      $("#worker-request-table"), state.workerRequests,
      `该 Worker 暂无计算请求 / No predict requests for ${board}`,
    );
  } catch (error) {
    errorBox.textContent = `加载 Worker Request 列表失败 / Failed to load Worker requests：${error.message}`;
    errorBox.classList.remove("hidden");
    if (!state.workerRequests.length) renderRequests($("#worker-request-table"), []);
  }
}
async function queryStudentRequests(studentId, range = state.studentRequestRange) {
  state.studentRequestStudentId = studentId;
  state.studentRequestRange = range;
  $("#student-request-id-input").value = studentId;
  $("#student-request-range").value = range;
  const errorBox = $("#student-request-error");
  try {
    const path = range === "all"
      ? `/requests?student_id=${encodeURIComponent(studentId)}&all=true`
      : `/requests?student_id=${encodeURIComponent(studentId)}&limit=5`;
    const data = (await apiFetch(path, { timeout: range === "all" ? 30000 : 5000 })).data;
    state.studentRequests = Array.isArray(data) ? data : [];
    errorBox.classList.add("hidden");
    const summary = $("#student-request-summary");
    summary.textContent = range === "all"
      ? `${studentId} · 全部 ${state.studentRequests.length} 条`
      : `${studentId} · 最近 5 条`;
    summary.classList.remove("hidden");
    renderRequests(
      $("#student-request-table"), state.studentRequests,
      `该学生暂无计算请求 / No predict requests for ${studentId}`,
    );
  } catch (error) {
    errorBox.textContent = `加载学生 Request 列表失败 / Failed to load student requests：${error.message}`;
    errorBox.classList.remove("hidden");
    if (!state.studentRequests.length) renderRequests($("#student-request-table"), []);
  }
}
function openRequest(item) {
  const content = $("#request-dialog-content"); clear(content);
  const list = node("dl", { className: "detail-grid" });
  const rows = [
    ["Request ID", item.request_id], ["Student ID", item.student_id], ["Artifact ID", item.artifact_id],
    ["Version", item.version], ["Status", stateLabel(item.status)], ["Worker", item.worker],
    ["Created At", formatTime(item.created_at)], ["Started At", formatTime(item.started_at)],
    ["Completed At", formatTime(item.completed_at)], ["Duration", requestDuration(item)], ["Error", item.error],
  ];
  for (const [label, value] of rows) {
    const detail = node("dd");
    if (label === "Student ID") detail.append(studentLink(value));
    else if (label === "Worker") detail.append(workerLink(value));
    else detail.append(document.createTextNode(value ?? "—"));
    list.append(node("dt", { text: label }), detail);
  }
  content.append(list, node("h3", { text: "Result" }), node("pre", { text: JSON.stringify(item.result, null, 2) }));
  $("#request-dialog").showModal();
}
async function queryRequest(requestId, target = $("#predict-result"), password = $("#predict-password").value) {
  try {
    const item = (await apiFetch(`/requests/${encodeURIComponent(requestId)}`, { headers: { "X-Student-Password": password } })).data;
    renderDetail(target, [["Request ID", item.request_id], ["Student", item.student_id], ["Artifact", item.artifact_id], ["Version", item.version], ["Worker", item.worker], ["Status", stateLabel(item.status)], ["Error", item.error]], item.result);
    return item;
  } catch (error) { target.className = "detail-output empty"; target.textContent = error.status === 404 ? "未找到 Request / Request not found" : error.message; throw error; }
}
async function loadStudents() {
  const errorBox = $("#student-list-error");
  try {
    const data = (await apiFetch("/students")).data;
    state.students = Array.isArray(data) ? data : [];
    errorBox.classList.add("hidden"); renderStudents();
  } catch (error) {
    errorBox.textContent = `加载 Student 列表失败 / Failed to load students：${error.message}`;
    errorBox.classList.remove("hidden"); if (!state.students.length) renderStudents();
  }
}
function studentAction(label, handler) {
  const button = node("button", { className: "link-button", text: label, type: "button" });
  button.addEventListener("click", handler); return button;
}
function renderStudents() {
  const body = $("#student-table"); clear(body);
  const search = state.studentSearch.trim().toLowerCase();
  const students = state.students
    .filter((item) => !search || String(item.student_id).toLowerCase().includes(search))
    .sort((a, b) => naturalCompare(a.student_id, b.student_id));
  if (!students.length) {
    const row = node("tr"), cell = node("td", { className: "empty", text: "暂无 Student / No students" });
    cell.colSpan = 11; row.append(cell); body.append(row); return;
  }
  for (const item of students) {
    const row = node("tr"), actions = node("td", { className: "student-actions" });
    actions.append(
      studentAction("Requests", () => navigateToStudentRequests(item.student_id)),
      studentAction("Artifacts", () => navigateToStudentArtifacts(item.student_id)),
      studentAction("Events", () => navigateToStudentEvents(item.student_id)),
    );
    const student = node("td"); student.append(studentLink(item.student_id));
    [
      student,
      node("td", { text: item.latest_version || "—", title: item.latest_artifact_id || "" }),
      node("td", { className: `status-${stateKey(item.lease_state)}`, text: stateLabel(item.lease_state) }),
      node("td", { text: item.worker_id || "—" }),
      node("td", { text: item.queued_requests }), node("td", { text: item.running_requests }),
      node("td", { text: item.completed_requests }), node("td", { text: item.failed_requests }),
      node("td", { text: item.total_requests }),
      node("td", { text: formatTime(item.last_activity_at), title: item.last_activity_at || "" }),
      actions,
    ].forEach((cell) => row.append(cell));
    body.append(row);
  }
}

// Predict tester
async function submitPredict(event) {
  event.preventDefault(); stopPolling();
  const student = $("#predict-student").value.trim(), password = $("#predict-password").value, raw = $("#predict-payload").value;
  let payload;
  try { payload = JSON.parse(raw); } catch { toast("Payload 不是合法 JSON / Invalid JSON payload", "error"); return; }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) { toast("Payload 顶层必须是 JSON object", "error"); return; }
  if (!student) { toast("Student ID 不能为空", "error"); return; }
  const button = $("#predict-submit"), target = $("#predict-result"); button.disabled = true;
  try {
    const response = await apiFetch("/predict", { method: "POST", headers: { "Content-Type": "application/json", "X-Student-Password": password }, body: JSON.stringify({ student_id: student, payload }), timeout: 60000 });
    const item = response.data;
    renderDetail(target, [["Request ID", item.request_id], ["Student", item.student_id], ["Artifact", item.artifact_id], ["Version", item.version], ["Worker", item.worker], ["Status", stateLabel(item.status)], ["Error", item.error]], item.result);
    if (response.status === 202 || stateKey(item.status) === "queued") { toast("请求已排队 / Queued", "warning"); startPolling(item.request_id, password); } else toast("请求完成 / Completed", "success");
  } catch (error) { target.className = "detail-output empty"; target.textContent = error.message; toast(error.message, "error"); }
  finally { button.disabled = false; }
}
function startPolling(requestId, password) {
  stopPolling(); let attempts = 0; $("#stop-polling").classList.remove("hidden");
  state.polling = setInterval(async () => {
    attempts += 1;
    try {
      const item = await queryRequest(requestId, $("#predict-result"), password);
      if (["completed", "failed"].includes(stateKey(item.status))) { stopPolling(); toast(item.status === "completed" ? "请求完成" : "请求失败", item.status === "completed" ? "success" : "error"); }
    } catch { /* The detail panel already contains the error. */ }
    if (attempts >= 150) { stopPolling(); toast("自动查询已停止，请手动查询 Request ID。", "warning"); }
  }, 2000);
}
function stopPolling() { if (state.polling) clearInterval(state.polling); state.polling = null; $("#stop-polling").classList.add("hidden"); }

// Unified refresh manager
let refreshTimer = null;
async function refreshCurrent(force = false, manual = false) {
  clearTimeout(refreshTimer);
  const tasks = [];
  if (state.view === "overview") tasks.push(loadHealth(), loadWorkers(), loadArtifacts());
  else if (state.view === "workers") tasks.push(loadHealth(), loadWorkers());
  else if (state.view === "artifacts") tasks.push(loadArtifacts());
  else if (state.view === "requests") {
    tasks.push(loadRequests(), loadWorkers());
    if (state.workerRequestWorkerId && (state.workerRequestRange !== "all" || manual)) {
      tasks.push(queryWorkerRequests(state.workerRequestWorkerId, state.workerRequestRange));
    }
    if (state.studentRequestStudentId && (state.studentRequestRange !== "all" || manual)) {
      tasks.push(queryStudentRequests(state.studentRequestStudentId, state.studentRequestRange));
    }
  }
  else if (state.view === "student") tasks.push(loadStudents());
  else if (state.view === "events") {
    tasks.push(loadEvents());
    if (!state.eventTypes.length) tasks.push(loadEventTypes());
  }
  if (force) await Promise.allSettled(tasks);
  const visible = document.visibilityState === "visible";
  const normal = state.view === "artifacts" ? 10000 : ["student", "events"].includes(state.view) ? 5000 : 2000;
  refreshTimer = setTimeout(() => refreshCurrent(true), visible ? normal : 15000);
}

// Event wiring and initialization
function setApiBase(value) {
  const normalized = normalizeBase(value);
  try { new URL(normalized); } catch { toast("Central API URL 无效", "error"); return; }
  state.apiBase = normalized; localStorage.setItem(STORAGE.api, normalized);
  $("#api-base").value = normalized; $("#footer-api").textContent = normalized;
  $("#api-docs").href = `${normalized}/docs`; state.health = null; state.workers = []; state.artifacts = []; state.requests = []; state.students = []; state.studentRequests = []; state.workerRequests = []; state.events = []; state.eventTypes = []; state.cleanupPreview = null;
  toast("Central API 地址已保存", "success"); refreshCurrent(true);
}
function init() {
  applyTheme(localStorage.getItem(STORAGE.theme));
  $("#api-base").value = state.apiBase; $("#footer-api").textContent = state.apiBase; $("#api-docs").href = `${state.apiBase}/docs`;
  if (location.protocol === "file:" && !localStorage.getItem(STORAGE.api)) toast("当前页面以本地文件打开，请确认 Central API 地址。", "warning");
  renderAdminTokenStatus();
  $("#save-api").addEventListener("click", () => setApiBase($("#api-base").value));
  $("#api-base").addEventListener("keydown", (event) => { if (event.key === "Enter") setApiBase(event.target.value); });
  $("#theme-button").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; localStorage.setItem(STORAGE.theme, theme); applyTheme(theme); });
  $("#refresh-button").addEventListener("click", () => refreshCurrent(true, true));
  addEventListener("hashchange", route); document.addEventListener("visibilitychange", () => refreshCurrent(true));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => { location.hash = `#/${button.dataset.go}`; }));
  $("#worker-alert").addEventListener("click", () => { $("#worker-state-filter").value = "issues"; location.hash = "#/workers"; renderWorkers(); });
  $("#worker-search").addEventListener("input", renderWorkers); $("#worker-state-filter").addEventListener("change", renderWorkers);
  $("#artifact-student-filter").addEventListener("input", (event) => { state.artifactStudentFilter = event.target.value; renderArtifacts(); });
  $("#artifact-id-filter").addEventListener("input", renderArtifacts);
  $("#artifact-status-filter").addEventListener("change", renderArtifacts); $("#artifact-latest-only").addEventListener("change", renderArtifacts);
  $("#artifact-show-more").addEventListener("click", () => { state.artifactLimit += 100; renderArtifacts(); });
  $("#cleanup-preview-button").addEventListener("click", previewCleanup); $("#cleanup-execute-button").addEventListener("click", executeCleanup);
  $$('[data-upload-form]').forEach((form) => form.addEventListener("submit", (event) => { event.preventDefault(); uploadArtifact(form); }));
  $("#worker-request-query-form").addEventListener("submit", (event) => { event.preventDefault(); const board = $("#worker-request-id-select").value, range = $("#worker-request-range").value; if (board) queryWorkerRequests(board, range); });
  $("#worker-request-range").addEventListener("change", (event) => { const board = $("#worker-request-id-select").value; state.workerRequestRange = event.target.value; if (board) queryWorkerRequests(board, state.workerRequestRange); });
  $("#student-request-query-form").addEventListener("submit", (event) => { event.preventDefault(); const studentId = $("#student-request-id-input").value.trim(), range = $("#student-request-range").value; if (studentId) queryStudentRequests(studentId, range); });
  $("#student-request-range").addEventListener("change", (event) => { const studentId = $("#student-request-id-input").value.trim(); state.studentRequestRange = event.target.value; if (studentId) queryStudentRequests(studentId, state.studentRequestRange); });
  $("#request-status-filter").addEventListener("change", (event) => { state.requestStatusFilter = event.target.value; renderRecentRequests(); });
  $("#student-search-input").addEventListener("input", (event) => { state.studentSearch = event.target.value; renderStudents(); });
  $("#event-filter-form").addEventListener("submit", (event) => { event.preventDefault(); loadEvents(); });
  $("#admin-token-form").addEventListener("submit", saveAdminToken); $("#clear-admin-token").addEventListener("click", clearAdminToken);
  $("#predict-form").addEventListener("submit", submitPredict); $("#stop-polling").addEventListener("click", stopPolling);
  $("#close-dialog").addEventListener("click", () => $("#worker-dialog").close());
  $("#worker-dialog").addEventListener("click", (event) => { if (event.target === $("#worker-dialog")) $("#worker-dialog").close(); });
  $("#close-request-dialog").addEventListener("click", () => $("#request-dialog").close());
  $("#request-dialog").addEventListener("click", (event) => { if (event.target === $("#request-dialog")) $("#request-dialog").close(); });
  route();
}
document.addEventListener("DOMContentLoaded", init);
