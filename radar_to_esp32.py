import argparse
import csv
from contextlib import ExitStack
from pathlib import Path
import struct
import time

import serial


MAGIC_WORD = b"\x02\x01\x04\x03\x06\x05\x08\x07"
HEADER_LEN = 40
TLV_HEADER_LEN = 8
TLV_VITAL_SIGNS = 1040
VITAL_SIGNS_STRUCT = "<2H33f"

CSV_FIELDS = [
    "timestamp",
    "session_id",
    "label",
    "cfg",
    "frame",
    "num_detected_obj",
    "num_tlvs",
    "packet_len",
    "tlv_summary",
    "has_vital",
    "target_id",
    "range_bin",
    "breath_deviation",
    "heart_rate",
    "breath_rate",
]

HEART_RATE_MIN = 0.1
BREATH_RATE_MIN = 0.1
BREATH_DEV_MIN = 0.02
CONFIRM_TIME_SECONDS = 5.0
LOST_GRACE_SECONDS = 3.0


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
    saw_sensor_start_done = False

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

                return "Done" in decoded

            print(f"[WARN] No CLI response for: {line}")
            return False

        # A previous run can leave the radar streaming if the host process was killed.
        # sensorStop is safe to try before replaying the cfg and prevents sensorStart hangs.
        send_line("sensorStop")

        with open(cfg_path, "r", encoding="utf-8") as cfg:
            for raw_line in cfg:
                line = raw_line.strip()
                if not line or line.startswith("%") or line.startswith("#"):
                    continue

                is_sensor_start = line == "sensorStart"
                command_done = send_line(line)
                if is_sensor_start:
                    if command_done:
                        saw_sensor_start_done = True
                    else:
                        print(
                            "[WARN] sensorStart did not finish. Stop this run, press RST.SW, "
                            "wait 5 seconds, and run again."
                        )
                        break

    if not saw_sensor_start_done:
        raise RuntimeError(
            "sensorStart did not return 'Done'. Check that CLI port is the IWR Enhanced COM port, "
            "close other serial tools, press RST.SW, and run again."
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


def write_frame_csv(csv_writer, session_id, label, cfg_name, frame, vitals_list, timestamp):
    base_row = {
        "timestamp": f"{timestamp:.3f}",
        "session_id": session_id,
        "label": label,
        "cfg": cfg_name,
        "frame": frame["frame"],
        "num_detected_obj": frame["num_detected_obj"],
        "num_tlvs": frame["num_tlvs"],
        "packet_len": frame["packet_len"],
        "tlv_summary": make_tlv_summary(frame),
    }

    if not vitals_list:
        row = {
            **base_row,
            "has_vital": 0,
            "target_id": "",
            "range_bin": "",
            "breath_deviation": "",
            "heart_rate": "",
            "breath_rate": "",
        }
        csv_writer.writerow(row)
        return

    for vitals in vitals_list:
        row = {
            **base_row,
            "has_vital": 1,
            "target_id": vitals["id"],
            "range_bin": vitals["range_bin"],
            "breath_deviation": f"{vitals['breath_deviation']:.6f}",
            "heart_rate": f"{vitals['heart_rate']:.3f}",
            "breath_rate": f"{vitals['breath_rate']:.3f}",
        }
        csv_writer.writerow(row)


def open_esp_serial(port, baud):
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
    except serial.SerialException:
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
    except serial.SerialException:
        return

    if not feedback:
        return

    for line in feedback.splitlines():
        print(f"ESP32 -> {line.strip()}")


def main():
    script_dir = Path(__file__).parent
    default_cfg = (
        script_dir
        / "Vital_Signs_With_People_Tracking"
        / "chirp_configs"
        / "vital_signs_AOP_2m.cfg"
    )
    typo_cfg = (
        script_dir
        / "Vital_Signs_With_People_Tracking"
        / "chrip_configs"
        / "vital_signs_AOP_2m.cfg"
    )

    if not default_cfg.exists() and typo_cfg.exists():
        default_cfg = typo_cfg

    parser = argparse.ArgumentParser(
        description="Read IWR6843 data UART and forward a short summary to ESP32."
    )
    parser.add_argument("--cli-port", default="COM3")
    parser.add_argument("--data-port", default="COM5")
    parser.add_argument("--esp-port", default="COM7")
    parser.add_argument(
        "--cfg",
        default=str(default_cfg) if default_cfg.exists() else None,
        help="TI demo cfg file to send through CLI port. Defaults to Vital_Signs_With_People_Tracking/chirp_configs/vital_signs_AOP_2m.cfg.",
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
        help="Print raw COM5 byte counts and a short hex preview before parsed frames appear.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Append parsed radar rows to this CSV file for machine-learning dataset collection.",
    )
    parser.add_argument(
        "--label",
        default="unlabeled",
        help="Ground-truth label to write into CSV rows for the current recording session.",
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
        print("No cfg file was found. Use --cfg PATH or place vital_signs_AOP_2m.cfg under Vital_Signs_With_People_Tracking/chirp_configs.")

    session_id = args.session_id or time.strftime("session_%Y%m%d_%H%M%S")
    if args.cfg_name:
        cfg_name = args.cfg_name
    elif args.cfg:
        cfg_name = Path(args.cfg).name
    else:
        cfg_name = "current_radar_cfg"

    with ExitStack() as stack:
        csv_writer = None
        csv_file = None
        if args.csv:
            csv_path = Path(args.csv)
            csv_file_exists = csv_path.exists() and csv_path.stat().st_size > 0
            csv_file = stack.enter_context(
                open(csv_path, "a", newline="", encoding="utf-8-sig")
            )
            csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            if not csv_file_exists:
                csv_writer.writeheader()
            print(f"CSV logging enabled: {csv_path}")
            print(f"CSV session_id={session_id}, label={args.label}, cfg={cfg_name}")

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
        alert_state = AlertState() if args.esp_mode == "alert" else None

        while True:
            available = radar_data.in_waiting
            if available <= 0:
                now = time.time()
                if now - last_raw_report_time > 1.0:
                    print("Waiting for COM5 data...")
                    last_raw_report_time = now
                time.sleep(0.02)
                continue

            chunk = radar_data.read(available)
            if not chunk:
                time.sleep(0.02)
                continue

            raw_bytes_since_report += len(chunk)
            buffer += chunk
            frames, buffer = parse_frames(buffer)

            now = time.time()
            if args.raw_debug and now - last_raw_report_time > 1.0:
                preview = chunk[:24].hex(" ")
                magic_seen = "yes" if MAGIC_WORD in buffer or MAGIC_WORD in chunk else "no"
                print(
                    f"RAW COM5 bytes={raw_bytes_since_report}, "
                    f"buffer={len(buffer)}, magic_seen={magic_seen}, hex={preview}"
                )
                raw_bytes_since_report = 0
                last_raw_report_time = now

            for frame in frames:
                messages = make_frame_messages(frame)
                vitals_list = get_frame_vitals(frame)
                if csv_writer is not None:
                    write_frame_csv(
                        csv_writer,
                        session_id,
                        args.label,
                        cfg_name,
                        frame,
                        vitals_list,
                        now,
                    )
                    csv_file.flush()

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
