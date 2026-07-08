#!/usr/bin/env python3
import csv
from collections import Counter
from datetime import datetime, timezone
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import random
import subprocess
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
CONFIG_ROOT = APP_ROOT / "config"
RUNTIME_ROOT = APP_ROOT / "runtime"
PROFILE_ROOT = APP_ROOT / "profiles"
NODES_PATH = CONFIG_ROOT / "nodes.json"
REFERENCE_DATA_ROOT = APP_ROOT / "reference_data"
DEFAULT_DATA_DIR = Path(os.environ.get("HANIUM_DATA_DIR", str(REFERENCE_DATA_ROOT)))
DEFAULT_MODEL = os.environ.get("HANIUM_OLLAMA_MODEL", "qwen3:14b")
OLLAMA_URL = os.environ.get("HANIUM_OLLAMA_URL", "http://localhost:11434/api/chat")
BUNDLED_RADAR_SCRIPT = APP_ROOT / "radar_to_esp32.py"
RADAR_SCRIPT = Path(os.environ.get("HANIUM_RADAR_SCRIPT", str(BUNDLED_RADAR_SCRIPT)))

CONFIG_ROOT.mkdir(exist_ok=True)
RUNTIME_ROOT.mkdir(exist_ok=True)
PROFILE_ROOT.mkdir(exist_ok=True)
COLLECTION_PROCS = {}
SERIAL_LOCK = threading.Lock()
MONITOR_THREAD = None
MONITOR_STOP = threading.Event()


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path, payload):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_float(value):
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def parse_int(value):
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else None


def average(values):
    clean = [value for value in values if value is not None]
    return round(statistics.fmean(clean), 6) if clean else None


def percentile(values, ratio):
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    index = min(len(clean) - 1, max(0, round((len(clean) - 1) * ratio)))
    return round(clean[index], 6)


def compact_counter(counter, limit=10):
    return [{"value": str(value), "count": count} for value, count in counter.most_common(limit)]


def default_node(sensor_id="sensor_01"):
    return {
        "sensor_id": sensor_id,
        "name": "기본 센서 노드",
        "location_id": "unknown_position",
        "enabled": True,
        "connection_mode": "pc_iwr6843_usb",
        "board": "ESP32-S3",
        "sensor": "IWR6843AOPEVM",
        "cli_port": "",
        "data_port": "",
        "esp_port": "",
        "data_baud": 921600,
        "esp_baud": 115200,
        "cfg_path": "",
        "cfg_name": "vital_signs_AOP_6m.cfg",
        "notes": "내일 보드 연결 후 포트 스캔으로 COM/USB 포트를 선택",
        "status": "대기",
        "connection": "포트 미설정",
        "profile_status": "미배포",
        "last_result": None,
    }


def normalize_node(raw, fallback_id="sensor_01"):
    node = default_node(fallback_id)
    if isinstance(raw, dict):
        node.update({key: value for key, value in raw.items() if value is not None})
    node["sensor_id"] = str(node.get("sensor_id") or fallback_id).strip()
    node["name"] = str(node.get("name") or node["sensor_id"]).strip()
    node["location_id"] = str(node.get("location_id") or "unknown_position").strip()
    node["enabled"] = bool(node.get("enabled", True))
    node["connection_mode"] = str(node.get("connection_mode") or "pc_iwr6843_usb")
    node["board"] = str(node.get("board") or "ESP32-S3")
    node["sensor"] = str(node.get("sensor") or "IWR6843AOPEVM")
    for key in ["cli_port", "data_port", "esp_port", "cfg_path", "cfg_name", "notes"]:
        node[key] = str(node.get(key) or "").strip()
    for key, default in [("data_baud", 921600), ("esp_baud", 115200)]:
        try:
            node[key] = int(node.get(key) or default)
        except (TypeError, ValueError):
            node[key] = default
    if not node["enabled"]:
        node["status"] = "비활성"
    elif node.get("status") == "비활성":
        node["status"] = "대기"
    elif not node_readiness(node)["missing"] and node.get("status") in {"포트필요", "포트 설정 필요"}:
        node["status"] = "대기"
    if not node.get("esp_port") and node.get("profile_status") == "시뮬레이션":
        node["status"] = "시뮬레이션"
    node["connection"] = connection_label(node)
    node.setdefault("profile_status", "미배포")
    node.setdefault("last_result", None)
    return node


def node_readiness(node):
    mode = node.get("connection_mode")
    if not node.get("enabled", True):
        return {"state": "disabled", "label": "비활성", "missing": []}
    if mode == "pc_iwr6843_usb":
        missing = []
        if not node.get("cli_port"):
            missing.append("CLI 포트")
        if not node.get("data_port"):
            missing.append("DATA 포트")
        if not node.get("esp_port"):
            missing.append("ESP32 포트")
        return {
            "state": "ready" if not missing else "needs_ports",
            "label": "연결준비 완료" if not missing else "포트 설정 필요",
            "missing": missing,
        }
    missing = []
    if not node.get("esp_port"):
        missing.append("ESP32 포트")
    return {
        "state": "ready" if not missing else "needs_ports",
        "label": "연결준비 완료" if not missing else "ESP32 포트 필요",
        "missing": missing,
    }


def connection_label(node):
    readiness = node_readiness({**node, "connection": ""})
    if readiness["state"] == "disabled":
        return "비활성"
    ports = []
    for label, key in [("CLI", "cli_port"), ("DATA", "data_port"), ("ESP", "esp_port")]:
        if node.get(key):
            ports.append(f"{label}:{node[key]}")
    if ports:
        return " / ".join(ports)
    return readiness["label"]


def load_nodes():
    saved = load_json(NODES_PATH)
    if isinstance(saved, dict):
        saved = saved.get("nodes")
    if not isinstance(saved, list) or not saved:
        nodes = [default_node("sensor_01")]
        return save_nodes(nodes)
    seen = set()
    nodes = []
    for index, item in enumerate(saved, start=1):
        fallback_id = f"sensor_{index:02d}"
        node = normalize_node(item, fallback_id=fallback_id)
        if node["sensor_id"] in seen:
            node["sensor_id"] = fallback_id
        seen.add(node["sensor_id"])
        nodes.append(node)
    return nodes


def save_nodes(nodes):
    clean = [normalize_node(node, fallback_id=f"sensor_{index:02d}") for index, node in enumerate(nodes, start=1)]
    save_json(NODES_PATH, {"updated_at": utc_now(), "nodes": clean})
    return clean


def next_sensor_id(nodes):
    used = {node.get("sensor_id") for node in nodes}
    number = 1
    while True:
        candidate = f"sensor_{number:02d}"
        if candidate not in used:
            return candidate
        number += 1


def scan_serial_ports():
    ports = []
    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            ports.append(
                {
                    "device": port.device,
                    "description": port.description,
                    "hwid": port.hwid,
                }
            )
    except Exception:
        patterns = ["/dev/cu.*", "/dev/tty.*"]
        for pattern in patterns:
            for device in glob.glob(pattern):
                ports.append({"device": device, "description": "serial device", "hwid": ""})

    unique = {}
    for port in ports:
        device = port.get("device")
        if device:
            unique[device] = port
    return sorted(unique.values(), key=lambda item: item["device"])


def collection_job_snapshot():
    jobs = []
    for sensor_id, job in list(COLLECTION_PROCS.items()):
        proc = job.get("process")
        return_code = proc.poll() if proc else None
        running = return_code is None
        if not running and not job.get("closed"):
            handle = job.get("log_handle")
            if handle:
                try:
                    handle.write(f"--- {utc_now()} EXIT {return_code} ---\n")
                    handle.close()
                except OSError:
                    pass
            job["closed"] = True
        jobs.append(
            {
                "sensor_id": sensor_id,
                "pid": proc.pid if proc else None,
                "running": running,
                "return_code": return_code,
                "cmd": job.get("cmd", []),
                "log_path": str(job.get("log_path", "")),
                "csv_path": str(job.get("csv_path", "")),
                "started_at": job.get("started_at"),
            }
        )
        if not running:
            del COLLECTION_PROCS[sensor_id]

    if "STATE" in globals():
        running_now = any(job["running"] for job in jobs)
        completed = [job for job in jobs if not job["running"]]
        with STATE.lock:
            STATE.collection_running = running_now
            for job in completed:
                node = find_node(job["sensor_id"])
                if node and node.get("enabled", True):
                    node["status"] = "대기" if job["return_code"] == 0 else "수집오류"
                    if job["return_code"] not in (0, None):
                        node["last_collection_error"] = {
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "return_code": job["return_code"],
                            "log_path": job["log_path"],
                            "csv_path": job["csv_path"],
                        }
            if completed:
                STATE.nodes = save_nodes(STATE.nodes)
    return jobs


def safe_int(value):
    parsed = parse_int(value)
    return parsed if parsed is not None else 0


def is_vital_row(row):
    return str(row.get("has_vital", "")).lower() in {"1", "true", "yes"}


def file_mtime_label(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def latest_collection_csv(sensor_id):
    candidates = []
    with STATE.lock:
        node = find_node(sensor_id)
        if node and node.get("collection_csv"):
            candidates.append(Path(node["collection_csv"]))
    for job in collection_job_snapshot():
        if job.get("sensor_id") == sensor_id and job.get("csv_path"):
            candidates.append(Path(job["csv_path"]))
    candidates.extend(sorted(RUNTIME_ROOT.glob(f"collection_{sensor_id}_*.csv"), key=lambda path: path.stat().st_mtime, reverse=True))

    seen = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.exists() and path.is_file():
            return path
    return None


def read_csv_tail(path, limit=700, max_bytes=768 * 1024):
    if not path or not path.exists():
        return []
    size = path.stat().st_size
    if size <= 0:
        return []
    with path.open("rb") as handle:
        if size <= max_bytes:
            text = handle.read().decode("utf-8-sig", errors="replace")
        else:
            header = handle.readline().decode("utf-8-sig", errors="replace").strip()
            handle.seek(max(0, size - max_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
            tail_lines = tail.splitlines()
            if tail_lines:
                tail_lines = tail_lines[1:]
            text = header + "\n" + "\n".join(tail_lines)
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows[-limit:]


def compact_vital_row(row):
    return {
        "timestamp": parse_float(row.get("timestamp")),
        "session_id": row.get("session_id", ""),
        "label": row.get("label", ""),
        "cfg": row.get("cfg", ""),
        "frame": safe_int(row.get("frame")),
        "num_detected_obj": safe_int(row.get("num_detected_obj")),
        "num_tlvs": safe_int(row.get("num_tlvs")),
        "packet_len": safe_int(row.get("packet_len")),
        "tlv_summary": row.get("tlv_summary", ""),
        "has_vital": is_vital_row(row),
        "target_id": row.get("target_id", ""),
        "range_bin": row.get("range_bin", ""),
        "breath_deviation": parse_float(row.get("breath_deviation")),
        "heart_rate": parse_float(row.get("heart_rate")),
        "breath_rate": parse_float(row.get("breath_rate")),
    }


def vital_status(latest_row, latest_vital, recent_vital_ratio):
    if not latest_row:
        return {"state": "NO_DATA", "label": "데이터 없음", "severity": "bad"}
    if not latest_vital:
        return {"state": "NO_VITAL", "label": "VITAL 없음", "severity": "bad"}
    frame_gap = safe_int(latest_row.get("frame")) - safe_int(latest_vital.get("frame"))
    heart = parse_float(latest_vital.get("heart_rate"))
    breath = parse_float(latest_vital.get("breath_rate"))
    breath_dev = parse_float(latest_vital.get("breath_deviation"))
    if frame_gap > 90:
        return {"state": "STALE", "label": f"VITAL 지연 {frame_gap}f", "severity": "warn"}
    if heart is None or breath is None or heart <= 0 or breath <= 0:
        return {"state": "INVALID", "label": "값 불안정", "severity": "warn"}
    if recent_vital_ratio < 0.02:
        return {"state": "SPARSE", "label": "VITAL 희박", "severity": "warn"}
    if breath_dev is not None and breath_dev >= 0.02:
        return {"state": "TRACKING", "label": "생체신호 추적", "severity": "good"}
    return {"state": "WEAK", "label": "신호 약함", "severity": "warn"}


def median_value(values):
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return round(clean[mid], 6)
    return round((clean[mid - 1] + clean[mid]) / 2, 6)


def clamp(value, low=0.0, high=1.0):
    return min(high, max(low, value))


def finite_row_value(row, key):
    value = parse_float(row.get(key))
    return value if value is not None and math.isfinite(value) else None


def pc_live_detection(sensor_id, rows, vital_source_rows, latest_row, latest_vital, recent_vital_ratio):
    """Rule-based live decision calibrated from the older positive CSVs.

    The old usable samples had VITAL ratios around 5-9%. The previous profile
    used 8% as a hard threshold, which misses valid weaker but repeated VITAL
    windows. This decision keeps the lower ratio threshold but requires repeated
    VITAL rows, plausible HR/RR, fresh data, and a reasonably stable range bin.
    """
    now_label = datetime.now().strftime("%H:%M:%S")
    if not rows or not latest_row:
        return {
            "time": now_label,
            "sensor_id": sensor_id,
            "source": "pc_vital_window",
            "profile_version": "pc-rule-v2-old-data",
            "status": "NO_DATA",
            "person_count": 0,
            "survivor_candidate": False,
            "confidence": 0.0,
            "simulated": False,
            "reason_ko": "수집 CSV 데이터가 없습니다.",
            "metrics": {},
        }

    latest_frame = safe_int(latest_row.get("frame"))
    latest_vital_frame = safe_int(latest_vital.get("frame")) if latest_vital else -1
    frame_gap = latest_frame - latest_vital_frame if latest_vital else 9999
    recent_window = rows[-180:]
    recent_vitals = [row for row in recent_window if is_vital_row(row)]
    valid_vitals = []
    for row in recent_vitals:
        heart = finite_row_value(row, "heart_rate")
        breath = finite_row_value(row, "breath_rate")
        breath_dev = finite_row_value(row, "breath_deviation")
        if heart is None or breath is None or breath_dev is None:
            continue
        if 35 <= heart <= 130 and 4 <= breath <= 35 and breath_dev >= 0:
            valid_vitals.append(row)

    breath_devs = [finite_row_value(row, "breath_deviation") for row in valid_vitals]
    heart_rates = [finite_row_value(row, "heart_rate") for row in valid_vitals]
    breath_rates = [finite_row_value(row, "breath_rate") for row in valid_vitals]
    breath_dev_median = median_value(breath_devs)
    breath_dev_p75 = percentile(breath_devs, 0.75)
    heart_median = median_value(heart_rates)
    breath_median = median_value(breath_rates)
    range_counter = Counter(row.get("range_bin") for row in valid_vitals if row.get("range_bin") not in (None, ""))
    top_range, top_range_count = ("", 0)
    if range_counter:
        top_range, top_range_count = range_counter.most_common(1)[0]
    range_stability = round(top_range_count / len(valid_vitals), 4) if valid_vitals else 0.0

    thresholds = {
        "window_rows": len(recent_window),
        "vital_ratio_min": 0.045,
        "breath_dev_min": 0.025,
        "valid_vital_min": 3,
        "range_stability_min": 0.30,
        "fresh_frame_gap_max": 96,
    }
    fresh = frame_gap <= thresholds["fresh_frame_gap_max"]
    enough_vitals = len(valid_vitals) >= thresholds["valid_vital_min"]
    enough_ratio = recent_vital_ratio >= thresholds["vital_ratio_min"]
    enough_breath_dev = (breath_dev_p75 or 0) >= thresholds["breath_dev_min"]
    stable_range = range_stability >= thresholds["range_stability_min"] or top_range_count >= 3

    vital_score = clamp(recent_vital_ratio / 0.09)
    breath_score = clamp((breath_dev_p75 or 0) / 0.10)
    range_score = clamp(range_stability)
    recency_score = clamp(1.0 - max(0, frame_gap) / thresholds["fresh_frame_gap_max"])
    confidence = round(
        clamp(0.28 * vital_score + 0.34 * breath_score + 0.24 * range_score + 0.14 * recency_score),
        3,
    )

    detected = fresh and enough_vitals and enough_ratio and enough_breath_dev and stable_range
    weak = fresh and enough_vitals and (enough_ratio or enough_breath_dev)
    if detected:
        status = "SURVIVOR_CANDIDATE"
        reason = "최근 VITAL 반복, 호흡편차, rangeBin 안정성이 모두 기준 이상입니다."
    elif weak:
        status = "WEAK_VITAL"
        reason = "생체신호는 보이지만 반복성 또는 rangeBin 안정성이 부족합니다."
    else:
        status = "CLEAR"
        reason = "최근 윈도우에서 생체신호 반복성이 기준보다 낮습니다."

    return {
        "time": now_label,
        "sensor_id": sensor_id,
        "source": "pc_vital_window",
        "profile_version": "pc-rule-v2-old-data",
        "status": status,
        "person_count": 1 if detected else 0,
        "survivor_candidate": bool(detected),
        "confidence": confidence,
        "simulated": False,
        "reason_ko": reason,
        "metrics": {
            "window_rows": len(recent_window),
            "recent_vital_rows": len(recent_vitals),
            "valid_vital_rows": len(valid_vitals),
            "recent_vital_ratio": recent_vital_ratio,
            "frame_gap": frame_gap,
            "breath_deviation_median": breath_dev_median,
            "breath_deviation_p75": breath_dev_p75,
            "heart_rate_median": heart_median,
            "breath_rate_median": breath_median,
            "range_bin_mode": top_range,
            "range_bin_mode_count": top_range_count,
            "range_stability": range_stability,
            "thresholds": thresholds,
        },
    }


def node_vitals_snapshot(node):
    sensor_id = node["sensor_id"]
    path = latest_collection_csv(sensor_id)
    rows = read_csv_tail(path)
    recent = [compact_vital_row(row) for row in rows]
    vital_source_rows = [row for row in rows if is_vital_row(row)]
    vital_rows = [compact_vital_row(row) for row in vital_source_rows]
    latest_row = rows[-1] if rows else None
    latest_vital = vital_source_rows[-1] if vital_source_rows else None
    recent_window = rows[-180:] if rows else []
    recent_vital_count = len([row for row in recent_window if is_vital_row(row)])
    recent_vital_ratio = round(recent_vital_count / len(recent_window), 4) if recent_window else 0.0
    series = vital_rows[-80:]
    latest_result = node.get("last_result")
    result_raw = latest_result.get("raw", {}) if isinstance(latest_result, dict) else {}
    status = vital_status(latest_row, latest_vital, recent_vital_ratio)
    pc_detection = pc_live_detection(sensor_id, rows, vital_source_rows, latest_row, latest_vital, recent_vital_ratio)
    file_size = path.stat().st_size if path and path.exists() else 0

    return {
        "sensor_id": sensor_id,
        "name": node.get("name", sensor_id),
        "location_id": node.get("location_id", ""),
        "enabled": node.get("enabled", True),
        "status": node.get("status", ""),
        "profile_status": node.get("profile_status", ""),
        "connection": node.get("connection", ""),
        "csv_path": str(path) if path else "",
        "csv_exists": bool(path and path.exists()),
        "csv_size": file_size,
        "csv_mtime": file_mtime_label(path) if path else None,
        "tail_rows": len(rows),
        "tail_vital_rows": len(vital_rows),
        "recent_vital_ratio": recent_vital_ratio,
        "latest_row": compact_vital_row(latest_row) if latest_row else None,
        "latest_vital": compact_vital_row(latest_vital) if latest_vital else None,
        "status_signal": status,
        "pc_detection": pc_detection,
        "series": series,
        "recent_rows": recent[-30:],
        "recent_vitals": vital_rows[-20:],
        "edge_result": latest_result,
        "edge_raw": result_raw,
    }


def action_vitals():
    jobs = collection_job_snapshot()
    with STATE.lock:
        nodes = [dict(node) for node in STATE.nodes if node.get("enabled", True)]
        app_status = {
            "collection_running": STATE.collection_running,
            "monitor_running": STATE.monitor_running,
            "monitor_last_poll": STATE.monitor_last_poll,
            "monitor_poll_count": STATE.monitor_poll_count,
            "monitor_error_count": STATE.monitor_error_count,
            "last_result_tick": STATE.last_result_tick,
        }
    node_items = [node_vitals_snapshot(node) for node in nodes]
    total_rows = sum(item["tail_rows"] for item in node_items)
    total_vitals = sum(item["tail_vital_rows"] for item in node_items)
    return {
        "ok": True,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **app_status,
        "node_count": len(node_items),
        "tail_rows": total_rows,
        "tail_vital_rows": total_vitals,
        "tail_vital_ratio": round(total_vitals / total_rows, 4) if total_rows else 0.0,
        "collection_jobs": jobs,
        "nodes": node_items,
    }


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.data_dir = DEFAULT_DATA_DIR
        self.model = DEFAULT_MODEL
        self.collection_running = False
        self.monitor_running = False
        self.monitor_interval_seconds = float(os.environ.get("HANIUM_MONITOR_INTERVAL", "2.0"))
        self.monitor_last_poll = None
        self.monitor_poll_count = 0
        self.monitor_error_count = 0
        self.summary = load_json(RUNTIME_ROOT / "last_summary.json")
        self.profile = load_json(PROFILE_ROOT / "last_profile_batch.json")
        self.validation = None
        self.llm_meta = None
        self.last_result_tick = None
        self.logs = []
        self.nodes = load_nodes()
        if self.profile:
            node_ids = {node["sensor_id"] for node in self.nodes if node.get("enabled", True)}
            profile_ids = {
                item.get("sensor_id")
                for item in self.profile.get("profile_batch", {}).get("sensor_profiles", [])
            }
            if profile_ids != node_ids:
                self.profile = None
                self.add_log("SYSTEM", "노드 구성 변경으로 이전 LLM profile 무시")
        if self.summary:
            self.add_log("SYSTEM", "이전 CSV 요약 자동 로드")
        if self.profile:
            self.add_log("SYSTEM", "이전 LLM profile 자동 로드")
        self.add_log("SYSTEM", f"센서 노드 {len(self.nodes)}개 로드")
        self.add_log("SYSTEM", "대시보드 초기화 완료")

    def add_log(self, kind, message):
        self.logs.insert(
            0,
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "kind": kind,
                "message": message,
            },
        )
        self.logs = self.logs[:80]

    def snapshot(self):
        with self.lock:
            return {
                "app": "HANIIUM Radar Local LLM Control",
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data_dir": str(self.data_dir),
                "model": self.model,
                "collection_running": self.collection_running,
                "monitor_running": self.monitor_running,
                "monitor_interval_seconds": self.monitor_interval_seconds,
                "monitor_last_poll": self.monitor_last_poll,
                "monitor_poll_count": self.monitor_poll_count,
                "monitor_error_count": self.monitor_error_count,
                "nodes": self.nodes,
                "node_count": len(self.nodes),
                "enabled_node_count": len([node for node in self.nodes if node.get("enabled", True)]),
                "summary": self.summary,
                "profile": self.profile,
                "validation": self.validation,
                "llm_meta": self.llm_meta,
                "last_result_tick": self.last_result_tick,
                "collection_jobs": collection_job_snapshot(),
                "logs": self.logs,
            }


STATE = AppState()


def summarize_csv_file(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    labels = {}
    phase_labels = {}
    sessions = {}
    vital_rows = 0
    detected_objects = []
    breath_devs = []
    heart_rates = []
    breath_rates = []
    range_bins = {}
    tlv1040_rows = 0

    for row in rows:
        label = row.get("label") or row.get("main_label") or "unlabeled"
        labels[label] = labels.get(label, 0) + 1
        phase = row.get("phase_label")
        if phase:
            phase_labels[phase] = phase_labels.get(phase, 0) + 1
        session = row.get("session_id")
        if session:
            sessions[session] = sessions.get(session, 0) + 1

        detected_objects.append(parse_int(row.get("num_detected_obj")))
        if "1040:" in (row.get("tlv_summary") or ""):
            tlv1040_rows += 1

        has_vital = str(row.get("has_vital", "")).lower() in {"1", "true", "yes"}
        if has_vital:
            vital_rows += 1
            breath_devs.append(parse_float(row.get("breath_deviation")))
            heart_rates.append(parse_float(row.get("heart_rate")))
            breath_rates.append(parse_float(row.get("breath_rate")))
            range_bin = row.get("range_bin")
            if range_bin not in (None, ""):
                range_bins[range_bin] = range_bins.get(range_bin, 0) + 1

    sorted_labels = sorted(labels.items(), key=lambda item: item[1], reverse=True)
    sorted_ranges = sorted(range_bins.items(), key=lambda item: item[1], reverse=True)
    return {
        "file": path.name,
        "rows": len(rows),
        "labels": [{"value": key, "count": value} for key, value in sorted_labels[:10]],
        "phase_labels": [{"value": key, "count": value} for key, value in sorted(phase_labels.items(), key=lambda item: item[1], reverse=True)[:10]],
        "sessions": [{"value": key, "count": value} for key, value in sorted(sessions.items(), key=lambda item: item[1], reverse=True)[:8]],
        "vital_rows": vital_rows,
        "vital_ratio": round(vital_rows / len(rows), 6) if rows else 0.0,
        "tlv1040_rows": tlv1040_rows,
        "num_detected_obj_mean": average(detected_objects),
        "num_detected_obj_p90": percentile(detected_objects, 0.9),
        "num_detected_obj_max": max([value for value in detected_objects if value is not None], default=None),
        "breath_deviation_p50": percentile(breath_devs, 0.5),
        "breath_deviation_p90": percentile(breath_devs, 0.9),
        "heart_rate_mean": average(heart_rates),
        "breath_rate_mean": average(breath_rates),
        "range_bin_modes": [{"value": key, "count": value} for key, value in sorted_ranges[:5]],
    }


def summarize_dataset(data_dir):
    csv_paths = sorted(Path(data_dir).glob("*.csv"))
    files = [summarize_csv_file(path) for path in csv_paths]
    label_counts = {}
    total_rows = 0
    total_vital_rows = 0

    for item in files:
        total_rows += item["rows"]
        total_vital_rows += item["vital_rows"]
        for label in item["labels"]:
            label_counts[label["value"]] = label_counts.get(label["value"], 0) + label["count"]

    missing = []
    for label in ["no_person", "person_moving"]:
        if label not in label_counts:
            missing.append(label)

    labels = [{"value": key, "count": value} for key, value in sorted(label_counts.items(), key=lambda item: item[1], reverse=True)]
    return {
        "generated_at": utc_now(),
        "data_dir": str(data_dir),
        "csv_file_count": len(files),
        "total_rows": total_rows,
        "total_vital_rows": total_vital_rows,
        "overall_vital_ratio": round(total_vital_rows / total_rows, 6) if total_rows else 0,
        "labels": labels,
        "data_quality_flags": {
            "missing_baseline_labels": missing,
            "small_multiclass_dataset": total_rows < 10000 or len(labels) < 5,
            "profile_should_be_trial_only": bool(missing),
        },
        "files": files,
    }


def build_llm_messages(summary, nodes):
    schema = {
        "agent_role": "local_llm_calibration_agent",
        "profile_batch": {
            "profile_version": "local-agent-v1",
            "deployment_decision": "trial_only | collect_more_data | deploy_candidate",
            "global_rationale_ko": "짧은 한국어 판단 근거",
            "sensor_profiles": [
                {
                    "sensor_id": "sensor_01",
                    "location_id": "entrance_left",
                    "profile": {
                        "mode": "vital_detect",
                        "window_seconds": 8,
                        "breath_dev_min": 0.025,
                        "vital_ratio_min": 0.045,
                        "confirm_seconds": 4,
                        "lost_grace_seconds": 4,
                        "moving_reject": True,
                        "confidence_policy": "sensitive | balanced | conservative",
                    },
                    "rationale_ko": "이 센서에 맞춘 이유",
                    "risk_notes_ko": ["위험 1"],
                    "required_next_data": ["다음 수집할 데이터"],
                }
            ],
            "next_experiments": ["다음 실험"],
        },
    }
    system = (
        "You are the local LLM calibration agent in a rescue radar project. "
        "You analyze radar CSV summaries, decide whether data is enough, and generate ESP32-executable profiles. "
        "Return valid JSON only. Do not include markdown. Keep values inside the allowed ranges."
    )
    user = (
        "Generate calibration profiles for the listed ESP32 sensor nodes. "
        "ESP32 will not run an LLM; it will only execute lightweight threshold/window logic. "
        "If no_person/person_moving baselines are missing, mark deployment_decision as trial_only or collect_more_data. "
        "Known positive CSVs in this project often have vital_ratio around 0.05..0.09, so do not choose 0.08+ as a hard "
        "minimum unless the sensor has a strong no_person baseline. Prefer 0.04..0.06 trial thresholds with range-bin stability checks. "
        "Allowed numeric ranges: window_seconds 5..12, breath_dev_min 0.005..0.08, vital_ratio_min 0.02..0.5, "
        "confirm_seconds 3..15, lost_grace_seconds 1..8.\n\n"
        f"Required JSON shape:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Sensor nodes:\n{json.dumps(nodes, ensure_ascii=False, indent=2)}\n\n"
        f"Dataset summary:\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def call_ollama(model, messages):
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.15, "top_p": 0.9, "num_predict": 1800},
    }
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=360) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
        return json.loads(text[start : end + 1])


def validate_profile_batch(profile):
    errors = []
    warnings = []
    batch = profile.get("profile_batch") if isinstance(profile, dict) else None
    if not isinstance(batch, dict):
        return {"ok": False, "errors": ["profile_batch가 없습니다."], "warnings": warnings}
    sensor_profiles = batch.get("sensor_profiles")
    if not isinstance(sensor_profiles, list) or not sensor_profiles:
        errors.append("sensor_profiles가 비어 있습니다.")
    for item in sensor_profiles or []:
        sensor_id = item.get("sensor_id", "unknown")
        cfg = item.get("profile", {})
        ranges = {
            "window_seconds": (5, 12),
            "breath_dev_min": (0.005, 0.08),
            "vital_ratio_min": (0.02, 0.5),
            "confirm_seconds": (3, 15),
            "lost_grace_seconds": (1, 8),
        }
        for key, (low, high) in ranges.items():
            value = cfg.get(key)
            if not isinstance(value, (int, float)):
                errors.append(f"{sensor_id}: {key} 숫자값이 아닙니다.")
            elif value < low or value > high:
                errors.append(f"{sensor_id}: {key}={value} 허용 범위 {low}..{high} 밖입니다.")
        if cfg.get("confidence_policy") not in {"sensitive", "balanced", "conservative"}:
            errors.append(f"{sensor_id}: confidence_policy 값이 잘못됐습니다.")
        if not isinstance(cfg.get("moving_reject"), bool):
            errors.append(f"{sensor_id}: moving_reject는 boolean이어야 합니다.")
    if batch.get("deployment_decision") == "deploy_candidate":
        warnings.append("deploy_candidate는 실제 no_person/person_moving 데이터 검증 후만 사용하세요.")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def generate_simulated_result(node, profile, summary):
    cfg = profile.get("profile", {}) if profile else {}
    vital_ratio = summary.get("overall_vital_ratio", 0.06) if summary else 0.06
    strictness = cfg.get("breath_dev_min", 0.02) * 8 + cfg.get("vital_ratio_min", 0.08)
    base = min(0.94, max(0.08, vital_ratio * 5.2 + random.uniform(-0.05, 0.08)))
    confidence = min(0.98, max(0.02, base - strictness * 0.15))
    detected = confidence >= 0.45
    if cfg.get("confidence_policy") == "conservative":
        detected = confidence >= 0.55
    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "sensor_id": node["sensor_id"],
        "person_count": 1 if detected else 0,
        "survivor_candidate": detected,
        "confidence": round(confidence, 3),
        "status": "SURVIVOR_CANDIDATE" if detected else "CLEAR",
        "profile_version": profile.get("profile_version", "none") if profile else "none",
    }


def parse_result_line(line):
    prefix = "RESULT_JSON "
    if not line.startswith(prefix):
        return None
    try:
        return json.loads(line[len(prefix) :])
    except json.JSONDecodeError:
        return None


def first_result_payload(responses):
    for line in responses or []:
        parsed = parse_result_line(line)
        if parsed:
            return parsed
    return None


def build_result_record(sensor_id, parsed):
    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "sensor_id": parsed.get("sensor_id", sensor_id),
        "person_count": parsed.get("person_count", 0),
        "survivor_candidate": parsed.get("survivor_candidate", False),
        "confidence": parsed.get("confidence", 0.0),
        "status": parsed.get("status", "UNKNOWN"),
        "profile_version": parsed.get("profile_version", "unknown"),
        "raw": parsed,
    }


def store_node_read(sensor_id, target, result, parsed, source):
    with STATE.lock:
        node = find_node(sensor_id)
        if not node:
            return None
        node["last_result_read"] = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "target": target,
            "source": source,
            **result,
        }
        if parsed:
            node["last_result"] = build_result_record(sensor_id, parsed)
            node["status"] = "자동수신" if source == "monitor" else "결과수신"
        elif result.get("ok"):
            node["status"] = "응답대기"
        else:
            node["status"] = "연결실패"
        STATE.nodes = save_nodes(STATE.nodes)
        return node


def write_serial_line(port, baud, line, read_seconds=1.2):
    with SERIAL_LOCK:
        return _write_serial_line(port, baud, line, read_seconds=read_seconds)


def write_serial_lines(port, baud, lines, read_seconds=1.2):
    with SERIAL_LOCK:
        try:
            import serial

            with serial.Serial(port, baud, timeout=1.0, write_timeout=2.0) as handle:
                time.sleep(0.15)
                try:
                    handle.reset_input_buffer()
                except Exception:
                    pass
                for line in lines:
                    handle.write((line + "\n").encode("utf-8"))
                    handle.flush()
                    time.sleep(0.06)
                responses = []
                deadline = time.time() + read_seconds
                while time.time() < deadline:
                    raw = handle.readline()
                    if raw:
                        text = raw.decode("utf-8", errors="replace").strip()
                        if text:
                            responses.append(text)
                    else:
                        time.sleep(0.03)
            return {"ok": True, "method": "pyserial", "responses": responses}
        except ImportError:
            pass
        except OSError as error:
            return {"ok": False, "error": str(error)}

        combined = {"ok": True, "method": "fallback", "responses": []}
        for line in lines:
            result = _write_serial_line(port, baud, line, read_seconds=read_seconds)
            combined["responses"].extend(result.get("responses", []))
            combined["ok"] = combined["ok"] and result.get("ok", False)
            if not result.get("ok"):
                combined["error"] = result.get("error")
        return combined


def _write_serial_line(port, baud, line, read_seconds=1.2):
    try:
        import serial

        with serial.Serial(port, baud, timeout=1.0, write_timeout=2.0) as handle:
            time.sleep(0.15)
            try:
                handle.reset_input_buffer()
            except Exception:
                pass
            handle.write((line + "\n").encode("utf-8"))
            handle.flush()
            responses = []
            deadline = time.time() + read_seconds
            while time.time() < deadline:
                raw = handle.readline()
                if raw:
                    text = raw.decode("utf-8", errors="replace").strip()
                    if text:
                        responses.append(text)
                else:
                    time.sleep(0.03)
        return {"ok": True, "method": "pyserial", "responses": responses}
    except ImportError:
        pass

    if os.name != "posix":
        return {
            "ok": False,
            "error": "pyserial이 없어 Windows COM 포트에 직접 쓸 수 없습니다.",
        }

    import termios

    baud_map = {
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
        57600: termios.B57600,
        115200: termios.B115200,
        230400: getattr(termios, "B230400", termios.B115200),
        460800: getattr(termios, "B460800", termios.B115200),
        921600: getattr(termios, "B921600", termios.B115200),
    }
    speed = baud_map.get(int(baud), termios.B115200)
    fd = None
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[4] = speed
        attrs[5] = speed
        attrs[2] = attrs[2] | termios.CLOCAL | termios.CREAD
        attrs[3] = 0
        attrs[0] = 0
        attrs[1] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, (line + "\n").encode("utf-8"))
        responses = []
        buffer = b""
        deadline = time.time() + read_seconds
        while time.time() < deadline:
            try:
                chunk = os.read(fd, 512)
            except BlockingIOError:
                chunk = b""
            if chunk:
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    if text:
                        responses.append(text)
            else:
                time.sleep(0.03)
        return {"ok": True, "method": "termios", "responses": responses}
    except OSError as error:
        return {"ok": False, "error": str(error)}
    finally:
        if fd is not None:
            os.close(fd)


def build_profile_line(node_profile):
    payload = {
        "type": "profile",
        "sensor_id": node_profile.get("sensor_id"),
        "location_id": node_profile.get("location_id"),
        "profile_version": node_profile.get("profile_version"),
        "profile": node_profile.get("profile", {}),
    }
    return "PROFILE_JSON " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def pc_detection_for_sensor(sensor_id):
    path = latest_collection_csv(sensor_id)
    rows = read_csv_tail(path)
    vital_source_rows = [row for row in rows if is_vital_row(row)]
    latest_row = rows[-1] if rows else None
    latest_vital = vital_source_rows[-1] if vital_source_rows else None
    recent_window = rows[-180:] if rows else []
    recent_vital_count = len([row for row in recent_window if is_vital_row(row)])
    recent_vital_ratio = round(recent_vital_count / len(recent_window), 4) if recent_window else 0.0
    return pc_live_detection(sensor_id, rows, vital_source_rows, latest_row, latest_vital, recent_vital_ratio)


def build_pc_result_line(pc_detection):
    metrics = pc_detection.get("metrics", {}) if isinstance(pc_detection, dict) else {}
    payload = {
        "sensor_id": pc_detection.get("sensor_id"),
        "profile_version": pc_detection.get("profile_version"),
        "status": pc_detection.get("status"),
        "person_count": pc_detection.get("person_count", 0),
        "survivor_candidate": pc_detection.get("survivor_candidate", False),
        "confidence": pc_detection.get("confidence", 0.0),
        "source": pc_detection.get("source", "pc_vital_window"),
        "simulated": False,
        "vital_ratio": metrics.get("recent_vital_ratio", 0.0),
        "breath_deviation": metrics.get("breath_deviation_p75", 0.0),
        "heart_rate": metrics.get("heart_rate_median", 0.0),
        "breath_rate": metrics.get("breath_rate_median", 0.0),
        "range_bin": metrics.get("range_bin_mode", ""),
        "range_stability": metrics.get("range_stability", 0.0),
        "frame_gap": metrics.get("frame_gap", 0),
        "valid_vital_rows": metrics.get("valid_vital_rows", 0),
    }
    return "PC_RESULT_JSON " + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def action_list_ports():
    ports = scan_serial_ports()
    with STATE.lock:
        STATE.add_log("PORT", f"시리얼 포트 {len(ports)}개 스캔")
    return {"ports": ports}


def action_add_node(data):
    with STATE.lock:
        sensor_id = str(data.get("sensor_id") or next_sensor_id(STATE.nodes)).strip()
        if any(node["sensor_id"] == sensor_id for node in STATE.nodes):
            raise RuntimeError(f"{sensor_id} 노드가 이미 있습니다.")
        node = normalize_node({**data, "sensor_id": sensor_id}, fallback_id=sensor_id)
        STATE.nodes.append(node)
        STATE.nodes = save_nodes(STATE.nodes)
        STATE.add_log("NODE", f"{sensor_id} 추가")
        return {"nodes": STATE.nodes, "node": node}


def action_update_node(data):
    sensor_id = str(data.get("sensor_id") or "").strip()
    if not sensor_id:
        raise RuntimeError("sensor_id가 필요합니다.")
    with STATE.lock:
        for index, node in enumerate(STATE.nodes):
            if node["sensor_id"] == sensor_id:
                merged = {**node, **data, "sensor_id": sensor_id}
                STATE.nodes[index] = normalize_node(merged, fallback_id=sensor_id)
                STATE.nodes = save_nodes(STATE.nodes)
                STATE.add_log("NODE", f"{sensor_id} 수정")
                return {"nodes": STATE.nodes, "node": STATE.nodes[index]}
    raise RuntimeError(f"{sensor_id} 노드를 찾지 못했습니다.")


def action_delete_node(data):
    sensor_id = str(data.get("sensor_id") or "").strip()
    if not sensor_id:
        raise RuntimeError("sensor_id가 필요합니다.")
    with STATE.lock:
        before = len(STATE.nodes)
        STATE.nodes = [node for node in STATE.nodes if node["sensor_id"] != sensor_id]
        if len(STATE.nodes) == before:
            raise RuntimeError(f"{sensor_id} 노드를 찾지 못했습니다.")
        STATE.nodes = save_nodes(STATE.nodes)
        STATE.add_log("NODE", f"{sensor_id} 삭제")
        return {"nodes": STATE.nodes}


def find_node(sensor_id):
    for node in STATE.nodes:
        if node["sensor_id"] == sensor_id:
            return node
    return None


def action_test_node(data):
    sensor_id = str(data.get("sensor_id") or "").strip()
    if not sensor_id:
        raise RuntimeError("sensor_id가 필요합니다.")
    with STATE.lock:
        node = find_node(sensor_id)
        if not node:
            raise RuntimeError(f"{sensor_id} 노드를 찾지 못했습니다.")
        if not node.get("esp_port"):
            raise RuntimeError(f"{sensor_id} ESP32 포트가 비어 있습니다.")
        port = node["esp_port"]
        baud = node.get("esp_baud", 115200)

    result = write_serial_line(port, baud, "PING", read_seconds=1.6)
    with STATE.lock:
        node = find_node(sensor_id)
        if node:
            node["last_connection_test"] = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "target": port,
                **result,
            }
            if result.get("ok") and any(line.startswith("ACK,PONG") or line.startswith("ACK,READY") for line in result.get("responses", [])):
                node["status"] = "연결확인"
            elif result.get("ok"):
                node["status"] = "응답대기"
            else:
                node["status"] = "연결실패"
            STATE.nodes = save_nodes(STATE.nodes)
        STATE.add_log("TEST", f"{sensor_id} 연결 테스트 완료")
        return {"node": node, "test": result, "nodes": STATE.nodes}


def action_read_node_result(data):
    sensor_id = str(data.get("sensor_id") or "").strip()
    if not sensor_id:
        raise RuntimeError("sensor_id가 필요합니다.")
    with STATE.lock:
        node = find_node(sensor_id)
        if not node:
            raise RuntimeError(f"{sensor_id} 노드를 찾지 못했습니다.")
        if not node.get("esp_port"):
            raise RuntimeError(f"{sensor_id} ESP32 포트가 비어 있습니다.")
        port = node["esp_port"]
        baud = node.get("esp_baud", 115200)

    result = write_serial_line(port, baud, "RESULT?", read_seconds=1.8)
    parsed = first_result_payload(result.get("responses", []))
    node = store_node_read(sensor_id, port, result, parsed, "manual")

    with STATE.lock:
        STATE.add_log("RESULT", f"{sensor_id} 결과 읽기 완료")
        return {"node": node, "read": result, "parsed": parsed, "nodes": STATE.nodes}


def action_scan():
    with STATE.lock:
        data_dir = STATE.data_dir
    summary = summarize_dataset(data_dir)
    with STATE.lock:
        STATE.summary = summary
        STATE.add_log("SCAN", f"CSV {summary['csv_file_count']}개, {summary['total_rows']}행 요약 완료")
    save_json(RUNTIME_ROOT / "last_summary.json", summary)
    return summary


def start_collection_jobs():
    started = []
    skipped = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not RADAR_SCRIPT.exists():
        raise RuntimeError(f"radar_to_esp32.py를 찾지 못했습니다: {RADAR_SCRIPT}")

    with STATE.lock:
        nodes = [node for node in STATE.nodes if node.get("enabled", True)]

    for node in nodes:
        sensor_id = node["sensor_id"]
        if sensor_id in COLLECTION_PROCS and COLLECTION_PROCS[sensor_id]["process"].poll() is None:
            skipped.append({"sensor_id": sensor_id, "reason": "이미 수집중"})
            continue
        if node.get("connection_mode") != "pc_iwr6843_usb":
            skipped.append({"sensor_id": sensor_id, "reason": "ESP32 엣지 모드는 결과읽기 사용"})
            continue
        missing = node_readiness(node)["missing"]
        required_missing = [item for item in missing if item in {"CLI 포트", "DATA 포트"}]
        if required_missing:
            skipped.append({"sensor_id": sensor_id, "reason": ", ".join(required_missing)})
            with STATE.lock:
                live_node = find_node(sensor_id)
                if live_node:
                    live_node["status"] = "포트필요"
            continue

        csv_path = RUNTIME_ROOT / f"collection_{sensor_id}_{timestamp}.csv"
        log_path = RUNTIME_ROOT / f"collection_{sensor_id}.log"
        session_id = f"{sensor_id}_{timestamp}"
        cmd = [
            sys.executable,
            "-u",
            str(RADAR_SCRIPT),
            "--cli-port",
            node["cli_port"],
            "--data-port",
            node["data_port"],
            "--data-baud",
            str(node.get("data_baud", 921600)),
            "--csv",
            str(csv_path),
            "--label",
            "live_unlabeled",
            "--session-id",
            session_id,
            "--cfg-name",
            node.get("cfg_name") or "current_radar_cfg",
            "--esp-mode",
            "none",
        ]
        if node.get("cfg_path"):
            cmd.extend(["--cfg", node["cfg_path"]])
        else:
            cmd.append("--no-cfg")

        log_handle = open(log_path, "a", encoding="utf-8")
        log_handle.write(f"\n--- {utc_now()} START {' '.join(cmd)} ---\n")
        log_handle.flush()
        process = subprocess.Popen(
            cmd,
            cwd=str(RADAR_SCRIPT.parent),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        COLLECTION_PROCS[sensor_id] = {
            "process": process,
            "log_handle": log_handle,
            "cmd": cmd,
            "log_path": log_path,
            "csv_path": csv_path,
            "started_at": utc_now(),
        }
        with STATE.lock:
            live_node = find_node(sensor_id)
            if live_node:
                live_node["status"] = "수집중"
                live_node["collection_log"] = str(log_path)
                live_node["collection_csv"] = str(csv_path)
        started.append({"sensor_id": sensor_id, "pid": process.pid, "csv_path": str(csv_path), "log_path": str(log_path)})

    with STATE.lock:
        STATE.nodes = save_nodes(STATE.nodes)
    return {"started": started, "skipped": skipped}


def stop_collection_jobs():
    stopped = []
    for sensor_id, job in list(COLLECTION_PROCS.items()):
        process = job.get("process")
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        handle = job.get("log_handle")
        if handle:
            handle.write(f"--- {utc_now()} STOP ---\n")
            handle.close()
        stopped.append({"sensor_id": sensor_id, "return_code": process.poll() if process else None})
        del COLLECTION_PROCS[sensor_id]
        with STATE.lock:
            node = find_node(sensor_id)
            if node and node.get("enabled", True):
                node["status"] = "대기"
    with STATE.lock:
        STATE.nodes = save_nodes(STATE.nodes)
    return {"stopped": stopped}


def action_collect():
    with STATE.lock:
        should_start = not STATE.collection_running

    if should_start:
        result = start_collection_jobs()
        with STATE.lock:
            STATE.collection_running = bool(result["started"])
            if not result["started"]:
                STATE.collection_running = False
            STATE.add_log("COLLECT", f"수집 시작: 시작 {len(result['started'])}개, 보류 {len(result['skipped'])}개")
            return {
                "collection_running": STATE.collection_running,
                "nodes": STATE.nodes,
                "collection_jobs": collection_job_snapshot(),
                **result,
            }

    result = stop_collection_jobs()
    with STATE.lock:
        STATE.collection_running = False
        STATE.add_log("COLLECT", f"수집 중지: {len(result['stopped'])}개")
        return {
            "collection_running": STATE.collection_running,
            "nodes": STATE.nodes,
            "collection_jobs": collection_job_snapshot(),
            **result,
        }


def action_calibrate():
    with STATE.lock:
        if STATE.summary is None:
            STATE.summary = summarize_dataset(STATE.data_dir)
        summary = STATE.summary
        nodes = [node for node in STATE.nodes if node.get("enabled", True)]
        if not nodes:
            raise RuntimeError("활성화된 센서 노드가 없습니다.")
        model = STATE.model
        STATE.add_log("LLM", f"{model} 캘리브레이션 요청 시작: 노드 {len(nodes)}개")
    messages = build_llm_messages(summary, nodes)
    try:
        raw = call_ollama(model, messages)
        content = raw.get("message", {}).get("content", "")
        profile = extract_json(content)
        validation = validate_profile_batch(profile)
        meta = {
            "model": raw.get("model"),
            "created_at": raw.get("created_at"),
            "done_reason": raw.get("done_reason"),
            "prompt_eval_count": raw.get("prompt_eval_count"),
            "eval_count": raw.get("eval_count"),
            "total_duration_ms": round((raw.get("total_duration") or 0) / 1_000_000, 1),
        }
    except Exception as error:
        with STATE.lock:
            STATE.add_log("ERROR", f"LLM 캘리브레이션 실패: {error}")
        raise

    profile["created_at"] = utc_now()
    save_json(PROFILE_ROOT / "last_profile_batch.json", profile)
    save_json(RUNTIME_ROOT / "last_llm_messages.json", {"messages": messages})
    with STATE.lock:
        STATE.profile = profile
        STATE.validation = validation
        STATE.llm_meta = meta
        STATE.add_log("LLM", f"profile 생성 완료, 검증 ok={validation['ok']}")
    return {"profile": profile, "validation": validation, "llm_meta": meta}


def profile_by_sensor(profile, sensor_id):
    if not profile:
        return None
    batch = profile.get("profile_batch", {})
    version = batch.get("profile_version", "unknown")
    for item in batch.get("sensor_profiles", []):
        if item.get("sensor_id") == sensor_id:
            result = dict(item)
            result["profile_version"] = version
            return result
    return None


def action_deploy():
    with STATE.lock:
        profile = STATE.profile
        summary = STATE.summary
        if not profile:
            raise RuntimeError("배포할 profile이 없습니다. 먼저 LLM 캘리브레이션을 실행하세요.")
        for node in STATE.nodes:
            if not node.get("enabled", True):
                node["status"] = "비활성"
                node["profile_status"] = "미배포"
                continue
            node_profile = profile_by_sensor(profile, node["sensor_id"])
            if node_profile:
                if node.get("esp_port"):
                    line = build_profile_line(node_profile)
                    deploy_result = write_serial_line(node["esp_port"], node.get("esp_baud", 115200), line, read_seconds=1.8)
                    has_ack = any(response.startswith("ACK,PROFILE") for response in deploy_result.get("responses", []))
                    if deploy_result.get("ok") and has_ack:
                        node["profile_status"] = "배포완료"
                    elif deploy_result.get("ok"):
                        node["profile_status"] = "ACK없음"
                    else:
                        node["profile_status"] = "배포실패"
                    node["last_deploy"] = {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "target": node["esp_port"],
                        **deploy_result,
                    }
                else:
                    node["profile_status"] = "시뮬레이션"
                    node["last_deploy"] = {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "target": "no esp_port",
                        "ok": True,
                        "method": "simulation",
                    }
                node["status"] = "엣지연산" if node.get("esp_port") else "시뮬레이션"
                node["last_result"] = generate_simulated_result(node, node_profile, summary)
            else:
                node["profile_status"] = "대기"
        STATE.nodes = save_nodes(STATE.nodes)
        STATE.add_log("DEPLOY", "ESP32 profile 배포 처리 완료")
        return {"nodes": STATE.nodes}


def monitor_targets():
    with STATE.lock:
        return [
            {
                "sensor_id": node["sensor_id"],
                "port": node["esp_port"],
                "baud": node.get("esp_baud", 115200),
            }
            for node in STATE.nodes
            if node.get("enabled", True) and node.get("esp_port")
        ]


def poll_esp32_results(read_seconds=0.9, source="monitor"):
    targets = monitor_targets()
    results = []
    parsed_count = 0
    error_count = 0
    for target in targets:
        pc_detection = pc_detection_for_sensor(target["sensor_id"])
        lines = [build_pc_result_line(pc_detection), "RESULT?"]
        result = write_serial_lines(target["port"], target["baud"], lines, read_seconds=read_seconds)
        result["pc_sync"] = {
            "sent": True,
            "status": pc_detection.get("status"),
            "confidence": pc_detection.get("confidence"),
            "source": pc_detection.get("source"),
        }
        parsed = first_result_payload(result.get("responses", []))
        if parsed:
            parsed_count += 1
        if not result.get("ok"):
            error_count += 1
        node = store_node_read(target["sensor_id"], target["port"], result, parsed, source)
        results.append(
            {
                "sensor_id": target["sensor_id"],
                "target": target["port"],
                "ok": result.get("ok", False),
                "parsed": bool(parsed),
                "node_status": node.get("status") if node else "missing",
            }
        )

    with STATE.lock:
        now = utc_now()
        STATE.monitor_last_poll = now
        STATE.last_result_tick = now
        if source == "monitor":
            STATE.monitor_poll_count += 1
            STATE.monitor_error_count += error_count
            if error_count:
                STATE.add_log("MONITOR", f"자동 결과 읽기 오류 {error_count}개")
        return {
            "polls": results,
            "target_count": len(targets),
            "parsed_count": parsed_count,
            "error_count": error_count,
            "last_result_tick": STATE.last_result_tick,
        }


def monitor_loop():
    try:
        while not MONITOR_STOP.wait(STATE.monitor_interval_seconds):
            try:
                poll_esp32_results(read_seconds=0.8, source="monitor")
            except Exception as error:
                with STATE.lock:
                    STATE.monitor_error_count += 1
                    STATE.add_log("MONITOR", f"자동 모니터 오류: {error}")
    finally:
        with STATE.lock:
            STATE.monitor_running = False
            STATE.add_log("MONITOR", "자동 모니터 종료")


def start_monitor():
    global MONITOR_THREAD
    targets = monitor_targets()
    if not targets:
        raise RuntimeError("ESP32 포트가 설정된 활성 노드가 없습니다.")
    if MONITOR_THREAD and MONITOR_THREAD.is_alive():
        with STATE.lock:
            STATE.monitor_running = True
        return {"monitor_running": True, "target_count": len(targets)}

    MONITOR_STOP.clear()
    MONITOR_THREAD = threading.Thread(target=monitor_loop, name="esp32-result-monitor", daemon=True)
    with STATE.lock:
        STATE.monitor_running = True
        STATE.add_log("MONITOR", f"자동 모니터 시작: {len(targets)}개 노드")
    MONITOR_THREAD.start()
    return {"monitor_running": True, "target_count": len(targets)}


def stop_monitor():
    global MONITOR_THREAD
    MONITOR_STOP.set()
    thread = MONITOR_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=3)
    MONITOR_THREAD = None
    with STATE.lock:
        STATE.monitor_running = False
    return {"monitor_running": False}


def action_monitor():
    with STATE.lock:
        running = STATE.monitor_running
    if running:
        return stop_monitor()
    return start_monitor()


def action_tick():
    poll = poll_esp32_results(read_seconds=1.0, source="manual")
    with STATE.lock:
        for node in STATE.nodes:
            if not node.get("enabled", True):
                continue
            node_profile = profile_by_sensor(STATE.profile, node["sensor_id"])
            if not node.get("esp_port") and node_profile:
                node["last_result"] = generate_simulated_result(node, node_profile, STATE.summary)
                node["status"] = "시뮬레이션"
                if node.get("profile_status") in {"미배포", "대기"}:
                    node["profile_status"] = "시뮬레이션"
        STATE.last_result_tick = utc_now()
        STATE.nodes = save_nodes(STATE.nodes)
        STATE.add_log("RESULT", "센서 결과값 갱신")
        return {"nodes": STATE.nodes, "last_result_tick": STATE.last_result_tick, "poll": poll}


def read_request_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "HaniumRadarControl/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, error, status=500):
        self.send_json({"ok": False, "error": str(error)}, status=status)

    def do_GET(self):
        if self.path == "/":
            return self.serve_static("index.html")
        if self.path == "/vitals":
            return self.serve_static("vitals.html")
        if self.path == "/api/state":
            return self.send_json(STATE.snapshot())
        if self.path == "/api/vitals":
            return self.send_json(action_vitals())
        if self.path == "/api/ports":
            return self.send_json({"ok": True, **action_list_ports()})
        if self.path.startswith("/static/"):
            return self.serve_static(self.path[len("/static/") :])
        return self.send_error_json("Not found", status=404)

    def do_HEAD(self):
        if self.path in {"/", "/vitals"}:
            target = STATIC_ROOT / ("vitals.html" if self.path == "/vitals" else "index.html")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            return
        return self.send_error_json("Not found", status=404)

    def do_POST(self):
        try:
            if self.path == "/api/scan":
                return self.send_json({"ok": True, "summary": action_scan()})
            if self.path == "/api/collect":
                return self.send_json({"ok": True, **action_collect()})
            if self.path == "/api/calibrate":
                return self.send_json({"ok": True, **action_calibrate()})
            if self.path == "/api/deploy":
                return self.send_json({"ok": True, **action_deploy()})
            if self.path == "/api/tick":
                return self.send_json({"ok": True, **action_tick()})
            if self.path == "/api/monitor":
                return self.send_json({"ok": True, **action_monitor()})
            if self.path == "/api/nodes/add":
                return self.send_json({"ok": True, **action_add_node(read_request_json(self))})
            if self.path == "/api/nodes/update":
                return self.send_json({"ok": True, **action_update_node(read_request_json(self))})
            if self.path == "/api/nodes/delete":
                return self.send_json({"ok": True, **action_delete_node(read_request_json(self))})
            if self.path == "/api/nodes/test":
                return self.send_json({"ok": True, **action_test_node(read_request_json(self))})
            if self.path == "/api/nodes/read-result":
                return self.send_json({"ok": True, **action_read_node_result(read_request_json(self))})
            if self.path == "/api/config":
                data = read_request_json(self)
                with STATE.lock:
                    if data.get("data_dir"):
                        STATE.data_dir = Path(data["data_dir"])
                    if data.get("model"):
                        STATE.model = data["model"]
                    STATE.add_log("CONFIG", "설정 변경 완료")
                return self.send_json({"ok": True, "state": STATE.snapshot()})
            return self.send_error_json("Not found", status=404)
        except Exception as error:
            return self.send_error_json(error, status=500)

    def serve_static(self, relative):
        target = (STATIC_ROOT / relative).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())) or not target.exists():
            return self.send_error_json("Static file not found", status=404)
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"HANIIUM Radar Local LLM Control")
    print(f"URL: http://{host}:{port}")
    print(f"Data dir: {DEFAULT_DATA_DIR}")
    print(f"Ollama model: {DEFAULT_MODEL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        stop_collection_jobs()
        stop_monitor()


if __name__ == "__main__":
    main()
