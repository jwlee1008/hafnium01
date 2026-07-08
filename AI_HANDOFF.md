# AI Handoff - Hafnium01 Radar Program

Date: 2026-07-08
Repository: `jwlee1008/hafnium01`

## Purpose

This repository is an MVP for an IWR6843AOPEVM radar + ESP32-S3 workflow.
It collects radar vital-sign output on the PC, evaluates recent CSV windows,
and sends lightweight result/profile JSON to an ESP32 node.

Important: this code is not an LD2450 direct-ESP32 coordinate pipeline. In the
current architecture, the PC reads the IWR6843 USB CLI/DATA ports. The ESP32
does not receive raw IWR6843 DATA frames directly.

## Current Architecture

```text
IWR6843AOPEVM
  CLI USB  -> PC sends mmWave cfg commands
  DATA USB -> PC reads binary mmWave TLV frames

PC server.py
  serves dashboard at http://127.0.0.1:8787
  starts/stops radar_to_esp32.py collection jobs
  reads runtime CSV files
  computes rule-based live vital detection
  sends PC_RESULT_JSON to ESP32

ESP32-S3 firmware
  receives PROFILE_JSON and PC_RESULT_JSON over serial
  answers RESULT?
  returns either recent PC result or simulator fallback
```

## Main Finding From Debugging

The radar was likely failing because the checked-in/default runtime setup did
not match the target PC:

- `config/nodes.json` had macOS `/dev/cu.usbserial...` ports saved.
- New nodes could run with `--no-cfg` if `cfg_path` was empty.
- `pyserial` may be missing, which prevents serial scanning and collection.
- `sensorStart did not return Done` can happen when the CLI/DATA ports are
  swapped, another serial tool is open, the board needs reset, or the cfg does
  not match the flashed demo firmware.
- DATA bytes can arrive but still fail parsing if the selected DATA port,
  baud rate, firmware, or cfg is wrong.

## Fixes Applied

### `server.py`

- Added preflight checks before collection starts:
  - `pyserial` availability
  - empty CLI/DATA ports
  - identical CLI and DATA ports
  - Windows running with `/dev/...` macOS/Linux ports
  - missing cfg file
- Added serial port role suggestions returned by `/api/ports`.
- Resolved relative cfg paths against the app root.
- Set default node cfg path to `configs/vital_signs_AOP_6m.cfg`.
- Failed preflight now records `PRECHECK_FAILED` and a concrete reason.

### `radar_to_esp32.py`

- Moved `pyserial` import behind a clear runtime error.
- Added a clean one-line error if `pyserial` is not installed.
- Added `sensorStart` failure detail with the last CLI response.
- Replaced hard-coded `COM5` debug wording with the actual DATA port.
- Warns if DATA port returns CLI-like text, which usually means CLI/DATA ports
  are swapped.
- Warns if many DATA bytes arrive but no mmWave magic word is parsed.

### `static/app.js`

- Stores port scan suggestions from `/api/ports`.
- Shows a warning when `pyserial` is missing.
- Adds an "Apply suggested ports" button.
- Auto-fills empty CLI/DATA/ESP fields from suggestions.
- Uses `configs/vital_signs_AOP_6m.cfg` as the default cfg path for new nodes.

### `config/nodes.json`

- Removed macOS-specific saved ports.
- Default node now starts with empty ports and asks the user to scan/set ports.

## Setup On Another Computer

```bash
git clone https://github.com/jwlee1008/hafnium01.git
cd hafnium01
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python server.py
```

Open:

```text
http://127.0.0.1:8787
http://127.0.0.1:8787/vitals
```

## Windows Port Mapping Checklist

Use the dashboard port scan first.

Common Windows mapping:

```text
IWR6843 Enhanced COM Port -> Radar CLI
IWR6843 Standard COM Port -> Radar DATA
ESP32-S3 USB serial       -> ESP32
DATA baud                 -> 921600
ESP baud                  -> 115200
```

If `sensorStart did not return Done`:

1. Close Arduino Serial Monitor, PuTTY, Tera Term, or other serial tools.
2. Confirm Enhanced COM is CLI and Standard COM is DATA.
3. Press `RST.SW` on the IWR6843 board.
4. Wait 5 seconds.
5. Start collection again with `configs/vital_signs_AOP_6m.cfg`.

If collection runs but no VITAL rows appear:

1. Open `/vitals`.
2. Check latest CSV path and `recent_vital_ratio`.
3. Enable raw debug by running `radar_to_esp32.py --raw-debug` manually if
   needed.
4. If DATA bytes arrive but no magic word is parsed, verify DATA port, 921600
   baud, flashed firmware/demo type, and cfg compatibility.

## Manual Debug Commands

Example only; replace COM ports with the actual scanned ports:

```bash
python radar_to_esp32.py ^
  --cli-port COM3 ^
  --data-port COM5 ^
  --data-baud 921600 ^
  --cfg configs/vital_signs_AOP_6m.cfg ^
  --csv runtime/manual_debug.csv ^
  --label manual_debug ^
  --session-id manual_debug ^
  --esp-mode none ^
  --raw-debug
```

ESP32 ping/result checks are done through the dashboard, or manually:

```text
PING
RESULT?
PROFILE?
```

## Notes For Future AI Agents

- Do not assume ESP32 is reading radar raw frames in this repo.
- First debug serial port mapping and pyserial installation.
- Treat `sensorStart` failure as a board/cfg/CLI-state issue before changing
  the algorithm.
- For reliable detection, use the existing PC window metrics:
  - `valid_vital_rows`
  - `recent_vital_ratio`
  - `breath_deviation_p75`
  - `range_stability`
  - `frame_gap`
- If the user returns to LD2450 coordinate work, that is a different pipeline
  and should use zone-state/hysteresis logic rather than IWR6843 vital TLVs.

