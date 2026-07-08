from __future__ import annotations

import argparse
import math
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


CSV_NAME = "dataset_aop6m_grid_windows.csv"
OUT_DIR = Path("outputs") / "iwr6843_grid_visuals"


def find_csv(csv_path: str | None = None) -> Path:
    if csv_path:
        candidate = Path(csv_path)
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Could not find {candidate}")

    local = Path(CSV_NAME)
    if local.exists():
        return local

    roots = [Path.cwd(), Path("C:/")]
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            candidate = entry / CSV_NAME
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue

    raise FileNotFoundError(f"Could not find {CSV_NAME}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render IWR6843 grid window CSV files into heatmap PNG/GIF artifacts."
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Grid window CSV path. Defaults to dataset_aop6m_grid_windows.csv discovery.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(OUT_DIR),
        help="Output directory for PNG/GIF/summary CSV artifacts.",
    )
    return parser.parse_args()


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
    ]
    for path in candidates:
        try:
            if path.exists():
                return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default()


FONT = {
    "title": load_font(36, True),
    "subtitle": load_font(19),
    "panel": load_font(22, True),
    "axis": load_font(16),
    "small": load_font(13),
    "tiny": load_font(11),
}


def metric_matrix(row: pd.Series | pd.DataFrame, metric: str, y_cells: int, x_cells: int) -> np.ndarray:
    matrix = np.zeros((y_cells, x_cells), dtype=float)
    if isinstance(row, pd.DataFrame):
        source = row.sum(numeric_only=True)
    else:
        source = row

    for y in range(y_cells):
        for x in range(x_cells):
            col = f"cell_y{y:02d}_x{x:02d}_{metric}"
            if col in source.index:
                value = source[col]
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = 0.0
                if math.isfinite(value):
                    matrix[y, x] = value
    return matrix


def build_palette(stops: list[tuple[float, tuple[int, int, int]]], steps: int = 256) -> list[tuple[int, int, int]]:
    palette: list[tuple[int, int, int]] = []
    stops = sorted(stops)
    for i in range(steps):
        t = i / (steps - 1)
        for idx in range(len(stops) - 1):
            t0, c0 = stops[idx]
            t1, c1 = stops[idx + 1]
            if t0 <= t <= t1:
                local = 0 if t1 == t0 else (t - t0) / (t1 - t0)
                color = tuple(int(c0[j] + (c1[j] - c0[j]) * local) for j in range(3))
                palette.append(color)
                break
        else:
            palette.append(stops[-1][1])
    return palette


PALETTE = build_palette(
    [
        (0.00, (245, 248, 250)),
        (0.10, (210, 232, 234)),
        (0.35, (77, 166, 177)),
        (0.65, (30, 94, 150)),
        (0.88, (232, 160, 68)),
        (1.00, (126, 43, 38)),
    ]
)


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def color_for(value: float, vmax: float, *, log_scale: bool) -> tuple[int, int, int]:
    if vmax <= 0 or value <= 0:
        return PALETTE[0]
    if log_scale:
        t = math.log1p(value) / math.log1p(vmax)
    else:
        t = value / vmax
    idx = max(0, min(255, int(t * 255)))
    return PALETTE[idx]


def format_value(value: float) -> str:
    if value == 0:
        return ""
    if abs(value) >= 1_000_000:
        return f"{value:.1e}"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f}k"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def draw_heatmap(
    draw: ImageDraw.ImageDraw,
    matrix: np.ndarray,
    box: tuple[int, int, int, int],
    title: str,
    subtitle: str,
    x_min: float,
    y_min: float,
    cell_size_m: float,
    *,
    log_scale: bool = True,
    cap_percentile: float | None = None,
    show_values: bool = False,
    marker: tuple[float, float] | None = None,
) -> None:
    left, top, right, bottom = box
    draw.text((left, top), title, font=FONT["panel"], fill=(29, 42, 55))
    draw.text((left, top + 29), subtitle, font=FONT["small"], fill=(93, 105, 116))

    plot_left = left + 54
    plot_top = top + 62
    plot_right = right - 24
    plot_bottom = bottom - 48
    y_cells, x_cells = matrix.shape
    cell_w = (plot_right - plot_left) / x_cells
    cell_h = (plot_bottom - plot_top) / y_cells

    positives = matrix[matrix > 0]
    if positives.size == 0:
        vmax = 0.0
    elif cap_percentile is None:
        vmax = float(positives.max())
    else:
        vmax = float(np.percentile(positives, cap_percentile))
        vmax = max(vmax, float(positives.min()))

    for y in range(y_cells):
        for x in range(x_cells):
            value = float(matrix[y, x])
            clipped = min(value, vmax) if vmax > 0 else value
            color = color_for(clipped, vmax, log_scale=log_scale)
            px0 = plot_left + int(round(x * cell_w))
            px1 = plot_left + int(round((x + 1) * cell_w))
            py1 = plot_bottom - int(round(y * cell_h))
            py0 = plot_bottom - int(round((y + 1) * cell_h))
            draw.rectangle((px0, py0, px1, py1), fill=color, outline=(225, 230, 235))
            if show_values and value > 0:
                label = format_value(value)
                tw, th = text_size(draw, label, FONT["tiny"])
                luminance = 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]
                fill = (255, 255, 255) if luminance < 120 else (28, 38, 48)
                draw.text((px0 + (px1 - px0 - tw) / 2, py0 + (py1 - py0 - th) / 2), label, font=FONT["tiny"], fill=fill)

    draw.rectangle((plot_left, plot_top, plot_right, plot_bottom), outline=(48, 61, 74), width=2)

    for x in range(x_cells + 1):
        if x % 2 == 0:
            meter = x_min + x * cell_size_m
            px = plot_left + x * cell_w
            label = f"{meter:g}"
            tw, _ = text_size(draw, label, FONT["small"])
            draw.text((px - tw / 2, plot_bottom + 8), label, font=FONT["small"], fill=(76, 86, 96))
    for y in range(y_cells + 1):
        if y % 2 == 0:
            meter = y_min + y * cell_size_m
            py = plot_bottom - y * cell_h
            label = f"{meter:g}"
            tw, th = text_size(draw, label, FONT["small"])
            draw.text((plot_left - 13 - tw, py - th / 2), label, font=FONT["small"], fill=(76, 86, 96))

    x_label = "X lateral (m)"
    tw, th = text_size(draw, x_label, FONT["axis"])
    draw.text((plot_left + (plot_right - plot_left - tw) / 2, bottom - 22), x_label, font=FONT["axis"], fill=(44, 54, 65))
    y_label = "Y range (m)"
    draw.text((left, plot_top + (plot_bottom - plot_top) / 2 - 8), y_label, font=FONT["axis"], fill=(44, 54, 65))

    if marker is not None:
        mx, my = marker
        px = plot_left + ((mx - x_min) / cell_size_m + 0.5) * cell_w
        py = plot_bottom - ((my - y_min) / cell_size_m + 0.5) * cell_h
        r = 9
        draw.ellipse((px - r, py - r, px + r, py + r), outline=(255, 255, 255), width=4)
        draw.ellipse((px - r, py - r, px + r, py + r), outline=(31, 42, 55), width=2)

    if vmax > 0:
        legend_w = 126
        legend_h = 10
        lx = plot_right - legend_w
        ly = top + 36
        for i in range(legend_w):
            idx = int(i / (legend_w - 1) * 255)
            draw.line((lx + i, ly, lx + i, ly + legend_h), fill=PALETTE[idx])
        draw.rectangle((lx, ly, lx + legend_w, ly + legend_h), outline=(192, 199, 207))
        vmax_label = f"max {format_value(vmax)}"
        draw.text((lx + legend_w + 7, ly - 3), vmax_label, font=FONT["tiny"], fill=(83, 94, 105))


def draw_summary(df: pd.DataFrame, csv_path: Path, output_path: Path) -> None:
    first = df.iloc[0]
    x_cells = int(first["x_cells"])
    y_cells = int(first["y_cells"])
    x_min = float(first["x_min"])
    y_min = float(first["y_min"])
    cell_size = float(first["cell_size"])
    time_start = float(df["window_start_sec"].min())
    time_end = float(df["window_end_sec"].max())

    panels = [
        ("point_count", "Point detections", "sum over all windows, log color", True, None, False),
        ("target_count", "Tracked targets", "sum over all windows", True, None, True),
        ("vital_count", "Vital detections", "sum over all windows", True, None, True),
        ("motion_sum", "Motion energy", "sum, log color capped at p95", True, 95.0, False),
    ]

    width, height = 1600, 1220
    img = Image.new("RGB", (width, height), (250, 251, 252))
    draw = ImageDraw.Draw(img)

    title = "IWR6843 AOPEVM 2D Grid Summary"
    draw.text((52, 34), title, font=FONT["title"], fill=(23, 33, 43))
    subtitle = (
        f"{len(df)} windows | {time_start:.1f}-{time_end:.1f}s | grid {x_cells}x{y_cells} | "
        f"cell {cell_size:g}m | source: {csv_path.name}"
    )
    draw.text((54, 82), subtitle, font=FONT["subtitle"], fill=(88, 99, 110))

    warning_rows = int((pd.to_numeric(df.get("parse_warning_frame_count", 0), errors="coerce").fillna(0) > 0).sum())
    unknown_rows = int((pd.to_numeric(df.get("unknown_tlv_frame_count", 0), errors="coerce").fillna(0) > 0).sum())
    note = f"Data quality: parse warnings in {warning_rows} windows, unknown TLVs in {unknown_rows} windows."
    draw.text((54, 111), note, font=FONT["small"], fill=(112, 78, 49))

    boxes = [
        (48, 152, 776, 662),
        (824, 152, 1552, 662),
        (48, 704, 776, 1164),
        (824, 704, 1552, 1164),
    ]

    for box, (metric, panel_title, panel_subtitle, log_scale, cap, show_values) in zip(boxes, panels):
        matrix = metric_matrix(df, metric, y_cells, x_cells)
        if metric == "motion_sum":
            finite = matrix[np.isfinite(matrix)]
            extreme = finite[finite > 1e6]
            if extreme.size:
                panel_subtitle += f" ({extreme.size} extreme cells capped)"
        draw_heatmap(
            draw,
            matrix,
            box,
            panel_title,
            panel_subtitle,
            x_min,
            y_min,
            cell_size,
            log_scale=log_scale,
            cap_percentile=cap,
            show_values=show_values,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=95)


def draw_path(df: pd.DataFrame, output_path: Path) -> None:
    first = df.iloc[0]
    x_cells = int(first["x_cells"])
    y_cells = int(first["y_cells"])
    x_min = float(first["x_min"])
    y_min = float(first["y_min"])
    cell_size = float(first["cell_size"])
    x_max = x_min + x_cells * cell_size
    y_max = y_min + y_cells * cell_size

    width, height = 1100, 980
    img = Image.new("RGB", (width, height), (250, 251, 252))
    draw = ImageDraw.Draw(img)

    draw.text((48, 36), "Dominant Cell Path by Window", font=FONT["title"], fill=(23, 33, 43))
    draw.text((50, 84), "Circle color follows time; larger circles mean higher dominant cell score.", font=FONT["subtitle"], fill=(88, 99, 110))

    left, top, right, bottom = 118, 136, 980, 862
    plot_w = right - left
    plot_h = bottom - top
    draw.rectangle((left, top, right, bottom), fill=(246, 248, 250), outline=(48, 61, 74), width=2)

    for x in range(x_cells + 1):
        px = left + x / x_cells * plot_w
        draw.line((px, top, px, bottom), fill=(218, 225, 232))
        if x % 2 == 0:
            meter = x_min + x * cell_size
            label = f"{meter:g}"
            tw, _ = text_size(draw, label, FONT["small"])
            draw.text((px - tw / 2, bottom + 10), label, font=FONT["small"], fill=(76, 86, 96))

    for y in range(y_cells + 1):
        py = bottom - y / y_cells * plot_h
        draw.line((left, py, right, py), fill=(218, 225, 232))
        if y % 2 == 0:
            meter = y_min + y * cell_size
            label = f"{meter:g}"
            tw, th = text_size(draw, label, FONT["small"])
            draw.text((left - 14 - tw, py - th / 2), label, font=FONT["small"], fill=(76, 86, 96))

    points: list[tuple[float, float, float, int]] = []
    for _, row in df.iterrows():
        try:
            x = float(row["dominant_cell_x_m"])
            y = float(row["dominant_cell_y_m"])
            score = float(row["dominant_cell_score"])
            idx = int(row["window_index"])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in [x, y, score]):
            continue
        if x_min <= x <= x_max and y_min <= y <= y_max:
            px = left + (x - x_min) / (x_max - x_min) * plot_w
            py = bottom - (y - y_min) / (y_max - y_min) * plot_h
            points.append((px, py, score, idx))

    if len(points) > 1:
        for p0, p1 in zip(points, points[1:]):
            draw.line((p0[0], p0[1], p1[0], p1[1]), fill=(60, 83, 102), width=3)

    max_score = max((p[2] for p in points), default=1.0)
    for i, (px, py, score, idx) in enumerate(points):
        t = i / max(1, len(points) - 1)
        color = PALETTE[int(t * 255)]
        radius = 7 + 14 * math.sqrt(score / max_score) if max_score else 8
        draw.ellipse((px - radius, py - radius, px + radius, py + radius), fill=color, outline=(31, 42, 55), width=2)
        if i == 0 or i == len(points) - 1 or idx % 3 == 0:
            label = str(idx)
            tw, th = text_size(draw, label, FONT["tiny"])
            draw.rectangle((px + radius - 1, py - radius - th - 5, px + radius + tw + 5, py - radius), fill=(255, 255, 255), outline=(205, 212, 219))
            draw.text((px + radius + 2, py - radius - th - 3), label, font=FONT["tiny"], fill=(41, 51, 62))

    x_label = "X lateral (m)"
    tw, _ = text_size(draw, x_label, FONT["axis"])
    draw.text((left + (plot_w - tw) / 2, height - 66), x_label, font=FONT["axis"], fill=(44, 54, 65))
    draw.text((26, top + plot_h / 2 - 8), "Y range (m)", font=FONT["axis"], fill=(44, 54, 65))
    img.save(output_path, quality=95)


def make_animation(df: pd.DataFrame, output_path: Path) -> None:
    first = df.iloc[0]
    x_cells = int(first["x_cells"])
    y_cells = int(first["y_cells"])
    x_min = float(first["x_min"])
    y_min = float(first["y_min"])
    cell_size = float(first["cell_size"])

    matrices = [metric_matrix(row, "point_count", y_cells, x_cells) for _, row in df.iterrows()]
    global_max = max((float(m.max()) for m in matrices), default=1.0)
    frames: list[Image.Image] = []

    for idx, matrix in enumerate(matrices):
        img = Image.new("RGB", (720, 720), (250, 251, 252))
        draw = ImageDraw.Draw(img)
        row = df.iloc[idx]
        title = f"Point Grid Window {int(row['window_index'])}"
        draw.text((32, 24), title, font=FONT["panel"], fill=(23, 33, 43))
        draw.text(
            (34, 54),
            f"{float(row['window_start_sec']):.1f}-{float(row['window_end_sec']):.1f}s | point_total {float(row['point_total']):.0f}",
            font=FONT["small"],
            fill=(88, 99, 110),
        )
        draw_heatmap(
            draw,
            matrix,
            (32, 84, 688, 690),
            "",
            "",
            x_min,
            y_min,
            cell_size,
            log_scale=True,
            cap_percentile=None,
            show_values=False,
            marker=(float(row["dominant_cell_x_m"]), float(row["dominant_cell_y_m"])),
        )
        frames.append(img)

    if frames:
        duration = 650 if len(frames) <= 40 else 350
        frames[0].save(output_path, save_all=True, append_images=frames[1:], duration=duration, loop=0)


def write_summary(df: pd.DataFrame, output_path: Path) -> None:
    cols = [
        "window_index",
        "window_start_sec",
        "window_end_sec",
        "point_total",
        "target_total",
        "vital_total",
        "dominant_cell_x_m",
        "dominant_cell_y_m",
        "dominant_cell_score",
        "presence_ratio",
        "parse_warning_frame_count",
        "unknown_tlv_frame_count",
    ]
    available = [c for c in cols if c in df.columns]
    df[available].to_csv(output_path, index=False)


def main() -> None:
    args = parse_args()
    csv_path = find_csv(args.csv)
    df = pd.read_csv(csv_path)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_png = out_dir / "iwr6843_grid_summary.png"
    path_png = out_dir / "iwr6843_dominant_cell_path.png"
    gif_path = out_dir / "iwr6843_point_windows.gif"
    summary_csv = out_dir / "iwr6843_window_summary.csv"

    draw_summary(df, csv_path, summary_png)
    draw_path(df, path_png)
    make_animation(df, gif_path)
    write_summary(df, summary_csv)

    print(f"source={csv_path}")
    print(f"rows={len(df)}")
    print(f"summary_png={summary_png.resolve()}")
    print(f"path_png={path_png.resolve()}")
    print(f"gif={gif_path.resolve()}")
    print(f"summary_csv={summary_csv.resolve()}")


if __name__ == "__main__":
    main()
