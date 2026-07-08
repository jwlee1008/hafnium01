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

- LLM profile 후보 생성 후 Python 검증 루프
- ESP32 펌웨어의 시뮬레이션 신호를 실제 센서 입력/로직으로 교체
- Wi-Fi/MQTT 기반 ESP32 결과 전송 옵션
- 상황판 평면도/센서 위치 표시
