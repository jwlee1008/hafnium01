#!/usr/bin/env python3
import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re
import statistics
import urllib.error
import urllib.request


DEFAULT_MODEL = "qwen3:14b"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"


def parse_float(value):
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    if not math.isfinite(result):
        return None
    return result


def parse_int(value):
    parsed = parse_float(value)
    if parsed is None:
        return None
    return int(parsed)


def mean(values):
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return round(statistics.fmean(clean), 6)


def percentile(values, ratio):
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    index = min(len(clean) - 1, max(0, round((len(clean) - 1) * ratio)))
    return round(clean[index], 6)


def compact_counter(counter, limit=8):
    return [
        {"value": str(value), "count": count}
        for value, count in counter.most_common(limit)
    ]


def summarize_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    labels = Counter()
    phase_labels = Counter()
    sessions = Counter()
    source_counter = Counter()
    has_vital_count = 0
    detected_objs = []
    breath_devs = []
    heart_rates = []
    breath_rates = []
    range_bins = []
    timestamps = []
    frame_numbers = []
    tlv1040_count = 0

    for row in rows:
        label = row.get("label") or row.get("main_label") or "unlabeled"
        labels[label] += 1
        if row.get("phase_label"):
            phase_labels[row["phase_label"]] += 1
        if row.get("session_id"):
            sessions[row["session_id"]] += 1
        if row.get("source"):
            source_counter[row["source"]] += 1

        has_vital = str(row.get("has_vital", "")).strip().lower() in {"1", "true", "yes"}
        has_vital_count += int(has_vital)

        detected_objs.append(parse_int(row.get("num_detected_obj")))
        if has_vital:
            breath_devs.append(parse_float(row.get("breath_deviation")))
            heart_rates.append(parse_float(row.get("heart_rate")))
            breath_rates.append(parse_float(row.get("breath_rate")))
            range_bin = row.get("range_bin")
            if range_bin not in (None, ""):
                range_bins.append(range_bin)

        timestamps.append(parse_float(row.get("timestamp") or row.get("estimated_time_sec")))
        frame_numbers.append(parse_int(row.get("frame")))
        tlv_summary = row.get("tlv_summary") or ""
        if "1040:" in tlv_summary:
            tlv1040_count += 1

    clean_timestamps = [value for value in timestamps if value is not None]
    clean_frames = [value for value in frame_numbers if value is not None]
    duration_seconds = None
    if len(clean_timestamps) >= 2:
        duration_seconds = round(max(clean_timestamps) - min(clean_timestamps), 3)

    return {
        "file": path.name,
        "rows": len(rows),
        "labels": compact_counter(labels),
        "phase_labels": compact_counter(phase_labels),
        "sessions": compact_counter(sessions),
        "sources": compact_counter(source_counter),
        "duration_seconds": duration_seconds,
        "frame_min": min(clean_frames) if clean_frames else None,
        "frame_max": max(clean_frames) if clean_frames else None,
        "vital_rows": has_vital_count,
        "vital_ratio": round(has_vital_count / len(rows), 6) if rows else 0.0,
        "tlv1040_rows": tlv1040_count,
        "num_detected_obj_mean": mean(detected_objs),
        "num_detected_obj_p90": percentile(detected_objs, 0.90),
        "num_detected_obj_max": max([value for value in detected_objs if value is not None], default=None),
        "breath_deviation_mean_on_vital": mean(breath_devs),
        "breath_deviation_p50_on_vital": percentile(breath_devs, 0.50),
        "breath_deviation_p90_on_vital": percentile(breath_devs, 0.90),
        "heart_rate_mean_on_vital": mean(heart_rates),
        "breath_rate_mean_on_vital": mean(breath_rates),
        "range_bin_modes_on_vital": compact_counter(Counter(range_bins), limit=5),
    }


def build_dataset_summary(csv_paths):
    files = [summarize_csv(path) for path in csv_paths]
    all_labels = Counter()
    total_rows = 0
    total_vital = 0

    for file_summary in files:
        total_rows += file_summary["rows"]
        total_vital += file_summary["vital_rows"]
        for label in file_summary["labels"]:
            all_labels[label["value"]] += label["count"]

    missing_baselines = []
    if "no_person" not in all_labels:
        missing_baselines.append("no_person")
    if "person_moving" not in all_labels:
        missing_baselines.append("person_moving")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "csv_file_count": len(files),
        "total_rows": total_rows,
        "total_vital_rows": total_vital,
        "overall_vital_ratio": round(total_vital / total_rows, 6) if total_rows else 0.0,
        "labels": compact_counter(all_labels, limit=12),
        "data_quality_flags": {
            "missing_baseline_labels": missing_baselines,
            "small_multiclass_dataset": total_rows < 10000 or len(all_labels) < 5,
            "profile_should_be_trial_only": True,
        },
        "files": files,
    }


def build_messages(dataset_summary):
    schema = {
        "agent_role": "local_llm_calibration_agent",
        "profile_batch": {
            "profile_version": "trial-local-llm-v1",
            "deployment_decision": "trial_only | collect_more_data | deploy_candidate",
            "global_rationale_ko": "short Korean explanation",
            "sensor_profiles": [
                {
                    "sensor_id": "sensor_01",
                    "location_id": "unknown_lab_position",
                    "profile": {
                        "mode": "vital_detect",
                        "window_seconds": "integer 5..12",
                        "breath_dev_min": "float 0.005..0.08",
                        "vital_ratio_min": "float 0.02..0.5",
                        "confirm_seconds": "integer 3..15",
                        "lost_grace_seconds": "integer 1..8",
                        "moving_reject": "boolean",
                        "confidence_policy": "sensitive | balanced | conservative",
                    },
                    "rationale_ko": "why these values fit the current data",
                    "risk_notes_ko": ["risk 1", "risk 2"],
                    "required_next_data": ["label/session to collect next"],
                }
            ],
            "next_experiments": ["experiment suggestion"],
        },
    }

    system = (
        "You are a local LLM calibration agent for a mmWave radar + ESP32 edge system. "
        "Your job is to propose lightweight sensor profiles that an ESP32 can execute. "
        "You do not write firmware. You do not claim deployment readiness when baseline data is missing. "
        "Return valid JSON only. No markdown."
    )
    user = (
        "Use the dataset summary below to propose a trial calibration profile for sensor_01. "
        "The profile must be conservative because the dataset currently has limited no-person baseline data. "
        "Keep numeric parameters realistic for ESP32 threshold/window logic. "
        "Return JSON matching this shape:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        "Dataset summary:\n"
        f"{json.dumps(dataset_summary, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def call_ollama(model, messages, timeout_seconds):
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.15,
            "top_p": 0.9,
            "num_predict": 1400,
        },
    }
    request = urllib.request.Request(
        OLLAMA_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(f"Ollama request failed: {error}") from error


def extract_json_object(text):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response.")
    return json.loads(match.group(0))


def validate_profile(parsed):
    errors = []
    warnings = []

    batch = parsed.get("profile_batch")
    if not isinstance(batch, dict):
        errors.append("profile_batch must be an object")
        return {"ok": False, "errors": errors, "warnings": warnings}

    profiles = batch.get("sensor_profiles")
    if not isinstance(profiles, list) or not profiles:
        errors.append("profile_batch.sensor_profiles must be a non-empty list")
        return {"ok": False, "errors": errors, "warnings": warnings}

    for index, item in enumerate(profiles):
        profile = item.get("profile") if isinstance(item, dict) else None
        if not isinstance(profile, dict):
            errors.append(f"sensor_profiles[{index}].profile must be an object")
            continue

        ranges = {
            "window_seconds": (5, 12),
            "breath_dev_min": (0.005, 0.08),
            "vital_ratio_min": (0.02, 0.5),
            "confirm_seconds": (3, 15),
            "lost_grace_seconds": (1, 8),
        }
        for key, (low, high) in ranges.items():
            value = profile.get(key)
            if not isinstance(value, (int, float)):
                errors.append(f"{key} must be numeric")
            elif value < low or value > high:
                errors.append(f"{key}={value} outside allowed range {low}..{high}")

        if profile.get("confidence_policy") not in {"sensitive", "balanced", "conservative"}:
            errors.append("confidence_policy must be sensitive, balanced, or conservative")

        if not isinstance(profile.get("moving_reject"), bool):
            errors.append("moving_reject must be boolean")

    decision = batch.get("deployment_decision")
    if decision == "deploy_candidate":
        warnings.append("Model suggested deploy_candidate; verify with no_person and moving baselines before field use.")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(
        description="Trial a local Ollama LLM as a radar calibration profile agent."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--csv-glob", default="*.csv")
    parser.add_argument("--output", default="local_llm_calibration_trial.json")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    csv_paths = sorted(Path.cwd().glob(args.csv_glob))
    if not csv_paths:
        raise SystemExit(f"No CSV files matched {args.csv_glob!r} in {Path.cwd()}")

    dataset_summary = build_dataset_summary(csv_paths)
    messages = build_messages(dataset_summary)
    ollama_response = call_ollama(args.model, messages, args.timeout)
    content = ollama_response.get("message", {}).get("content", "")
    parsed = extract_json_object(content)
    validation = validate_profile(parsed)

    report = {
        "trial_type": "local_llm_profile_calibration",
        "model": args.model,
        "repo": str(Path.cwd()),
        "dataset_summary": dataset_summary,
        "prompt_messages": messages,
        "ollama_response_meta": {
            key: ollama_response.get(key)
            for key in [
                "model",
                "created_at",
                "done",
                "done_reason",
                "total_duration",
                "load_duration",
                "prompt_eval_count",
                "eval_count",
            ]
        },
        "llm_response_text": content,
        "parsed_profile": parsed,
        "validation": validation,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Model: {args.model}")
    print(f"CSV files: {len(csv_paths)}")
    print(f"Rows: {dataset_summary['total_rows']}")
    print(f"Vital rows: {dataset_summary['total_vital_rows']}")
    print(f"Validation ok: {validation['ok']}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
