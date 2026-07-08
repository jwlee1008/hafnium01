const $ = (id) => document.getElementById(id);

let vitalsState = null;
let spatialState = null;
let selectedSensorId = localStorage.getItem("hanium.vitals.sensor") || "";
let spatialMetric = localStorage.getItem("hanium.vitals.spatialMetric") || "point_count";
let spatialMode = localStorage.getItem("hanium.vitals.spatialMode") || "aggregate";
let loading = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toFixed(digits);
}

function percent(value) {
  if (!value) return "0%";
  return `${(Number(value) * 100).toFixed(1)}%`;
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

function severityClass(node) {
  return node?.status_signal?.severity || "bad";
}

function ensureSelectedNode() {
  const nodes = vitalsState?.nodes || [];
  if (!nodes.length) {
    selectedSensorId = "";
    return null;
  }
  if (!selectedSensorId || !nodes.some((node) => node.sensor_id === selectedSensorId)) {
    selectedSensorId = nodes[0].sensor_id;
    localStorage.setItem("hanium.vitals.sensor", selectedSensorId);
  }
  return nodes.find((node) => node.sensor_id === selectedSensorId) || nodes[0];
}

function renderNodeSelect(nodes) {
  const select = $("nodeSelect");
  const current = selectedSensorId;
  select.innerHTML = "";
  if (!nodes.length) {
    select.innerHTML = `<option value="">노드 없음</option>`;
    select.disabled = true;
    return;
  }
  nodes.forEach((node) => {
    const option = document.createElement("option");
    option.value = node.sensor_id;
    option.textContent = `${node.sensor_id} · ${node.name || node.location_id || "센서"}`;
    select.appendChild(option);
  });
  select.disabled = false;
  select.value = nodes.some((node) => node.sensor_id === current) ? current : nodes[0].sensor_id;
}

function renderScoreboard(node) {
  const latest = node?.latest_vital;
  const latestRow = node?.latest_row;
  const signal = node?.status_signal || { label: "데이터 없음", severity: "bad" };
  const signalCard = $("signalCard");
  signalCard.className = `metric signal-card ${signal.severity || "bad"}`;
  $("signalState").textContent = signal.label || "NO DATA";
  $("signalDetail").textContent = node?.csv_exists
    ? `${node.csv_mtime || "-"} · ${node.csv_size || 0} bytes`
    : "수집 CSV 없음";
  $("heartRate").textContent = fmt(latest?.heart_rate, 1);
  $("breathRate").textContent = fmt(latest?.breath_rate, 1);
  $("breathDeviation").textContent = fmt(latest?.breath_deviation, 4);
  $("vitalRatio").textContent = percent(node?.recent_vital_ratio || 0);
  $("tailInfo").textContent = `${node?.tail_vital_rows || 0}/${node?.tail_rows || 0} rows`;
  $("frameValue").textContent = latestRow?.frame ?? latest?.frame ?? "-";
  $("rangeValue").textContent = `range ${latest?.range_bin || "-"}`;
}

function renderEdge(node) {
  const result = node?.pc_detection || node?.edge_result;
  const status = result?.status || "NO DATA";
  const edge = $("edgeStatus");
  const confidence = result?.confidence;
  let edgeSeverity = "bad";
  if (result?.survivor_candidate || status === "SURVIVOR_CANDIDATE") {
    edgeSeverity = "good";
  } else if (result) {
    edgeSeverity = "warn";
  }
  edge.className = `edge-status ${edgeSeverity}`;
  edge.querySelector("strong").textContent = status;
  $("edgeCount").textContent = result?.person_count ?? "-";
  $("edgeSurvivor").textContent = result ? (result.survivor_candidate ? "YES" : "NO") : "-";
  $("edgeConfidence").textContent = confidence === undefined || confidence === null ? "-" : fmt(confidence, 3);
  $("edgeProfile").textContent = result?.profile_version || "-";
  const metrics = result?.metrics || {};
  $("edgeMeta").textContent = result?.time
    ? `${result.time} · ${result.source || "unknown"} · range ${metrics.range_bin_mode || "-"}`
    : "결과 대기";
}

function renderNodeMeta(node) {
  const pc = node?.pc_detection;
  const metrics = pc?.metrics || {};
  const edge = node?.edge_result;
  const rows = [
    ["노드", `${node?.sensor_id || "-"} · ${node?.name || "-"}`],
    ["위치", node?.location_id || "-"],
    ["연결", node?.connection || "-"],
    ["노드 상태", node?.status || "-"],
    ["Profile", node?.profile_status || "-"],
    ["CSV", node?.csv_path || "-"],
    ["PC 근거", pc?.reason_ko || "-"],
    ["PC 윈도우", pc ? `${metrics.valid_vital_rows ?? 0} valid / ${metrics.window_rows ?? 0} rows` : "-"],
    ["Range 안정성", pc ? `${metrics.range_bin_mode || "-"} · ${percent(metrics.range_stability || 0)}` : "-"],
    ["ESP32 raw", edge ? `${edge.status || "-"} · conf ${fmt(edge.confidence, 3)}` : "-"],
    ["최근 Poll", vitalsState?.monitor_last_poll || "-"],
  ];
  $("nodeMeta").innerHTML = rows
    .map(([label, value]) => `<div class="node-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`)
    .join("");
}

function rangeFor(values, fallbackMin, fallbackMax) {
  const clean = values.filter((value) => Number.isFinite(value));
  if (!clean.length) return [fallbackMin, fallbackMax];
  const min = Math.min(fallbackMin, ...clean);
  const max = Math.max(fallbackMax, ...clean);
  const pad = Math.max((max - min) * 0.08, 1);
  return [min - pad, max + pad];
}

function drawSeries(ctx, series, key, color, min, max, left, top, width, height) {
  const values = series.map((item) => Number(item[key])).filter((value) => Number.isFinite(value));
  if (values.length < 2) return;
  ctx.beginPath();
  ctx.lineWidth = 3;
  ctx.strokeStyle = color;
  let started = false;
  series.forEach((item, index) => {
    const raw = Number(item[key]);
    if (!Number.isFinite(raw)) return;
    const x = left + (index / Math.max(series.length - 1, 1)) * width;
    const clamped = Math.max(min, Math.min(max, raw));
    const y = top + height - ((clamped - min) / Math.max(max - min, 1)) * height;
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    }
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawChart(series) {
  const canvas = $("vitalChart");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#0b1118";
  ctx.fillRect(0, 0, width, height);

  const left = 46;
  const right = 16;
  const top = 22;
  const bottom = 34;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;

  ctx.strokeStyle = "#283241";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = top + (plotHeight / 4) * i;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + plotWidth, y);
    ctx.stroke();
  }

  ctx.fillStyle = "#a8b1bf";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.fillText("HR", 12, top + 12);
  ctx.fillText("BR", 12, top + 32);

  if (!series.length) {
    ctx.fillStyle = "#a8b1bf";
    ctx.font = "18px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    ctx.fillText("VITAL 데이터 대기", left + 12, top + 42);
    return;
  }

  const heartValues = series.map((item) => Number(item.heart_rate));
  const breathValues = series.map((item) => Number(item.breath_rate));
  const [heartMin, heartMax] = rangeFor(heartValues, 40, 130);
  const [breathMin, breathMax] = rangeFor(breathValues, 0, 40);
  drawSeries(ctx, series, "heart_rate", "#ff5c5c", heartMin, heartMax, left, top, plotWidth, plotHeight);
  drawSeries(ctx, series, "breath_rate", "#59a6ff", breathMin, breathMax, left, top, plotWidth, plotHeight);

  const first = series[0]?.frame ?? "-";
  const last = series[series.length - 1]?.frame ?? "-";
  ctx.fillStyle = "#a8b1bf";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.fillText(`frame ${first}`, left, height - 10);
  ctx.fillText(`frame ${last}`, Math.max(left, width - 118), height - 10);
}

function heatColor(value, maxValue) {
  if (!maxValue || value <= 0) return "#111820";
  const t = Math.max(0, Math.min(1, Math.log1p(value) / Math.log1p(maxValue)));
  if (t < 0.18) return "#1c3942";
  if (t < 0.36) return "#236a78";
  if (t < 0.58) return "#2f91a4";
  if (t < 0.78) return "#d89a3d";
  return "#b84e42";
}

function metricLabel(data, key) {
  return (data?.metrics || []).find((item) => item.key === key)?.label || key;
}

function matrixMax(matrix) {
  return Math.max(0, ...(matrix || []).flat().map((value) => Number(value) || 0));
}

function drawSpatialHeatmap(data) {
  const canvas = $("spatialHeatmap");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#0b1118";
  ctx.fillRect(0, 0, width, height);

  if (!data?.available) {
    ctx.fillStyle = "#a8b1bf";
    ctx.font = "18px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    ctx.fillText("grid window 데이터 대기", 24, 48);
    return;
  }

  const metric = spatialMetric in (data.aggregate || {}) ? spatialMetric : "point_count";
  const mode = spatialMode in data ? spatialMode : "aggregate";
  const matrix = data?.[mode]?.[metric] || [];
  const grid = data.grid || {};
  const yCells = matrix.length;
  const xCells = matrix[0]?.length || 0;
  if (!xCells || !yCells) {
    ctx.fillStyle = "#a8b1bf";
    ctx.font = "18px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    ctx.fillText("grid matrix 없음", 24, 48);
    return;
  }

  const left = 54;
  const top = 18;
  const right = 20;
  const bottom = 42;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const cellWidth = plotWidth / xCells;
  const cellHeight = plotHeight / yCells;
  const maxValue = data?.maxima?.[metric]?.[mode] || matrixMax(matrix);

  for (let y = 0; y < yCells; y += 1) {
    for (let x = 0; x < xCells; x += 1) {
      const value = Number(matrix[y][x]) || 0;
      const px0 = left + x * cellWidth;
      const py0 = top + (yCells - y - 1) * cellHeight;
      ctx.fillStyle = heatColor(value, maxValue);
      ctx.fillRect(px0, py0, Math.ceil(cellWidth), Math.ceil(cellHeight));
      ctx.strokeStyle = "#26313f";
      ctx.lineWidth = 1;
      ctx.strokeRect(px0, py0, cellWidth, cellHeight);
      if (value > 0 && cellWidth > 32 && cellHeight > 22) {
        ctx.fillStyle = value > maxValue * 0.58 ? "#fff7e8" : "#d7e8ef";
        ctx.font = "11px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(value >= 1000 ? `${(value / 1000).toFixed(1)}k` : `${Math.round(value)}`, px0 + cellWidth / 2, py0 + cellHeight / 2 + 4);
      }
    }
  }

  ctx.strokeStyle = "#7f8da1";
  ctx.lineWidth = 2;
  ctx.strokeRect(left, top, plotWidth, plotHeight);
  ctx.fillStyle = "#a8b1bf";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`${metricLabel(data, metric)} · ${mode === "aggregate" ? "누적" : "최신"} · max ${fmt(maxValue, 1)}`, left, 14);
  ctx.fillText(`X ${grid.x_min ?? "-"}..${grid.x_max ?? "-"}m`, left, height - 12);
  ctx.textAlign = "right";
  ctx.fillText(`Y ${grid.y_min ?? "-"}..${grid.y_max ?? "-"}m`, width - right, height - 12);

  const latest = data.latest_window;
  if (latest?.dominant_cell_x_m !== null && latest?.dominant_cell_y_m !== null) {
    const xMin = Number(grid.x_min);
    const yMin = Number(grid.y_min);
    const cellSize = Number(grid.cell_size || 0.5);
    const markerX = left + ((Number(latest.dominant_cell_x_m) - xMin) / cellSize) * cellWidth;
    const markerY = top + plotHeight - ((Number(latest.dominant_cell_y_m) - yMin) / cellSize) * cellHeight;
    ctx.beginPath();
    ctx.arc(markerX, markerY, 7, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.strokeStyle = "#ffcf4a";
    ctx.lineWidth = 3;
    ctx.stroke();
  }
}

function renderSpatial(data) {
  spatialState = data;
  const metricSelect = $("spatialMetric");
  const modeSelect = $("spatialMode");
  if (metricSelect) metricSelect.value = spatialMetric;
  if (modeSelect) modeSelect.value = spatialMode;

  if (!data?.available) {
    $("spatialMeta").textContent = "grid 없음";
    $("spatialSource").textContent = "-";
    $("spatialStats").innerHTML = `<div class="node-row"><span>상태</span><b>grid window CSV 없음</b></div>`;
    $("spatialWindows").innerHTML = `<tr><td colspan="6">공간 데이터 없음</td></tr>`;
    drawSpatialHeatmap(data);
    return;
  }

  const totals = data.totals || {};
  const latest = data.latest_window || {};
  $("spatialMeta").textContent = `${data.file} · ${data.row_count} windows`;
  $("spatialSource").textContent = `${data.source} · ${data.mtime || "-"}`;
  const rows = [
    ["데이터", `${data.source || "-"} · ${data.file || "-"}`],
    ["Grid", `${data.grid?.x_cells || "-"} x ${data.grid?.y_cells || "-"} · cell ${fmt(data.grid?.cell_size, 2)}m`],
    ["누적 포인트", totals.point_total ?? 0],
    ["누적 타깃", totals.target_total ?? 0],
    ["누적 VITAL", totals.vital_total ?? 0],
    ["Presence 평균", percent(totals.presence_ratio_mean || 0)],
    ["최신 우세셀", `${fmt(latest.dominant_cell_x_m, 2)}, ${fmt(latest.dominant_cell_y_m, 2)}m`],
  ];
  $("spatialStats").innerHTML = rows
    .map(([label, value]) => `<div class="node-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`)
    .join("");

  const windows = (data.windows || []).slice().reverse();
  $("spatialWindows").innerHTML = windows.length
    ? windows
      .map((row) => `
        <tr>
          <td>${row.window_index ?? "-"}</td>
          <td>${fmt(row.window_start_sec, 1)}-${fmt(row.window_end_sec, 1)}s</td>
          <td>${row.point_total ?? 0}</td>
          <td>${row.target_total ?? 0}</td>
          <td>${row.vital_total ?? 0}</td>
          <td>${fmt(row.dominant_cell_x_m, 2)}, ${fmt(row.dominant_cell_y_m, 2)}</td>
        </tr>
      `)
      .join("")
    : `<tr><td colspan="6">window 없음</td></tr>`;
  drawSpatialHeatmap(data);
}

function renderVitalsTable(rows) {
  const body = $("recentVitals");
  const items = (rows || []).slice().reverse();
  $("vitalRowsMeta").textContent = `${items.length} rows`;
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="6">VITAL 행 없음</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (row) => `
        <tr>
          <td>${row.frame ?? "-"}</td>
          <td>${fmt(row.heart_rate, 1)}</td>
          <td>${fmt(row.breath_rate, 1)}</td>
          <td>${fmt(row.breath_deviation, 4)}</td>
          <td>${escapeHtml(row.range_bin || "-")}</td>
          <td>${escapeHtml(row.target_id || "-")}</td>
        </tr>
      `
    )
    .join("");
}

function renderRadarTable(rows) {
  const body = $("recentRadar");
  const items = (rows || []).slice().reverse();
  $("radarRowsMeta").textContent = `${items.length} rows`;
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="6">RADAR 행 없음</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (row) => `
        <tr>
          <td>${row.frame ?? "-"}</td>
          <td>${row.has_vital ? "YES" : "NO"}</td>
          <td>${row.num_detected_obj ?? "-"}</td>
          <td>${row.num_tlvs ?? "-"}</td>
          <td>${row.packet_len ?? "-"}</td>
          <td>${escapeHtml(row.tlv_summary || "-")}</td>
        </tr>
      `
    )
    .join("");
}

function render(data, spatial) {
  vitalsState = data;
  $("clockNow").textContent = data.now || "-";
  $("pageState").textContent = data.collection_running ? "수집중" : "대기";
  $("collectBtn").textContent = data.collection_running ? "수집 중지" : "수집 시작";
  $("monitorBtn").textContent = data.monitor_running ? "자동 모니터 중지" : "자동 모니터 시작";
  $("monitorBtn").classList.toggle("primary", Boolean(data.monitor_running));
  renderNodeSelect(data.nodes || []);
  const node = ensureSelectedNode();
  if (node) {
    $("nodeSelect").value = node.sensor_id;
  }
  renderScoreboard(node);
  renderEdge(node);
  renderNodeMeta(node);
  renderVitalsTable(node?.recent_vitals || []);
  renderRadarTable(node?.recent_rows || []);
  drawChart(node?.series || []);
  renderSpatial(spatial);
  $("chartMeta").textContent = node
    ? `${node.sensor_id} · VITAL ${node.tail_vital_rows || 0}/${node.tail_rows || 0}`
    : "노드 없음";
}

async function loadVitals() {
  if (loading) return;
  loading = true;
  try {
    const [data, spatial] = await Promise.all([
      api("/api/vitals"),
      api("/api/spatial")
    ]);
    render(data, spatial);
  } catch (error) {
    $("pageState").textContent = "오류";
    console.error(error);
  } finally {
    loading = false;
  }
}

async function runAction(label, action) {
  showToast(label);
  try {
    await action();
    await loadVitals();
  } catch (error) {
    alert(error.message);
  } finally {
    hideToast();
  }
}

function selectedSensor() {
  const node = ensureSelectedNode();
  return node?.sensor_id || "";
}

function bind() {
  $("nodeSelect").addEventListener("change", (event) => {
    selectedSensorId = event.target.value;
    localStorage.setItem("hanium.vitals.sensor", selectedSensorId);
    render(vitalsState);
  });
  $("collectBtn").addEventListener("click", () => {
    runAction("수집 상태 변경중", () => api("/api/collect", "POST"));
  });
  $("monitorBtn").addEventListener("click", () => {
    runAction("자동 모니터 상태 변경중", () => api("/api/monitor", "POST"));
  });
  $("resultBtn").addEventListener("click", () => {
    const sensorId = selectedSensor();
    if (!sensorId) {
      alert("선택된 센서가 없습니다.");
      return;
    }
    runAction(`${sensorId} ESP32 결과 읽는중`, () => api("/api/nodes/read-result", "POST", { sensor_id: sensorId }));
  });
  $("refreshBtn").addEventListener("click", () => {
    runAction("새로고침", loadVitals);
  });
  $("spatialMetric").addEventListener("change", (event) => {
    spatialMetric = event.target.value;
    localStorage.setItem("hanium.vitals.spatialMetric", spatialMetric);
    renderSpatial(spatialState);
  });
  $("spatialMode").addEventListener("change", (event) => {
    spatialMode = event.target.value;
    localStorage.setItem("hanium.vitals.spatialMode", spatialMode);
    renderSpatial(spatialState);
  });
  window.addEventListener("resize", () => {
    const node = ensureSelectedNode();
    drawChart(node?.series || []);
    drawSpatialHeatmap(spatialState);
  });
}

bind();
loadVitals();
setInterval(loadVitals, 1800);
