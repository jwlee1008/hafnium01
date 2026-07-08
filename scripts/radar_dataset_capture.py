import argparse
import csv
from contextlib import ExitStack
import math
from pathlib import Path
import struct
import time


MAGIC_WORD = b"\x02\x01\x04\x03\x06\x05\x08\x07"
HEADER_LEN = 40
TLV_HEADER_LEN = 8
TLV_DETECTED_POINTS = 1
TLV_SIDE_INFO = 7
TLV_TARGET_LIST = 1010
TLV_TARGET_INDEX = 1011
TLV_COMPRESSED_POINTS = 1020
TLV_PRESENCE_INDICATION = 1021
TLV_VITAL_SIGNS = 1040

DETECTED_POINT_STRUCT = "<4f"
SIDE_INFO_STRUCT = "<2h"
COMPRESSED_POINT_UNIT_STRUCT = "<5f"
COMPRESSED_POINT_STRUCT = "<bbhHH"
TARGET_STRUCT = "<I9f16f2f"
VITAL_SIGNS_STRUCT = "<2H33f"

COMMON_CSV_FIELDS = [
    "timestamp",
    "session_id",
    "cfg",
    "frame",
    "sub_frame",
]

FRAME_CSV_FIELDS = [
    *COMMON_CSV_FIELDS,
    "version",
    "platform",
    "time_cpu_cycles",
    "num_detected_obj",
    "num_tlvs",
    "packet_len",
    "tlv_summary",
    "presence_indication",
    "point_count",
    "target_count",
    "target_index_count",
    "vital_count",
    "unknown_tlv_summary",
]

POINT_CSV_FIELDS = [
    *COMMON_CSV_FIELDS,
    "point_index",
    "tlv_type",
    "point_format",
    "x_m",
    "y_m",
    "z_m",
    "range_m",
    "azimuth_rad",
    "elevation_rad",
    "doppler_mps",
    "snr_db",
    "noise_db",
    "raw_range",
    "raw_azimuth",
    "raw_elevation",
    "raw_doppler",
    "raw_snr",
    "raw_noise",
    "range_unit",
    "azimuth_unit",
    "elevation_unit",
    "doppler_unit",
    "snr_unit",
]

TARGET_CSV_FIELDS = [
    *COMMON_CSV_FIELDS,
    "target_index",
    "target_id",
    "pos_x_m",
    "pos_y_m",
    "pos_z_m",
    "vel_x_mps",
    "vel_y_mps",
    "vel_z_mps",
    "acc_x_mps2",
    "acc_y_mps2",
    "acc_z_mps2",
    *[f"ec{i}" for i in range(16)],
    "g",
    "confidence",
]

TARGET_INDEX_CSV_FIELDS = [
    *COMMON_CSV_FIELDS,
    "point_index",
    "target_index",
    "raw_target_index",
]

VITAL_CSV_FIELDS = [
    *COMMON_CSV_FIELDS,
    "vital_index",
    "target_id",
    "range_bin",
    "breath_deviation",
    "heart_rate",
    "breath_rate",
    "heart_waveform",
    "breath_waveform",
]

CSV_FIELDSETS = {
    "frames": FRAME_CSV_FIELDS,
    "points": POINT_CSV_FIELDS,
    "targets": TARGET_CSV_FIELDS,
    "target_indexes": TARGET_INDEX_CSV_FIELDS,
    "vitals": VITAL_CSV_FIELDS,
}

HEART_RATE_MIN = 0.1
BREATH_RATE_MIN = 0.1
BREATH_DEV_MIN = 0.02
CONFIRM_TIME_SECONDS = 5.0
LOST_GRACE_SECONDS = 3.0


def require_serial():
    try:
        import serial
    except ImportError as error:
        raise RuntimeError(
            "pyserial is required for radar serial ports. Run: pip install -r requirements.txt"
        ) from error
    return serial


class AlertState:
    def __init__(self):
        self.first_detected_time = None
        self.last_detected_time = None
        self.survivor_detected = False

    def update(self, frame_number, vitals, now):
        heart_rate = vitals["heart_rate"]
        breath_rate = vitals["breath_rate"]
        breath_deviation = vitals["breath_deviation"]

        heart_detected = heart_rate > HEART_RATE_MIN
        breath_detected = breath_rate > BREATH_RATE_MIN
        breath_motion_detected = breath_deviation >= BREATH_DEV_MIN
        instant_detected = heart_detected or breath_detected or breath_motion_detected

        if instant_detected:
            if (
                self.first_detected_time is None
                or self.last_detected_time is None
                or now - self.last_detected_time > LOST_GRACE_SECONDS
            ):
                self.first_detected_time = now

            self.last_detected_time = now
            self.survivor_detected = (
                now - self.first_detected_time
            ) >= CONFIRM_TIME_SECONDS
        elif (
            self.last_detected_time is None
            or now - self.last_detected_time > LOST_GRACE_SECONDS
        ):
            self.first_detected_time = None
            self.survivor_detected = False

        if not instant_detected:
            level = 0
            status = "CLEAR"
            confidence = 0.0
        else:
            confirm_progress = 0.0
            if self.first_detected_time is not None:
                confirm_progress = min(
                    1.0, max(0.0, (now - self.first_detected_time) / CONFIRM_TIME_SECONDS)
                )

            signal_score = 0.0
            if heart_detected:
                signal_score += 0.35
            if breath_detected:
                signal_score += 0.35
            signal_score += min(0.30, max(0.0, breath_deviation / 0.10) * 0.30)

            confidence = min(1.0, (confirm_progress * 0.55) + (signal_score * 0.45))

            if self.survivor_detected:
                level = 2
                status = "SURVIVOR"
                confidence = max(confidence, 0.75)
            else:
                level = 1
                status = "CANDIDATE"

        return (
            "ALERT,"
            f"frame={frame_number},"
            f"status={status},"
            f"level={level},"
            f"confidence={confidence:.2f},"
            f"id={vitals['id']},"
            f"rangeBin={vitals['range_bin']},"
            f"heartRate={heart_rate:.1f},"
            f"breathRate={breath_rate:.1f},"
            f"breathDev={breath_deviation:.4f}"
        )


def send_cfg(cli_port, cfg_path):
    serial = require_serial()
    saw_sensor_start_done = False
    sensor_start_response = ""

    with serial.Serial(cli_port, 115200, timeout=0.5) as cli:
        time.sleep(0.5)

        def read_response(is_sensor_start):
            response = b""
            end_time = time.time() + (20.0 if is_sensor_start else 0.5)
            while time.time() < end_time:
                chunk = cli.read(cli.in_waiting or 1)
                if chunk:
                    response += chunk
                    decoded_so_far = response.decode("ascii", errors="replace")
                    if "not recognized as a CLI command" in decoded_so_far:
                        break
                    if "Error" in decoded_so_far:
                        break
                    if is_sensor_start and "Done" in decoded_so_far:
                        break
                elif response and not is_sensor_start:
                    break
            return response

        def send_line(line):
            is_sensor_start = line == "sensorStart"
            print(f"CLI -> {line}")
            cli.reset_input_buffer()
            cli.write((line + "\r\n").encode("ascii"))
            cli.flush()
            time.sleep(0.25 if is_sensor_start else 0.08)

            response = read_response(is_sensor_start)
            if response:
                decoded = response.decode("ascii", errors="replace").strip()
                print(decoded)

                if "not recognized as a CLI command" in decoded:
                    raise RuntimeError(f"Radar rejected cfg command: {line}")

                return "Done" in decoded, decoded

            print(f"[WARN] No CLI response for: {line}")
            return False, ""

        with open(cfg_path, "r", encoding="utf-8") as cfg:
            for raw_line in cfg:
                line = raw_line.strip()
                if not line or line.startswith("%") or line.startswith("#"):
                    continue

                is_sensor_start = line == "sensorStart"
                command_done, response_text = send_line(line)
                if is_sensor_start:
                    if command_done:
                        saw_sensor_start_done = True
                    else:
                        sensor_start_response = response_text
                        print(
                            "[WARN] sensorStart did not finish. Stop this run, press RST.SW, "
                            "wait 5 seconds, and run again."
                        )
                        break

    if not saw_sensor_start_done:
        detail = f" Last CLI response: {sensor_start_response[:500]}" if sensor_start_response else ""
        raise RuntimeError(
            "sensorStart did not return 'Done'. Check that CLI port is the IWR Enhanced COM port, "
            "close other serial tools, press RST.SW, and run again."
            + detail
        )


def parse_frames(buffer):
    frames = []

    while True:
      magic_index = buffer.find(MAGIC_WORD)
      if magic_index < 0:
          keep = max(0, len(buffer) - len(MAGIC_WORD) + 1)
          return frames, buffer[keep:]

      if magic_index > 0:
          buffer = buffer[magic_index:]

      if len(buffer) < HEADER_LEN:
          return frames, buffer

      header_values = struct.unpack_from("<8I", buffer, 8)
      version = header_values[0]
      total_packet_len = header_values[1]
      platform = header_values[2]
      frame_number = header_values[3]
      time_cpu_cycles = header_values[4]
      num_detected_obj = header_values[5]
      num_tlvs = header_values[6]
      sub_frame_number = header_values[7]

      if total_packet_len < HEADER_LEN or total_packet_len > 65535:
          buffer = buffer[1:]
          continue

      if len(buffer) < total_packet_len:
          return frames, buffer

      packet = buffer[:total_packet_len]
      buffer = buffer[total_packet_len:]

      tlvs = []
      offset = HEADER_LEN
      for _ in range(num_tlvs):
          if offset + TLV_HEADER_LEN > len(packet):
              break

          tlv_type, tlv_length = struct.unpack_from("<2I", packet, offset)
          payload_start = offset + TLV_HEADER_LEN
          payload_end = payload_start + tlv_length

          if payload_end > len(packet):
              if offset + tlv_length <= len(packet) and tlv_length >= TLV_HEADER_LEN:
                  payload_start = offset + TLV_HEADER_LEN
                  payload_end = offset + tlv_length
              else:
                  break

          tlvs.append(
              {
                  "type": tlv_type,
                  "length": payload_end - payload_start,
                  "payload": packet[payload_start:payload_end],
              }
          )
          offset = payload_end

      frames.append(
          {
              "version": version,
              "platform": platform,
              "frame": frame_number,
              "time_cpu_cycles": time_cpu_cycles,
              "num_detected_obj": num_detected_obj,
              "num_tlvs": num_tlvs,
              "sub_frame": sub_frame_number,
              "packet_len": total_packet_len,
              "tlvs": tlvs,
          }
      )


def decode_vital_signs(payload):
    vitals_size = struct.calcsize(VITAL_SIGNS_STRUCT)
    if len(payload) < vitals_size:
        return None

    values = struct.unpack_from(VITAL_SIGNS_STRUCT, payload)
    return {
        "id": values[0],
        "range_bin": values[1],
        "breath_deviation": values[2],
        "heart_rate": values[3],
        "breath_rate": values[4],
        "heart_waveform": values[5:20],
        "breath_waveform": values[20:35],
    }


def format_float(value, digits=6):
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def format_float_list(values, digits=6):
    return ";".join(format_float(value, digits) for value in values)


def frame_base_row(session_id, cfg_name, frame, timestamp):
    return {
        "timestamp": f"{timestamp:.3f}",
        "session_id": session_id,
        "cfg": cfg_name,
        "frame": frame["frame"],
        "sub_frame": frame["sub_frame"],
    }


def decode_detected_points(payload):
    point_size = struct.calcsize(DETECTED_POINT_STRUCT)
    point_count = len(payload) // point_size
    points = []

    for point_index in range(point_count):
        offset = point_index * point_size
        x, y, z, doppler = struct.unpack_from(DETECTED_POINT_STRUCT, payload, offset)
        points.append(
            {
                "point_index": point_index,
                "tlv_type": TLV_DETECTED_POINTS,
                "point_format": "cartesian_float",
                "x_m": x,
                "y_m": y,
                "z_m": z,
                "range_m": math.sqrt((x * x) + (y * y) + (z * z)),
                "azimuth_rad": math.atan2(x, y) if x or y else 0.0,
                "elevation_rad": math.atan2(z, math.sqrt((x * x) + (y * y))),
                "doppler_mps": doppler,
                "snr_db": None,
                "noise_db": None,
                "raw_range": "",
                "raw_azimuth": "",
                "raw_elevation": "",
                "raw_doppler": "",
                "raw_snr": "",
                "raw_noise": "",
                "range_unit": "",
                "azimuth_unit": "",
                "elevation_unit": "",
                "doppler_unit": "",
                "snr_unit": "",
            }
        )

    return points


def decode_side_info(payload):
    side_info_size = struct.calcsize(SIDE_INFO_STRUCT)
    side_info_count = len(payload) // side_info_size
    side_info = []

    for point_index in range(side_info_count):
        offset = point_index * side_info_size
        snr, noise = struct.unpack_from(SIDE_INFO_STRUCT, payload, offset)
        side_info.append({"point_index": point_index, "snr_db": snr * 0.1, "noise_db": noise * 0.1})

    return side_info


def decode_compressed_points(payload):
    unit_size = struct.calcsize(COMPRESSED_POINT_UNIT_STRUCT)
    point_size = struct.calcsize(COMPRESSED_POINT_STRUCT)

    if len(payload) < unit_size:
        return []

    (
        elevation_unit,
        azimuth_unit,
        doppler_unit,
        range_unit,
        snr_unit,
    ) = struct.unpack_from(COMPRESSED_POINT_UNIT_STRUCT, payload)

    point_count = (len(payload) - unit_size) // point_size
    points = []

    for point_index in range(point_count):
        offset = unit_size + (point_index * point_size)
        raw_elevation, raw_azimuth, raw_doppler, raw_range, raw_snr = struct.unpack_from(
            COMPRESSED_POINT_STRUCT, payload, offset
        )
        elevation = raw_elevation * elevation_unit
        azimuth = raw_azimuth * azimuth_unit
        doppler = raw_doppler * doppler_unit
        range_m = raw_range * range_unit
        snr = raw_snr * snr_unit

        x = range_m * math.cos(elevation) * math.sin(azimuth)
        y = range_m * math.cos(elevation) * math.cos(azimuth)
        z = range_m * math.sin(elevation)

        points.append(
            {
                "point_index": point_index,
                "tlv_type": TLV_COMPRESSED_POINTS,
                "point_format": "compressed_spherical",
                "x_m": x,
                "y_m": y,
                "z_m": z,
                "range_m": range_m,
                "azimuth_rad": azimuth,
                "elevation_rad": elevation,
                "doppler_mps": doppler,
                "snr_db": snr,
                "noise_db": None,
                "raw_range": raw_range,
                "raw_azimuth": raw_azimuth,
                "raw_elevation": raw_elevation,
                "raw_doppler": raw_doppler,
                "raw_snr": raw_snr,
                "raw_noise": "",
                "range_unit": range_unit,
                "azimuth_unit": azimuth_unit,
                "elevation_unit": elevation_unit,
                "doppler_unit": doppler_unit,
                "snr_unit": snr_unit,
            }
        )

    return points


def decode_target_list(payload):
    target_size = struct.calcsize(TARGET_STRUCT)
    target_count = len(payload) // target_size
    targets = []

    for target_index in range(target_count):
        offset = target_index * target_size
        values = struct.unpack_from(TARGET_STRUCT, payload, offset)
        covariance = values[10:26]
        targets.append(
            {
                "target_index": target_index,
                "target_id": values[0],
                "pos_x_m": values[1],
                "pos_y_m": values[2],
                "pos_z_m": values[3],
                "vel_x_mps": values[4],
                "vel_y_mps": values[5],
                "vel_z_mps": values[6],
                "acc_x_mps2": values[7],
                "acc_y_mps2": values[8],
                "acc_z_mps2": values[9],
                **{f"ec{i}": covariance[i] for i in range(16)},
                "g": values[26],
                "confidence": values[27],
            }
        )

    return targets


def decode_target_indexes(payload):
    return [
        {
            "point_index": point_index,
            "target_index": raw_target_index,
            "raw_target_index": raw_target_index,
        }
        for point_index, raw_target_index in enumerate(payload)
    ]


def decode_presence_indication(payload):
    if len(payload) < 4:
        return ""
    return struct.unpack_from("<I", payload)[0]


def get_frame_points(frame):
    points = []
    side_info_by_index = {}

    for tlv in frame["tlvs"]:
        if tlv["type"] == TLV_SIDE_INFO:
            side_info_by_index = {
                item["point_index"]: item for item in decode_side_info(tlv["payload"])
            }

    for tlv in frame["tlvs"]:
        if tlv["type"] == TLV_DETECTED_POINTS:
            points.extend(decode_detected_points(tlv["payload"]))
        elif tlv["type"] == TLV_COMPRESSED_POINTS:
            points.extend(decode_compressed_points(tlv["payload"]))

    for point_index, point in enumerate(points):
        point["point_index"] = point_index
        if point["point_format"] == "cartesian_float" and point_index in side_info_by_index:
            point["snr_db"] = side_info_by_index[point_index]["snr_db"]
            point["noise_db"] = side_info_by_index[point_index]["noise_db"]
            point["raw_snr"] = side_info_by_index[point_index]["snr_db"]
            point["raw_noise"] = side_info_by_index[point_index]["noise_db"]

    return points


def get_frame_targets(frame):
    targets = []
    for tlv in frame["tlvs"]:
        if tlv["type"] == TLV_TARGET_LIST:
            targets.extend(decode_target_list(tlv["payload"]))
    return targets


def get_frame_target_indexes(frame):
    target_indexes = []
    for tlv in frame["tlvs"]:
        if tlv["type"] == TLV_TARGET_INDEX:
            target_indexes.extend(decode_target_indexes(tlv["payload"]))
    return target_indexes


def get_frame_presence_indication(frame):
    for tlv in frame["tlvs"]:
        if tlv["type"] == TLV_PRESENCE_INDICATION:
            return decode_presence_indication(tlv["payload"])
    return ""


def get_unknown_tlv_summary(frame):
    known_types = {
        TLV_DETECTED_POINTS,
        TLV_SIDE_INFO,
        TLV_TARGET_LIST,
        TLV_TARGET_INDEX,
        TLV_COMPRESSED_POINTS,
        TLV_PRESENCE_INDICATION,
        TLV_VITAL_SIGNS,
    }
    return ";".join(
        f"{tlv['type']}:{tlv['length']}"
        for tlv in frame["tlvs"]
        if tlv["type"] not in known_types
    )


def extract_frame_data(frame):
    return {
        "points": get_frame_points(frame),
        "targets": get_frame_targets(frame),
        "target_indexes": get_frame_target_indexes(frame),
        "vitals": get_frame_vitals(frame),
        "presence_indication": get_frame_presence_indication(frame),
        "unknown_tlv_summary": get_unknown_tlv_summary(frame),
    }


def make_tlv_summary(frame):
    return ";".join(f"{tlv['type']}:{tlv['length']}" for tlv in frame["tlvs"])


def make_frame_messages(frame):
    tlv_summary = make_tlv_summary(frame)
    messages = [
        (
            f"RADAR,{frame['frame']},{frame['num_detected_obj']},"
            f"{frame['num_tlvs']},{frame['packet_len']},{tlv_summary}"
        )
    ]

    for tlv in frame["tlvs"]:
        if tlv["type"] != TLV_VITAL_SIGNS:
            continue

        vitals = decode_vital_signs(tlv["payload"])
        if vitals is None:
            messages.append(f"VITAL,{frame['frame']},PARSE_ERROR,{tlv['length']}")
            continue

        messages.append(
            "VITAL,"
            f"{frame['frame']},"
            f"id={vitals['id']},"
            f"rangeBin={vitals['range_bin']},"
            f"breathDev={vitals['breath_deviation']:.4f},"
            f"heartRate={vitals['heart_rate']:.1f},"
            f"breathRate={vitals['breath_rate']:.1f}"
        )

    return messages


def get_frame_vitals(frame):
    vitals_list = []

    for tlv in frame["tlvs"]:
        if tlv["type"] != TLV_VITAL_SIGNS:
            continue

        vitals = decode_vital_signs(tlv["payload"])
        if vitals is not None:
            vitals_list.append(vitals)

    return vitals_list


def csv_output_paths(base_csv_path):
    base_path = Path(base_csv_path)
    if base_path.suffix.lower() == ".csv":
        prefix_path = base_path.with_suffix("")
    else:
        prefix_path = base_path

    return {
        name: prefix_path.with_name(f"{prefix_path.name}_{name}.csv")
        for name in CSV_FIELDSETS
    }


def open_csv_outputs(stack, base_csv_path):
    paths = csv_output_paths(base_csv_path)
    outputs = {}

    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists() and path.stat().st_size > 0
        csv_file = stack.enter_context(open(path, "a", newline="", encoding="utf-8-sig"))
        csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDSETS[name])
        if not file_exists:
            csv_writer.writeheader()
        outputs[name] = {"path": path, "file": csv_file, "writer": csv_writer}

    return outputs


def write_dataset_csv(csv_outputs, session_id, cfg_name, frame, frame_data, timestamp):
    base_row = frame_base_row(session_id, cfg_name, frame, timestamp)

    csv_outputs["frames"]["writer"].writerow(
        {
            **base_row,
            "version": frame["version"],
            "platform": frame["platform"],
            "time_cpu_cycles": frame["time_cpu_cycles"],
            "num_detected_obj": frame["num_detected_obj"],
            "num_tlvs": frame["num_tlvs"],
            "packet_len": frame["packet_len"],
            "tlv_summary": make_tlv_summary(frame),
            "presence_indication": frame_data["presence_indication"],
            "point_count": len(frame_data["points"]),
            "target_count": len(frame_data["targets"]),
            "target_index_count": len(frame_data["target_indexes"]),
            "vital_count": len(frame_data["vitals"]),
            "unknown_tlv_summary": frame_data["unknown_tlv_summary"],
        }
    )

    for point in frame_data["points"]:
        csv_outputs["points"]["writer"].writerow(
            {
                **base_row,
                "point_index": point["point_index"],
                "tlv_type": point["tlv_type"],
                "point_format": point["point_format"],
                "x_m": format_float(point["x_m"]),
                "y_m": format_float(point["y_m"]),
                "z_m": format_float(point["z_m"]),
                "range_m": format_float(point["range_m"]),
                "azimuth_rad": format_float(point["azimuth_rad"]),
                "elevation_rad": format_float(point["elevation_rad"]),
                "doppler_mps": format_float(point["doppler_mps"]),
                "snr_db": format_float(point["snr_db"]),
                "noise_db": format_float(point["noise_db"]),
                "raw_range": point["raw_range"],
                "raw_azimuth": point["raw_azimuth"],
                "raw_elevation": point["raw_elevation"],
                "raw_doppler": point["raw_doppler"],
                "raw_snr": point["raw_snr"],
                "raw_noise": point["raw_noise"],
                "range_unit": point["range_unit"],
                "azimuth_unit": point["azimuth_unit"],
                "elevation_unit": point["elevation_unit"],
                "doppler_unit": point["doppler_unit"],
                "snr_unit": point["snr_unit"],
            }
        )

    for target in frame_data["targets"]:
        csv_outputs["targets"]["writer"].writerow(
            {
                **base_row,
                "target_index": target["target_index"],
                "target_id": target["target_id"],
                "pos_x_m": format_float(target["pos_x_m"]),
                "pos_y_m": format_float(target["pos_y_m"]),
                "pos_z_m": format_float(target["pos_z_m"]),
                "vel_x_mps": format_float(target["vel_x_mps"]),
                "vel_y_mps": format_float(target["vel_y_mps"]),
                "vel_z_mps": format_float(target["vel_z_mps"]),
                "acc_x_mps2": format_float(target["acc_x_mps2"]),
                "acc_y_mps2": format_float(target["acc_y_mps2"]),
                "acc_z_mps2": format_float(target["acc_z_mps2"]),
                **{f"ec{i}": format_float(target[f"ec{i}"]) for i in range(16)},
                "g": format_float(target["g"]),
                "confidence": format_float(target["confidence"]),
            }
        )

    for target_index in frame_data["target_indexes"]:
        csv_outputs["target_indexes"]["writer"].writerow(
            {
                **base_row,
                "point_index": target_index["point_index"],
                "target_index": target_index["target_index"],
                "raw_target_index": target_index["raw_target_index"],
            }
        )

    for vital_index, vitals in enumerate(frame_data["vitals"]):
        csv_outputs["vitals"]["writer"].writerow(
            {
                **base_row,
                "vital_index": vital_index,
                "target_id": vitals["id"],
                "range_bin": vitals["range_bin"],
                "breath_deviation": format_float(vitals["breath_deviation"]),
                "heart_rate": format_float(vitals["heart_rate"], 3),
                "breath_rate": format_float(vitals["breath_rate"], 3),
                "heart_waveform": format_float_list(vitals["heart_waveform"]),
                "breath_waveform": format_float_list(vitals["breath_waveform"]),
            }
        )


def open_esp_serial(port, baud):
    serial = require_serial()
    esp = serial.Serial(
        port,
        baud,
        timeout=0.1,
        write_timeout=2.0,
        rtscts=False,
        dsrdtr=False,
        xonxoff=False,
    )
    try:
        esp.dtr = False
        esp.rts = False
        esp.reset_input_buffer()
        esp.reset_output_buffer()
    except Exception:
        pass
    return esp


def print_esp_feedback(esp):
    if esp is None:
        return

    try:
        available = esp.in_waiting
        if available <= 0:
            return

        feedback = esp.read(available).decode("ascii", errors="replace").strip()
    except Exception:
        return

    if not feedback:
        return

    for line in feedback.splitlines():
        print(f"ESP32 -> {line.strip()}")


def main():
    script_dir = Path(__file__).parent
    project_root = Path(r"C:\한이음 프로젝트")
    default_cfg_candidates = [
        project_root / "configs" / "vital_signs_AOP_6m.cfg",
        script_dir / "configs" / "vital_signs_AOP_6m.cfg",
        script_dir
        / "Vital_Signs_With_People_Tracking"
        / "chirp_configs"
        / "vital_signs_AOP_6m.cfg",
        script_dir
        / "Vital_Signs_With_People_Tracking"
        / "chrip_configs"
        / "vital_signs_AOP_6m.cfg",
    ]
    default_cfg = next(
        (cfg_path for cfg_path in default_cfg_candidates if cfg_path.exists()), None
    )

    parser = argparse.ArgumentParser(
        description="Read IWR6843 data UART and forward a short summary to ESP32."
    )
    parser.add_argument("--cli-port", default="COM3")
    parser.add_argument("--data-port", default="COM5")
    parser.add_argument("--esp-port", default="COM7")
    parser.add_argument(
        "--cfg",
        default=str(default_cfg) if default_cfg is not None else None,
        help=r"TI demo cfg file to send through CLI port. Defaults to C:\한이음 프로젝트\configs\vital_signs_AOP_6m.cfg when present.",
    )
    parser.add_argument(
        "--no-cfg",
        action="store_true",
        help="Skip sending cfg commands and only read the radar data port.",
    )
    parser.add_argument("--data-baud", type=int, default=921600)
    parser.add_argument("--esp-baud", type=int, default=115200)
    parser.add_argument(
        "--esp-open-wait",
        type=float,
        default=3.0,
        help="Seconds to wait after opening ESP32 before writing data.",
    )
    parser.add_argument(
        "--no-esp",
        action="store_true",
        help="Read only the radar data port without opening or forwarding to ESP32.",
    )
    parser.add_argument(
        "--delayed-esp",
        action="store_true",
        help="Open ESP32 only after the first radar frame is received.",
    )
    parser.add_argument(
        "--esp-vital-only",
        action="store_true",
        help="Deprecated. Use --esp-mode vital instead.",
    )
    parser.add_argument(
        "--esp-mode",
        choices=("none", "vital", "alert", "all"),
        default="none",
        help="What to forward to ESP32. Default is none for dataset collection without decision output.",
    )
    parser.add_argument(
        "--alert-interval",
        type=float,
        default=1.0,
        help="Minimum seconds between repeated ALERT messages when status does not change.",
    )
    parser.add_argument(
        "--esp-test",
        action="store_true",
        help="Only test writing one short ALERT line to ESP32 and exit.",
    )
    parser.add_argument(
        "--raw-debug",
        action="store_true",
        help="Print raw DATA-port byte counts and a short hex preview before parsed frames appear.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help=(
            "CSV output prefix for dataset collection. If dataset.csv is given, "
            "dataset_frames.csv, dataset_points.csv, dataset_targets.csv, "
            "dataset_target_indexes.csv, and dataset_vitals.csv are written."
        ),
    )
    parser.add_argument(
        "--label",
        default="unlabeled",
        help="Deprecated and ignored. Add ground-truth labels in a later dataset labeling step.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Recording session id for CSV rows. Defaults to a timestamp-based id.",
    )
    parser.add_argument(
        "--cfg-name",
        default=None,
        help="Short cfg name to store in CSV. Defaults to the cfg filename, or current_radar_cfg when --no-cfg is used.",
    )
    args = parser.parse_args()
    try:
        serial = require_serial()
    except RuntimeError as error:
        raise SystemExit(f"[ERROR] {error}")

    if args.esp_vital_only and args.esp_mode == "none":
        args.esp_mode = "vital"
        print("[INFO] --esp-vital-only is deprecated; forwarding VITAL lines.")

    if args.esp_test:
        test_message = (
            "ALERT,frame=TEST,status=SURVIVOR,level=2,confidence=0.95,"
            "id=0,rangeBin=0,heartRate=72.0,breathRate=12.0,breathDev=0.0400"
        )
        print(f"Opening ESP32 port {args.esp_port} @ {args.esp_baud}")
        with open_esp_serial(args.esp_port, args.esp_baud) as esp:
            print(f"ESP32 port opened: {args.esp_port}")
            print(f"Waiting {args.esp_open_wait:.1f}s for ESP32 serial...")
            time.sleep(args.esp_open_wait)
            print(f"Writing test message: {test_message}")
            esp.write((test_message + "\n").encode("ascii"))
            esp.flush()
            print("ESP32 write test done.")
            end_time = time.time() + 2.0
            while time.time() < end_time:
                print_esp_feedback(esp)
                time.sleep(0.05)
        return

    if args.cfg and not args.no_cfg:
        print(f"Using cfg: {args.cfg}")
        print("Sending radar cfg...")
        send_cfg(args.cli_port, args.cfg)
        print("Cfg sent.")
    elif not args.no_cfg:
        print(
            r"No cfg file was found. Use --cfg PATH or place vital_signs_AOP_6m.cfg under C:\한이음 프로젝트\configs."
        )

    session_id = args.session_id or time.strftime("session_%Y%m%d_%H%M%S")
    if args.cfg_name:
        cfg_name = args.cfg_name
    elif args.cfg:
        cfg_name = Path(args.cfg).name
    else:
        cfg_name = "current_radar_cfg"

    with ExitStack() as stack:
        csv_outputs = None
        if args.csv:
            csv_outputs = open_csv_outputs(stack, args.csv)
            print(f"CSV logging enabled with prefix: {Path(args.csv)}")
            for name, output in csv_outputs.items():
                print(f"  {name}: {output['path']}")
            print(f"CSV session_id={session_id}, cfg={cfg_name}")
            if args.label != "unlabeled":
                print("[INFO] --label is ignored. Add labels later during dataset labeling.")

        print(f"Opening radar data port {args.data_port} @ {args.data_baud}")
        radar_data = stack.enter_context(
            serial.Serial(args.data_port, args.data_baud, timeout=0.1)
        )
        print(f"Radar data port opened: {args.data_port}")

        if args.no_esp or args.esp_mode == "none":
            esp = None
            esp_disabled = True
            print("ESP32 forwarding disabled.")
        elif args.delayed_esp:
            esp = None
            esp_disabled = False
            print("ESP32 forwarding delayed until first radar frame.")
        else:
            print(f"Opening ESP32 port {args.esp_port} @ {args.esp_baud}")
            esp = stack.enter_context(open_esp_serial(args.esp_port, args.esp_baud))
            print(f"ESP32 port opened: {args.esp_port}")
            print(f"Waiting {args.esp_open_wait:.1f}s for ESP32 serial...")
            time.sleep(args.esp_open_wait)
            esp_disabled = False

        print("Reading radar data...")
        buffer = b""
        last_report_time = 0
        last_raw_report_time = time.time()
        last_alert_sent_time = 0
        last_alert_signature = None
        raw_bytes_since_report = 0
        bytes_since_frame = 0
        warned_text_data_port = False
        warned_no_magic = False
        alert_state = AlertState() if args.esp_mode == "alert" else None

        while True:
            available = radar_data.in_waiting
            if available <= 0:
                now = time.time()
                if now - last_raw_report_time > 1.0:
                    print(f"Waiting for radar data on {args.data_port}...")
                    last_raw_report_time = now
                time.sleep(0.02)
                continue

            chunk = radar_data.read(available)
            if not chunk:
                time.sleep(0.02)
                continue

            raw_bytes_since_report += len(chunk)
            bytes_since_frame += len(chunk)
            buffer += chunk
            frames, buffer = parse_frames(buffer)
            if frames:
                bytes_since_frame = 0

            if not warned_text_data_port:
                text_probe = chunk[:200].decode("ascii", errors="ignore")
                if "mmwDemo" in text_probe or "Done" in text_probe or "sensorStart" in text_probe:
                    print(
                        "[WARN] DATA port is returning CLI-like text. "
                        "CLI and DATA ports may be swapped."
                    )
                    warned_text_data_port = True

            if not frames and not warned_no_magic and bytes_since_frame >= 8192:
                print(
                    "[WARN] DATA bytes are arriving but no mmWave magic word was parsed. "
                    "Check DATA port, baud rate, firmware image, and cfg compatibility."
                )
                warned_no_magic = True

            now = time.time()
            if args.raw_debug and now - last_raw_report_time > 1.0:
                preview = chunk[:24].hex(" ")
                magic_seen = "yes" if MAGIC_WORD in buffer or MAGIC_WORD in chunk else "no"
                print(
                    f"RAW {args.data_port} bytes={raw_bytes_since_report}, "
                    f"buffer={len(buffer)}, magic_seen={magic_seen}, hex={preview}"
                )
                raw_bytes_since_report = 0
                last_raw_report_time = now

            for frame in frames:
                messages = make_frame_messages(frame)
                frame_data = extract_frame_data(frame)
                vitals_list = frame_data["vitals"]
                if csv_outputs is not None:
                    write_dataset_csv(
                        csv_outputs,
                        session_id,
                        cfg_name,
                        frame,
                        frame_data,
                        now,
                    )
                    for output in csv_outputs.values():
                        output["file"].flush()

                alert_messages = []
                if args.esp_mode == "alert":
                    for vitals in vitals_list:
                        alert_message = alert_state.update(frame["frame"], vitals, now)
                        alert_parts = alert_message.split(",")
                        alert_status = next(
                            part for part in alert_parts if part.startswith("status=")
                        )
                        alert_level = next(
                            part for part in alert_parts if part.startswith("level=")
                        )
                        alert_signature = f"{alert_status},{alert_level}"
                        should_send_alert = (
                            alert_signature != last_alert_signature
                            or now - last_alert_sent_time >= args.alert_interval
                        )
                        if should_send_alert:
                            alert_messages.append(alert_message)
                            last_alert_signature = alert_signature
                            last_alert_sent_time = now

                if args.esp_mode == "none":
                    esp_messages = []
                elif args.esp_mode == "alert":
                    esp_messages = alert_messages
                elif args.esp_mode == "vital":
                    esp_messages = [
                        message for message in messages if message.startswith("VITAL,")
                    ]
                else:
                    esp_messages = messages

                if esp is None and args.delayed_esp and not esp_disabled and esp_messages:
                    print(f"Opening ESP32 port {args.esp_port} @ {args.esp_baud}")
                    esp = stack.enter_context(open_esp_serial(args.esp_port, args.esp_baud))
                    print(f"ESP32 port opened: {args.esp_port}")
                    print(f"Waiting {args.esp_open_wait:.1f}s for ESP32 serial...")
                    time.sleep(args.esp_open_wait)

                if esp is not None and esp_messages:
                    for message in esp_messages:
                        try:
                            esp.write((message + "\n").encode("ascii"))
                            esp.flush()
                        except serial.SerialTimeoutException:
                            print("[WARN] ESP32 write timeout. Radar will keep running, but ESP32 forwarding is disabled.")
                            try:
                                esp.close()
                            except serial.SerialException:
                                pass
                            esp = None
                            esp_disabled = True
                            break
                    print_esp_feedback(esp)

                now = time.time()
                if now - last_report_time > 0.5:
                    for message in messages:
                        print(message)
                    for message in alert_messages:
                        print(message)
                    last_report_time = now


if __name__ == "__main__":
    main()
