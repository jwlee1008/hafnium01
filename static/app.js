const $ = (id) => document.getElementById(id);

let state = null;
let currentEditingId = null;
let serialPorts = [];
let serialSuggestions = {};
let serialHealth = null;

function percent(value) {
  if (!value) return "0%";
  return `${(value * 100).toFixed(1)}%`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
}

function hideToast() {
  $("toast").classList.add("hidden");
}

async function api(path, method = "GET", body = null) {
  const options = { method, headers: {} };
  if (body) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function runAction(button, label, fn) {
  const original = button.textContent;
  button.classList.add("loading");
  button.textContent = "진행중...";
  showToast(label);
  try {
    await fn();
    await loadState();
  } catch (error) {
    alert(error.message);
  } finally {
    button.textContent = original;
    button.classList.remove("loading");
    hideToast();
  }
}

function nextSensorId() {
  const used = new Set((state?.nodes || []).map((node) => node.sensor_id));
  for (let i = 1; i < 1000; i += 1) {
    const candidate = `sensor_${String(i).padStart(2, "0")}`;
    if (!used.has(candidate)) return candidate;
  }
  return `sensor_${Date.now()}`;
}

function defaultNode() {
  return {
    sensor_id: nextSensorId(),
    name: "새 센서 노드",
    location_id: "unknown_position",
    enabled: true,
    connection_mode: "pc_iwr6843_usb",
    board: "ESP32-S3",
    sensor: "IWR6843AOPEVM",
    cli_port: "",
    data_port: "",
    esp_port: "",
    data_baud: 921600,
    esp_baud: 115200,
    cfg_path: "configs/vital_signs_AOP_6m.cfg",
    cfg_name: "vital_signs_AOP_6m.cfg",
    notes: ""
  };
}

function nodeReadiness(node) {
  if (!node.enabled) return { label: "비활성", missing: [], good: false };
  const missing = [];
  if (node.connection_mode === "pc_iwr6843_usb") {
    if (!node.cli_port) missing.push("CLI");
    if (!node.data_port) missing.push("DATA");
    if (!node.esp_port) missing.push("ESP");
  } else if (!node.esp_port) {
    missing.push("ESP");
  }
  return {
    label: missing.length ? `${missing.join(", ")} 포트 필요` : "연결준비 완료",
    missing,
    good: missing.length === 0
  };
}

function profileForNode(sensorId) {
  const profiles = state?.profile?.profile_batch?.sensor_profiles || [];
  return profiles.find((item) => item.sensor_id === sensorId);
}

function renderNodes(nodes) {
  const root = $("nodes");
  root.innerHTML = "";
  if (!nodes.length) {
    root.innerHTML = `<div class="empty-state">등록된 센서 노드가 없습니다. 오른쪽에서 노드를 추가하세요.</div>`;
    return;
  }

  nodes.forEach((node) => {
    const result = node.last_result;
    const deploy = node.last_deploy;
    const test = node.last_connection_test;
    const read = node.last_result_read;
    const readiness = nodeReadiness(node);
    const nodeProfile = profileForNode(node.sensor_id);
    const div = document.createElement("div");
    div.className = `node ${node.profile_status === "배포완료" ? "ok" : readiness.good ? "ready" : "warn"} ${node.enabled ? "" : "off"}`;
    div.innerHTML = `
      <div class="node-top">
        <h3>${escapeHtml(node.sensor_id)} · ${escapeHtml(node.name)}</h3>
        <span class="pill ${readiness.good ? "good" : "bad"}">${escapeHtml(readiness.label)}</span>
      </div>
      <div class="node-row"><span>위치</span><b>${escapeHtml(node.location_id)}</b></div>
      <div class="node-row"><span>모드</span><b>${escapeHtml(node.connection_mode)}</b></div>
      <div class="node-row"><span>센서</span><b>${escapeHtml(node.sensor)}</b></div>
      <div class="node-row"><span>보드</span><b>${escapeHtml(node.board)}</b></div>
      <div class="node-row"><span>CLI</span><b>${escapeHtml(node.cli_port || "-")}</b></div>
      <div class="node-row"><span>DATA</span><b>${escapeHtml(node.data_port || "-")}</b></div>
      <div class="node-row"><span>ESP</span><b>${escapeHtml(node.esp_port || "-")}</b></div>
      <div class="node-row"><span>상태</span><b>${escapeHtml(node.status)}</b></div>
      <div class="node-row"><span>Profile</span><b>${escapeHtml(node.profile_status)}</b></div>
      <div class="node-row"><span>배포</span><b>${deploy ? `${escapeHtml(deploy.method || "-")} · ${deploy.ok ? "OK" : "FAIL"}` : "-"}</b></div>
      <div class="node-row"><span>테스트</span><b>${test ? `${escapeHtml(test.method || "-")} · ${test.responses?.length || 0}줄` : "-"}</b></div>
      <div class="node-row"><span>읽기</span><b>${read ? `${escapeHtml(read.method || "-")} · ${read.responses?.length || 0}줄` : "-"}</b></div>
      <div class="result">
        <span>최근 결과</span>
        <strong>${result ? escapeHtml(result.status) : "NO DATA"}</strong>
        <div class="node-row"><span>사람 수</span><b>${result ? result.person_count : "-"}</b></div>
        <div class="node-row"><span>신뢰도</span><b>${result ? result.confidence : "-"}</b></div>
        <div class="node-row"><span>Profile</span><b>${nodeProfile ? escapeHtml(state.profile.profile_batch.profile_version) : "-"}</b></div>
      </div>
      <div class="node-actions">
        <button type="button" data-edit="${escapeHtml(node.sensor_id)}">수정</button>
        <button type="button" data-test="${escapeHtml(node.sensor_id)}">연결테스트</button>
        <button type="button" data-read="${escapeHtml(node.sensor_id)}">결과읽기</button>
        <button type="button" class="danger" data-remove="${escapeHtml(node.sensor_id)}">삭제</button>
      </div>
    `;
    root.appendChild(div);
  });

  root.querySelectorAll("[data-edit]").forEach((button) => {
    button.addEventListener("click", () => editNode(button.dataset.edit));
  });
  root.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => deleteNode(button.dataset.remove));
  });
  root.querySelectorAll("[data-test]").forEach((button) => {
    button.addEventListener("click", () => testNode(button.dataset.test));
  });
  root.querySelectorAll("[data-read]").forEach((button) => {
    button.addEventListener("click", () => readNodeResult(button.dataset.read));
  });
}

function renderQuality(summary) {
  $("dataDir").textContent = summary ? summary.data_dir : "-";
  const flags = $("qualityFlags");
  flags.innerHTML = "";
  if (!summary) {
    flags.innerHTML = `<div class="flag bad"><span>CSV 미스캔</span><b>스캔 필요</b></div>`;
    return;
  }
  const missing = summary.data_quality_flags.missing_baseline_labels || [];
  flags.innerHTML += missing.length
    ? `<div class="flag bad"><span>부족 라벨</span><b>${escapeHtml(missing.join(", "))}</b></div>`
    : `<div class="flag good"><span>기준 라벨</span><b>확보</b></div>`;
  flags.innerHTML += summary.data_quality_flags.small_multiclass_dataset
    ? `<div class="flag bad"><span>데이터 규모</span><b>추가 수집 필요</b></div>`
    : `<div class="flag good"><span>데이터 규모</span><b>충분</b></div>`;

  const labels = $("labelList");
  labels.innerHTML = "";
  (summary.labels || []).forEach((item) => {
    labels.innerHTML += `<div class="label"><span>${escapeHtml(item.value)}</span><b>${item.count}</b></div>`;
  });
}

function renderReadiness(nodes) {
  const root = $("readinessList");
  root.innerHTML = "";
  if (!nodes.length) {
    root.innerHTML = `<div class="flag bad"><span>노드 없음</span><b>추가 필요</b></div>`;
    return;
  }
  nodes.forEach((node) => {
    const ready = nodeReadiness(node);
    root.innerHTML += `
      <div class="flag ${ready.good ? "good" : "bad"}">
        <span>${escapeHtml(node.sensor_id)} · ${escapeHtml(node.name)}</span>
        <b>${escapeHtml(ready.label)}</b>
      </div>
    `;
  });
}

function renderFiles(summary) {
  const body = $("fileRows");
  body.innerHTML = "";
  if (!summary) return;
  (summary.files || []).forEach((file) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(file.file)}</td>
      <td>${file.rows}</td>
      <td>${file.vital_rows}</td>
      <td>${percent(file.vital_ratio)}</td>
      <td>${file.num_detected_obj_mean ?? "-"}</td>
      <td>${file.num_detected_obj_max ?? "-"}</td>
      <td>${file.breath_deviation_p50 ?? "-"}</td>
      <td>${file.breath_deviation_p90 ?? "-"}</td>
    `;
    body.appendChild(tr);
  });
}

function renderJobs(jobs) {
  const root = $("jobsList");
  root.innerHTML = "";
  if (!jobs || !jobs.length) {
    root.innerHTML = `<div class="flag bad"><span>실행중 프로세스</span><b>없음</b></div>`;
    return;
  }
  jobs.forEach((job) => {
    root.innerHTML += `
      <div class="job">
        <div class="node-row"><span>노드</span><b>${escapeHtml(job.sensor_id)}</b></div>
        <div class="node-row"><span>PID</span><b>${job.pid ?? "-"}</b></div>
        <div class="node-row"><span>상태</span><b>${job.running ? "실행중" : `종료 ${job.return_code}`}</b></div>
        <div class="node-row"><span>CSV</span><b>${escapeHtml(job.csv_path || "-")}</b></div>
        <div class="node-row"><span>로그</span><b>${escapeHtml(job.log_path || "-")}</b></div>
      </div>
    `;
  });
}

function renderLogs(logs) {
  const root = $("logs");
  root.innerHTML = "";
  logs.forEach((log) => {
    const div = document.createElement("div");
    div.className = "log";
    div.innerHTML = `<span>${escapeHtml(log.time)}</span><b>${escapeHtml(log.kind)}</b><span>${escapeHtml(log.message)}</span>`;
    root.appendChild(div);
  });
}

function renderProfile(profile, validation, meta) {
  $("profileView").textContent = JSON.stringify({ profile, validation }, null, 2);
  if (meta) {
    $("llmMeta").textContent = `${meta.model || "-"} · ${meta.total_duration_ms || "-"} ms`;
  } else {
    $("llmMeta").textContent = profile ? "이전 profile 로드됨" : "대기";
  }
}

function renderPorts() {
  const datalist = $("portOptions");
  datalist.innerHTML = serialPorts.map((port) => `<option value="${escapeHtml(port.device)}">${escapeHtml(port.description || "")}</option>`).join("");
  const list = $("portsList");
  const warnings = [];
  if (serialHealth && serialHealth.pyserial_available === false) {
    warnings.push("pyserial missing: run pip install -r requirements.txt");
  }
  const suggestionRows = Object.entries(serialSuggestions || {})
    .map(([role, item]) => `<div class="port-item"><b>${escapeHtml(role)}</b><span>${escapeHtml(item.device)} (${Math.round((item.confidence || 0) * 100)}%)</span></div>`)
    .join("");
  const portRows = serialPorts
    .map((port) => `<div class="port-item"><b>${escapeHtml(port.device)}</b><span>${escapeHtml(port.description || port.hwid || "")}</span></div>`)
    .join("");
  if (!serialPorts.length && !warnings.length) {
    list.textContent = "No serial ports found yet.";
    return;
  }
  list.innerHTML = [
    ...warnings.map((warning) => `<div class="port-item warn"><b>Warning</b><span>${escapeHtml(warning)}</span></div>`),
    suggestionRows ? `<button type="button" id="applyPortSuggestionsBtn">Apply suggested ports</button>${suggestionRows}` : "",
    portRows
  ].join("");
  const applyButton = $("applyPortSuggestionsBtn");
  if (applyButton) applyButton.addEventListener("click", () => applyPortSuggestions(true));
}

function applyPortSuggestions(notify = false) {
  const mapping = {
    cli_port: "cliPortInput",
    data_port: "dataPortInput",
    esp_port: "espPortInput"
  };
  Object.entries(mapping).forEach(([role, inputId]) => {
    const suggestion = serialSuggestions?.[role]?.device;
    const input = $(inputId);
    if (suggestion && input && !input.value.trim()) {
      input.value = suggestion;
    }
  });
  if (notify) showToast("Suggested ports applied to empty fields.");
}

function render(nextState) {
  state = nextState;
  $("clockNow").textContent = nextState.now;
  $("modelName").textContent = nextState.model;
  const summary = nextState.summary;
  $("totalRows").textContent = summary ? summary.total_rows : "0";
  $("vitalRows").textContent = summary ? summary.total_vital_rows : "0";
  $("vitalRatio").textContent = summary ? percent(summary.overall_vital_ratio) : "0%";
  $("collectState").textContent = nextState.collection_running ? "수집중" : "대기";
  $("monitorState").textContent = nextState.monitor_running ? "동작" : "대기";
  $("nodeCount").textContent = `${nextState.enabled_node_count || 0}/${nextState.node_count || 0}`;
  $("monitorBtn").textContent = nextState.monitor_running ? "7. 자동 모니터 중지" : "7. 자동 모니터 시작";
  $("monitorBtn").classList.toggle("primary", Boolean(nextState.monitor_running));
  renderNodes(nextState.nodes || []);
  renderQuality(summary);
  renderReadiness(nextState.nodes || []);
  renderJobs(nextState.collection_jobs || []);
  renderFiles(summary);
  renderProfile(nextState.profile, nextState.validation, nextState.llm_meta);
  renderLogs(nextState.logs || []);
  if (!currentEditingId && !$("sensorIdInput").value) {
    fillNodeForm(defaultNode(), false);
  }
}

async function loadState() {
  const nextState = await api("/api/state");
  render(nextState);
}

function fillNodeForm(node, editing) {
  currentEditingId = editing ? node.sensor_id : null;
  $("editMode").textContent = editing ? `${node.sensor_id} 수정중` : "새 노드";
  $("sensorIdInput").readOnly = editing;
  $("sensorIdInput").value = node.sensor_id || "";
  $("nodeNameInput").value = node.name || "";
  $("locationInput").value = node.location_id || "";
  $("modeInput").value = node.connection_mode || "pc_iwr6843_usb";
  $("sensorInput").value = node.sensor || "";
  $("boardInput").value = node.board || "";
  $("cliPortInput").value = node.cli_port || "";
  $("dataPortInput").value = node.data_port || "";
  $("espPortInput").value = node.esp_port || "";
  $("dataBaudInput").value = node.data_baud || 921600;
  $("espBaudInput").value = node.esp_baud || 115200;
  $("cfgPathInput").value = node.cfg_path || "";
  $("cfgNameInput").value = node.cfg_name || "";
  $("notesInput").value = node.notes || "";
  $("enabledInput").checked = node.enabled !== false;
  $("deleteNodeBtn").disabled = !editing;
}

function nodeFromForm() {
  return {
    sensor_id: $("sensorIdInput").value.trim(),
    name: $("nodeNameInput").value.trim(),
    location_id: $("locationInput").value.trim(),
    connection_mode: $("modeInput").value,
    sensor: $("sensorInput").value.trim(),
    board: $("boardInput").value.trim(),
    cli_port: $("cliPortInput").value.trim(),
    data_port: $("dataPortInput").value.trim(),
    esp_port: $("espPortInput").value.trim(),
    data_baud: Number($("dataBaudInput").value || 921600),
    esp_baud: Number($("espBaudInput").value || 115200),
    cfg_path: $("cfgPathInput").value.trim(),
    cfg_name: $("cfgNameInput").value.trim(),
    notes: $("notesInput").value.trim(),
    enabled: $("enabledInput").checked
  };
}

function editNode(sensorId) {
  const node = (state?.nodes || []).find((item) => item.sensor_id === sensorId);
  if (node) fillNodeForm(node, true);
}

async function deleteNode(sensorId = currentEditingId) {
  if (!sensorId) return;
  const ok = confirm(`${sensorId} 노드를 삭제할까요?`);
  if (!ok) return;
  await api("/api/nodes/delete", "POST", { sensor_id: sensorId });
  fillNodeForm(defaultNode(), false);
  await loadState();
}

async function testNode(sensorId) {
  showToast(`${sensorId} 연결 테스트중`);
  try {
    await api("/api/nodes/test", "POST", { sensor_id: sensorId });
    await loadState();
  } catch (error) {
    alert(error.message);
  } finally {
    hideToast();
  }
}

async function readNodeResult(sensorId) {
  showToast(`${sensorId} 결과 읽기중`);
  try {
    await api("/api/nodes/read-result", "POST", { sensor_id: sensorId });
    await loadState();
  } catch (error) {
    alert(error.message);
  } finally {
    hideToast();
  }
}

async function saveNode(event) {
  event.preventDefault();
  const payload = nodeFromForm();
  if (!payload.sensor_id) {
    alert("노드 ID가 필요합니다.");
    return;
  }
  if (currentEditingId) {
    payload.sensor_id = currentEditingId;
    await api("/api/nodes/update", "POST", payload);
  } else {
    await api("/api/nodes/add", "POST", payload);
  }
  fillNodeForm(defaultNode(), false);
  await loadState();
}

async function scanPorts() {
  const data = await api("/api/ports");
  serialPorts = data.ports || [];
  serialSuggestions = data.suggestions || {};
  serialHealth = data;
  renderPorts();
  applyPortSuggestions(false);
}

function bind() {
  $("scanBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "CSV 스캔중", () => api("/api/scan", "POST"));
  });
  $("portsBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "포트 스캔중", scanPorts);
  });
  $("collectBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "수집 상태 변경중", () => api("/api/collect", "POST"));
  });
  $("calibrateBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "로컬 LLM 캘리브레이션중", () => api("/api/calibrate", "POST"));
  });
  $("deployBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "ESP32 profile 배포중", () => api("/api/deploy", "POST"));
  });
  $("tickBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "결과 갱신중", () => api("/api/tick", "POST"));
  });
  $("monitorBtn").addEventListener("click", (event) => {
    runAction(event.currentTarget, "자동 모니터 상태 변경중", () => api("/api/monitor", "POST"));
  });
  $("nodeForm").addEventListener("submit", saveNode);
  $("newNodeBtn").addEventListener("click", () => fillNodeForm(defaultNode(), false));
  $("deleteNodeBtn").addEventListener("click", () => deleteNode());
}

bind();
renderPorts();
loadState();
scanPorts().catch(() => {});
setInterval(loadState, 5000);
