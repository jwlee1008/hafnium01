import argparse
import csv
import math
from collections import Counter, defaultdict
from pathlib import Path


KNOWN_SUFFIXES = (
    "_frames",
    "_points",
    "_targets",
    "_target_indexes",
    "_vitals",
    "_grid_windows",
)


def parse_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def parse_int(value, default=0):
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def normalize_prefix(path_text):
    path = Path(path_text)
    stem_path = path.with_suffix("") if path.suffix.lower() == ".csv" else path

    for suffix in KNOWN_SUFFIXES:
        if stem_path.name.endswith(suffix):
            return stem_path.with_name(stem_path.name[: -len(suffix)])

    return stem_path


def input_paths(prefix_path):
    return {
        "frames": prefix_path.with_name(f"{prefix_path.name}_frames.csv"),
        "points": prefix_path.with_name(f"{prefix_path.name}_points.csv"),
        "targets": prefix_path.with_name(f"{prefix_path.name}_targets.csv"),
        "vitals": prefix_path.with_name(f"{prefix_path.name}_vitals.csv"),
    }


def read_csv_rows(path, required=False):
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required CSV not found: {path}")
        return []

    with open(path, "r", newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def session_id(row):
    return row.get("session_id") or "session"


def group_by_session(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[session_id(row)].append(row)
    return grouped


def frame_number(row):
    return parse_int(row.get("frame"), 0)


def add_relative_time(rows, min_frame, frame_period_sec):
    for row in rows:
        row["_rel_time_sec"] = (frame_number(row) - min_frame) * frame_period_sec


def cell_dimensions(x_min, x_max, y_min, y_max, cell_size):
    x_cells = math.ceil((x_max - x_min) / cell_size)
    y_cells = math.ceil((y_max - y_min) / cell_size)
    if x_cells <= 0 or y_cells <= 0:
        raise ValueError("Grid dimensions must be positive. Check x/y range and cell size.")
    return x_cells, y_cells


def cell_index(x, y, args, x_cells, y_cells):
    if x is None or y is None:
        return None
    if x < args.x_min or x >= args.x_max or y < args.y_min or y >= args.y_max:
        return None

    ix = int((x - args.x_min) / args.cell_size)
    iy = int((y - args.y_min) / args.cell_size)
    if ix < 0 or ix >= x_cells or iy < 0 or iy >= y_cells:
        return None
    return iy, ix


def cell_center(ix, iy, args):
    x = args.x_min + ((ix + 0.5) * args.cell_size)
    y = args.y_min + ((iy + 0.5) * args.cell_size)
    return x, y


def empty_grid(x_cells, y_cells):
    return [[0.0 for _ in range(x_cells)] for _ in range(y_cells)]


def add_grid_value(grid, cell, value=1.0):
    if cell is None:
        return False
    iy, ix = cell
    grid[iy][ix] += value
    return True


def flatten_grid(prefix, grid):
    row = {}
    for iy, grid_row in enumerate(grid):
        for ix, value in enumerate(grid_row):
            row[f"cell_y{iy:02d}_x{ix:02d}_{prefix}"] = format_number(value)
    return row


def format_number(value, digits=6):
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def mean(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def max_or_none(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(values)


def mode_or_blank(values):
    values = [value for value in values if value not in (None, "")]
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def count_tlv_items(tlv_summary):
    if not tlv_summary:
        return 0
    return sum(1 for item in tlv_summary.split(";") if ":" in item)


def build_cell_fieldnames(x_cells, y_cells):
    fieldnames = []
    for prefix in ("point_count", "target_count", "vital_count", "motion_sum"):
        for iy in range(y_cells):
            for ix in range(x_cells):
                fieldnames.append(f"cell_y{iy:02d}_x{ix:02d}_{prefix}")
    return fieldnames


def build_output_fieldnames(x_cells, y_cells):
    base_fields = [
        "session_id",
        "cfg",
        "window_index",
        "window_start_sec",
        "window_end_sec",
        "frame_start",
        "frame_end",
        "frame_count",
        "x_min",
        "x_max",
        "y_min",
        "y_max",
        "cell_size",
        "x_cells",
        "y_cells",
        "presence_frame_count",
        "presence_ratio",
        "parse_warning_frame_count",
        "unknown_tlv_frame_count",
        "point_total",
        "point_mean_per_frame",
        "point_out_of_grid_count",
        "target_total",
        "target_id_count",
        "target_out_of_grid_count",
        "avg_target_speed_mps",
        "max_target_speed_mps",
        "vital_total",
        "vital_target_id_count",
        "vital_range_bin_mode",
        "vital_range_bin_unique_count",
        "breath_deviation_mean",
        "breath_deviation_max",
        "heart_rate_mean",
        "breath_rate_mean",
        "avg_abs_doppler_mps",
        "max_abs_doppler_mps",
        "avg_snr_db",
        "max_snr_db",
        "occupied_point_cell_count",
        "occupied_target_cell_count",
        "occupied_vital_cell_count",
        "dominant_cell_x_m",
        "dominant_cell_y_m",
        "dominant_cell_score",
    ]
    return base_fields + build_cell_fieldnames(x_cells, y_cells)


def rows_in_window(rows, start_sec, end_sec):
    return [
        row
        for row in rows
        if start_sec <= row.get("_rel_time_sec", -1.0) < end_sec
    ]


def target_speed(row):
    vx = parse_float(row.get("vel_x_mps"), 0.0)
    vy = parse_float(row.get("vel_y_mps"), 0.0)
    vz = parse_float(row.get("vel_z_mps"), 0.0)
    return math.sqrt((vx * vx) + (vy * vy) + (vz * vz))


def build_window_row(
    session,
    cfg_name,
    window_index,
    start_sec,
    end_sec,
    frame_rows,
    point_rows,
    target_rows,
    vital_rows,
    args,
    x_cells,
    y_cells,
):
    point_grid = empty_grid(x_cells, y_cells)
    target_grid = empty_grid(x_cells, y_cells)
    vital_grid = empty_grid(x_cells, y_cells)
    motion_grid = empty_grid(x_cells, y_cells)

    point_out_of_grid = 0
    point_dopplers = []
    point_snrs = []

    for point in point_rows:
        x = parse_float(point.get("x_m"))
        y = parse_float(point.get("y_m"))
        cell = cell_index(x, y, args, x_cells, y_cells)
        if not add_grid_value(point_grid, cell):
            point_out_of_grid += 1

        doppler = parse_float(point.get("doppler_mps"))
        if doppler is not None:
            point_dopplers.append(abs(doppler))
            add_grid_value(motion_grid, cell, abs(doppler))

        snr = parse_float(point.get("snr_db"))
        if snr is not None:
            point_snrs.append(snr)

    target_out_of_grid = 0
    target_ids = set()
    target_speeds = []
    target_position_sum = {}
    target_position_count = Counter()

    for target in target_rows:
        target_id = target.get("target_id", "")
        if target_id != "":
            target_ids.add(target_id)

        x = parse_float(target.get("pos_x_m"))
        y = parse_float(target.get("pos_y_m"))
        cell = cell_index(x, y, args, x_cells, y_cells)
        if not add_grid_value(target_grid, cell):
            target_out_of_grid += 1

        speed = target_speed(target)
        target_speeds.append(speed)
        add_grid_value(motion_grid, cell, speed)

        if target_id != "" and x is not None and y is not None:
            sx, sy = target_position_sum.get(target_id, (0.0, 0.0))
            target_position_sum[target_id] = (sx + x, sy + y)
            target_position_count[target_id] += 1

    target_mean_position = {}
    for target_id, (sx, sy) in target_position_sum.items():
        count = target_position_count[target_id]
        target_mean_position[target_id] = (sx / count, sy / count)

    vital_target_ids = set()
    vital_range_bins = []
    breath_deviations = []
    heart_rates = []
    breath_rates = []

    for vital in vital_rows:
        target_id = vital.get("target_id", "")
        if target_id != "":
            vital_target_ids.add(target_id)

        if target_id in target_mean_position:
            x, y = target_mean_position[target_id]
            add_grid_value(vital_grid, cell_index(x, y, args, x_cells, y_cells))

        vital_range_bins.append(vital.get("range_bin", ""))
        breath_deviations.append(parse_float(vital.get("breath_deviation")))
        heart_rates.append(parse_float(vital.get("heart_rate")))
        breath_rates.append(parse_float(vital.get("breath_rate")))

    frames = [frame_number(row) for row in frame_rows]
    presence_values = [parse_int(row.get("presence_indication"), 0) for row in frame_rows]
    presence_count = sum(1 for value in presence_values if value > 0)
    parse_warning_count = sum(
        1
        for row in frame_rows
        if parse_int(row.get("num_tlvs"), 0) != count_tlv_items(row.get("tlv_summary", ""))
    )
    unknown_tlv_count = sum(1 for row in frame_rows if row.get("unknown_tlv_summary", ""))

    dominant_score = -1.0
    dominant_cell = None
    for iy in range(y_cells):
        for ix in range(x_cells):
            score = point_grid[iy][ix] + (target_grid[iy][ix] * 3.0) + (vital_grid[iy][ix] * 5.0)
            if score > dominant_score:
                dominant_score = score
                dominant_cell = (iy, ix)

    dominant_x = dominant_y = None
    if dominant_cell is not None and dominant_score > 0:
        dominant_y_index, dominant_x_index = dominant_cell
        dominant_x, dominant_y = cell_center(dominant_x_index, dominant_y_index, args)

    frame_count = len(frame_rows)
    row = {
        "session_id": session,
        "cfg": cfg_name,
        "window_index": window_index,
        "window_start_sec": format_number(start_sec, 3),
        "window_end_sec": format_number(end_sec, 3),
        "frame_start": min(frames) if frames else "",
        "frame_end": max(frames) if frames else "",
        "frame_count": frame_count,
        "x_min": args.x_min,
        "x_max": args.x_max,
        "y_min": args.y_min,
        "y_max": args.y_max,
        "cell_size": args.cell_size,
        "x_cells": x_cells,
        "y_cells": y_cells,
        "presence_frame_count": presence_count,
        "presence_ratio": format_number(presence_count / frame_count if frame_count else 0.0),
        "parse_warning_frame_count": parse_warning_count,
        "unknown_tlv_frame_count": unknown_tlv_count,
        "point_total": len(point_rows),
        "point_mean_per_frame": format_number(len(point_rows) / frame_count if frame_count else 0.0),
        "point_out_of_grid_count": point_out_of_grid,
        "target_total": len(target_rows),
        "target_id_count": len(target_ids),
        "target_out_of_grid_count": target_out_of_grid,
        "avg_target_speed_mps": format_number(mean(target_speeds)),
        "max_target_speed_mps": format_number(max_or_none(target_speeds)),
        "vital_total": len(vital_rows),
        "vital_target_id_count": len(vital_target_ids),
        "vital_range_bin_mode": mode_or_blank(vital_range_bins),
        "vital_range_bin_unique_count": len(set(value for value in vital_range_bins if value != "")),
        "breath_deviation_mean": format_number(mean(breath_deviations)),
        "breath_deviation_max": format_number(max_or_none(breath_deviations)),
        "heart_rate_mean": format_number(mean(heart_rates), 3),
        "breath_rate_mean": format_number(mean(breath_rates), 3),
        "avg_abs_doppler_mps": format_number(mean(point_dopplers)),
        "max_abs_doppler_mps": format_number(max_or_none(point_dopplers)),
        "avg_snr_db": format_number(mean(point_snrs)),
        "max_snr_db": format_number(max_or_none(point_snrs)),
        "occupied_point_cell_count": sum(1 for grid_row in point_grid for value in grid_row if value > 0),
        "occupied_target_cell_count": sum(1 for grid_row in target_grid for value in grid_row if value > 0),
        "occupied_vital_cell_count": sum(1 for grid_row in vital_grid for value in grid_row if value > 0),
        "dominant_cell_x_m": format_number(dominant_x),
        "dominant_cell_y_m": format_number(dominant_y),
        "dominant_cell_score": format_number(dominant_score if dominant_score > 0 else 0.0),
    }

    row.update(flatten_grid("point_count", point_grid))
    row.update(flatten_grid("target_count", target_grid))
    row.update(flatten_grid("vital_count", vital_grid))
    row.update(flatten_grid("motion_sum", motion_grid))
    return row


def build_grid_dataset(args):
    prefix_path = normalize_prefix(args.prefix)
    paths = input_paths(prefix_path)
    output_path = Path(args.output) if args.output else prefix_path.with_name(
        f"{prefix_path.name}_grid_windows.csv"
    )

    frames = read_csv_rows(paths["frames"], required=True)
    points = read_csv_rows(paths["points"])
    targets = read_csv_rows(paths["targets"])
    vitals = read_csv_rows(paths["vitals"])

    frame_sessions = group_by_session(frames)
    point_sessions = group_by_session(points)
    target_sessions = group_by_session(targets)
    vital_sessions = group_by_session(vitals)

    x_cells, y_cells = cell_dimensions(
        args.x_min, args.x_max, args.y_min, args.y_max, args.cell_size
    )
    output_fieldnames = build_output_fieldnames(x_cells, y_cells)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=output_fieldnames)
        writer.writeheader()

        total_windows = 0
        for session, session_frames in sorted(frame_sessions.items()):
            if not session_frames:
                continue

            min_frame = min(frame_number(row) for row in session_frames)
            max_frame = max(frame_number(row) for row in session_frames)
            max_time_sec = (max_frame - min_frame) * args.frame_period_sec

            session_points = point_sessions.get(session, [])
            session_targets = target_sessions.get(session, [])
            session_vitals = vital_sessions.get(session, [])

            for rows in (session_frames, session_points, session_targets, session_vitals):
                add_relative_time(rows, min_frame, args.frame_period_sec)

            cfg_name = session_frames[0].get("cfg", "")
            stride_sec = args.stride_sec or args.window_sec
            window_index = 0
            start_sec = 0.0
            while start_sec <= max_time_sec + 1e-9:
                end_sec = start_sec + args.window_sec
                frame_rows = rows_in_window(session_frames, start_sec, end_sec)
                if frame_rows:
                    row = build_window_row(
                        session,
                        cfg_name,
                        window_index,
                        start_sec,
                        end_sec,
                        frame_rows,
                        rows_in_window(session_points, start_sec, end_sec),
                        rows_in_window(session_targets, start_sec, end_sec),
                        rows_in_window(session_vitals, start_sec, end_sec),
                        args,
                        x_cells,
                        y_cells,
                    )
                    writer.writerow(row)
                    total_windows += 1

                window_index += 1
                start_sec += stride_sec

    return output_path, total_windows, x_cells, y_cells


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Convert radar point/target/vital CSV files into fixed-size 2D grid "
            "window features for machine-learning preprocessing."
        )
    )
    parser.add_argument(
        "--prefix",
        required=True,
        help=(
            "Dataset prefix or any generated CSV path. Example: dataset_aop6m.csv "
            "will read dataset_aop6m_frames.csv, dataset_aop6m_points.csv, "
            "dataset_aop6m_targets.csv, and dataset_aop6m_vitals.csv."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path. Defaults to <prefix>_grid_windows.csv.",
    )
    parser.add_argument("--x-min", type=float, default=-3.0)
    parser.add_argument("--x-max", type=float, default=3.0)
    parser.add_argument("--y-min", type=float, default=0.0)
    parser.add_argument("--y-max", type=float, default=6.0)
    parser.add_argument(
        "--cell-size",
        type=float,
        default=0.5,
        help="Grid cell size in meters. 0.5m creates a 12x12 grid for -3..3m and 0..6m.",
    )
    parser.add_argument(
        "--window-sec",
        type=float,
        default=5.0,
        help="Seconds per ML sample window.",
    )
    parser.add_argument(
        "--stride-sec",
        type=float,
        default=None,
        help="Window stride in seconds. Defaults to window-sec for non-overlapping windows.",
    )
    parser.add_argument(
        "--frame-period-sec",
        type=float,
        default=0.09,
        help="Radar frame period in seconds. The current AOP cfg uses 90 ms.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_path, window_count, x_cells, y_cells = build_grid_dataset(args)
    print(f"Grid dataset written: {output_path}")
    print(f"Windows: {window_count}")
    print(f"Grid: {x_cells} x {y_cells}, cell_size={args.cell_size}m")


if __name__ == "__main__":
    main()
