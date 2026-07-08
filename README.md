# HANIIUM Radar Local LLM Control

메인컴퓨터에서 센서 데이터를 수집하고, 로컬 LLM이 센서별 ESP32 profile을 생성한 뒤,
ESP32 엣지 노드가 경량 연산 결과만 메인컴퓨터로 보내는 흐름을 검증하는 MVP입니다.

## 실행

```bash
git clone https://github.com/jwlee1008/hafnium01.git
cd hafnium01
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

브라우저:

```text
http://127.0.0.1:8787
http://127.0.0.1:8787/vitals
```

## 현재 기능

- 기존 CSV 데이터 요약
- `qwen3:14b` 로컬 Ollama 모델 호출
- 센서별 ESP32 실행용 profile JSON 생성
- profile 스키마/범위 검증
- 센서/ESP32 노드 추가, 수정, 삭제
- Mac/Windows 시리얼 포트 스캔 후 노드별 포트 지정
- `esp_port`가 설정된 노드에 profile JSON을 시리얼 한 줄로 전송
- ESP32 `PING` 연결 테스트
- ESP32 `RESULT?` 결과 읽기
- 여러 ESP32 결과를 계속 읽는 자동 모니터링 시작/중지
- `radar_to_esp32.py` 실시간 수집 프로세스 시작/중지 준비
- `/vitals` 생체신호 전용 모니터
- `/vitals` 공간 heatmap 모니터
- IWR6843 point/target/vital 분리 CSV 생성 스크립트
- 5초 window 기반 2D grid ML feature 생성 스크립트
- ESP32 배포 및 결과 수신 시뮬레이션
- 투박하지만 가시성 높은 대시보드 UI

## 내일 보드 연결 순서

1. 센서/ESP32 보드를 USB로 연결합니다.
2. UI에서 `2. 포트 스캔`을 누릅니다.
3. 오른쪽 `노드 추가/수정`에서 노드를 선택하거나 새 노드를 만듭니다.
4. IWR6843 USB 방식이면 `Radar CLI 포트`, `Radar DATA 포트`, `ESP32 포트`를 모두 지정합니다.
5. ESP32가 센서 데이터를 직접 읽는 엣지 방식이면 `연결 모드`를 `ESP32 엣지 노드`로 바꾸고 `ESP32 포트`를 지정합니다.
6. `저장`을 누르면 `config/nodes.json`에 저장됩니다.
7. 노드 카드의 `연결테스트`를 눌러 `ACK,PONG` 응답을 확인합니다.
8. IWR6843 수집을 바로 확인하려면 `3. 수집 시작/중지`를 누릅니다.
9. `4. 로컬 LLM 캘리브레이션`을 실행하면 활성 노드 개수만큼 profile이 생성됩니다.
10. `5. ESP32 Profile 배포`로 `PROFILE_JSON`을 ESP32에 전송합니다.
11. 노드 카드의 `결과읽기`를 눌러 `RESULT_JSON` 결과가 들어오는지 확인합니다.
12. 여러 노드를 계속 감시하려면 `7. 자동 모니터 시작`을 누릅니다.

수집 버튼은 기본적으로 저장소 루트의 `radar_to_esp32.py`를 실행합니다.
다른 위치의 수집 스크립트를 쓰려면 `HANIUM_RADAR_SCRIPT`를 지정하세요.
현재 기본값:

```text
/path/to/hafnium01/radar_to_esp32.py
```

노드에 `cfg_path`가 비어 있으면 `--no-cfg`로 실행합니다.
레이더를 리셋했거나 처음 켜는 상황이면 UI에서 `CFG 경로`를 지정한 뒤 수집을 시작하는 편이 좋습니다.

수집 전 사전 검사는 다음을 막습니다.

```text
pyserial 미설치
CLI/DATA 포트 미설정
CLI와 DATA가 같은 포트인 상태
Windows에서 macOS/Linux /dev/... 포트를 쓰는 상태
없는 cfg 파일 경로
```

ESP32로 내려가는 profile 전송 형식:

```text
PROFILE_JSON {"type":"profile","sensor_id":"sensor_01","location_id":"unknown_position","profile_version":"local-agent-v1","profile":{...}}
```

현재 포함된 ESP32 펌웨어는 이 줄을 파싱하고 NVS에 profile을 저장합니다.

## ESP32 펌웨어

Arduino IDE에서 아래 파일을 엽니다.

```text
/Users/jwlee/Documents/Codex/2026-07-07/d/hanium_radar_ai_control/firmware/esp32_profile_node/esp32_profile_node.ino
```

보드 설정:

```text
Board: ESP32S3 Dev Module
Serial Monitor: 115200
```

Arduino CLI로 다시 빌드/업로드할 때:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3 firmware/esp32_profile_node
arduino-cli upload -p /dev/cu.usbserial-0001 --fqbn esp32:esp32:esp32s3 firmware/esp32_profile_node
```

지원 명령:

```text
PING
PROFILE_JSON {...}
RESULT?
PC_RESULT_JSON {...}
PROFILE?
RESET_PROFILE
```

응답:

```text
ACK,PONG,...
ACK,PROFILE,...
RESULT_JSON {...}
PROFILE_CURRENT {...}
```

노드 설정 파일:

```text
/Users/jwlee/Documents/Codex/2026-07-07/d/hanium_radar_ai_control/config/nodes.json
```

## PC와 ESP32 시리얼 프로토콜

대시보드는 ESP32 포트에 아래 한 줄 명령을 보냅니다.

```text
PING
PROFILE_JSON {...}
PC_RESULT_JSON {...}
RESULT?
PROFILE?
RESET_PROFILE
```

ESP32는 아래 형식으로 응답해야 합니다.

```text
ACK,PONG,...
ACK,PROFILE,...
ACK,PC_RESULT,...
RESULT_JSON {"sensor_id":"sensor_01","person_count":1,"survivor_candidate":true,"confidence":0.72,"status":"SURVIVOR_CANDIDATE","profile_version":"local-agent-v1"}
```

현재 PC-IWR6843 USB 구조에서는 PC가 최근 CSV/VITAL 윈도우로 `PC_RESULT_JSON`을 계산해 ESP32에 먼저 보내고,
ESP32는 최근 5초 안의 PC 판정이 있으면 그 값을 `RESULT_JSON`으로 반환합니다.

## ML/공간 Grid 파이프라인

기본 대시보드 수집은 기존 flat CSV를 유지합니다. 안정적인 생체신호 모니터링과 ESP32 연동을 깨지 않기 위해서입니다.

ML/공간 이미지화를 위한 고급 수집은 별도 스크립트를 사용합니다.

```bash
python3 scripts/radar_dataset_capture.py \
  --cli-port /dev/cu.usbserial-011D1A570 \
  --data-port /dev/cu.usbserial-011D1A571 \
  --data-baud 921600 \
  --cfg configs/vital_signs_AOP_6m.cfg \
  --csv runtime/dataset_sensor_01.csv \
  --session-id sensor_01_test_01 \
  --cfg-name vital_signs_AOP_6m.cfg \
  --esp-mode none \
  --raw-debug
```

이 명령은 아래 파일들을 만듭니다.

```text
runtime/dataset_sensor_01_frames.csv
runtime/dataset_sensor_01_points.csv
runtime/dataset_sensor_01_targets.csv
runtime/dataset_sensor_01_target_indexes.csv
runtime/dataset_sensor_01_vitals.csv
```

그 다음 5초 단위 grid window feature를 생성합니다.

```bash
python3 scripts/build_grid_dataset.py \
  --prefix runtime/dataset_sensor_01.csv \
  --output runtime/dataset_sensor_01_grid_windows.csv
```

`/vitals` 화면의 공간 히트맵은 `runtime/*_grid_windows.csv`가 있으면 최신 runtime 데이터를 우선 표시하고,
없으면 `reference_data/structured/dataset_aop6m_grid_windows.csv` 기준 샘플을 표시합니다.

PNG/GIF 시각화 파일을 만들 때:

```bash
python3 scripts/visualize_iwr_grid.py \
  --csv runtime/dataset_sensor_01_grid_windows.csv \
  --out-dir runtime/iwr6843_grid_visuals
```

포함된 기준 데이터:

```text
reference_data/structured/dataset_aop6m_frames.csv
reference_data/structured/dataset_aop6m_points.csv
reference_data/structured/dataset_aop6m_targets.csv
reference_data/structured/dataset_aop6m_vitals.csv
reference_data/structured/dataset_aop6m_grid_windows.csv
```

## 환경 변수

```bash
HANIUM_DATA_DIR=/path/to/hafnium01/reference_data
HANIUM_RADAR_SCRIPT=/path/to/hafnium01/radar_to_esp32.py
HANIUM_OLLAMA_MODEL=qwen3:14b
HANIUM_MONITOR_INTERVAL=2.0
PORT=8787
```

`HANIUM_DATA_DIR`를 지정하지 않으면 저장소의 `reference_data/`를 기준 CSV 폴더로 사용합니다.

## 현재 Mac 포트 매핑

2026-07-07 확인 기준:

```text
IWR6843 CLI  = /dev/cu.usbserial-011D1A570
IWR6843 DATA = /dev/cu.usbserial-011D1A571
ESP32-S3     = /dev/cu.usbserial-0001
CFG          = /Users/jwlee/Documents/Codex/2026-07-07/d/hanium_radar_ai_control/configs/vital_signs_AOP_6m.cfg
```

## 다음 개발 후보

- 새 grid window CSV에 실제 상황 라벨 붙이기
- RandomForest/LogisticRegression 기반 첫 ML 모델 학습
- LLM profile 후보 생성 후 Python 검증 루프
- ESP32 펌웨어의 시뮬레이션 신호를 실제 센서 입력/로직으로 교체
- Wi-Fi/MQTT 기반 ESP32 결과 전송 옵션
- 상황판 평면도/센서 위치 표시
