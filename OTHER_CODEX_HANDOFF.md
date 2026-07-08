# Handoff For Other Codex

## 목적

이 저장소는 `IWR6843AOPEVM + ESP32-S3 + Mac/PC 메인컴퓨터` 구조의 한이음 레이더 생체신호 시스템 MVP입니다.

현재 상황은 다음과 같습니다.

- 다른 컴퓨터 쪽에서는 센서가 더 잘 동작한다고 들었음.
- 이 컴퓨터에서는 UI/서버/ESP32 연동 시스템을 만들었지만, IWR6843 `sensorStart`가 가끔 실패하고 최근 실시간 수집이 불안정했음.
- 그래서 이 저장소를 다른 컴퓨터의 정상 센서 환경과 합쳐서 판단하려는 목적입니다.

## 핵심 결론

센서 자체는 수집 데이터가 없어도 TI vital signs demo가 `VITAL` TLV를 바로 출력할 수 있습니다.

다만 이 시스템에서 말하는 "수집"은 센서를 켜기 위한 조건이 아니라, 다음 용도입니다.

- 현재 설치 위치/거리/자세에서 어느 정도 VITAL 반복성이 나오는지 확인
- `SURVIVOR_CANDIDATE` 판정 기준 보정
- 로컬 LLM/룰 기반 profile 생성 근거 확보

## 현재 아키텍처

```text
IWR6843AOPEVM
  CLI USB  -> PC Python cfg 전송
  DATA USB -> PC Python TLV 파싱

PC server.py
  /        대시보드
  /vitals  생체신호 전용 모니터
  /api/vitals 최근 CSV 기반 PC 실시간 판정

ESP32-S3
  PROFILE_JSON 저장
  PC_RESULT_JSON 수신
  RESULT? 요청 시 최근 PC 판정 또는 시뮬레이션 결과 반환
```

중요: 현재 구조에서 ESP32는 IWR6843 raw DATA를 직접 받지 않습니다. PC가 IWR6843 DATA 포트를 읽고 판정한 결과를 ESP32에 `PC_RESULT_JSON`으로 보내는 구조입니다.

## 주요 파일

```text
server.py
  웹 서버, 노드 관리, 수집 프로세스 시작/중지, PC 실시간 판정, ESP32 통신

radar_to_esp32.py
  IWR6843 cfg 전송, DATA 포트 수신, TLV 1040 VITAL 파싱, CSV 저장

static/index.html
static/app.js
static/styles.css
  메인 대시보드

static/vitals.html
static/vitals.js
  생체신호 전용 모니터

firmware/esp32_profile_node/esp32_profile_node.ino
  ESP32-S3 펌웨어. PROFILE_JSON, PC_RESULT_JSON, RESULT? 지원

configs/vital_signs_AOP_6m.cfg
  현재 사용하는 IWR6843 6m vital signs cfg

reference_data/
  예전 정상/복합 상황 CSV. 기준값 비교용
```

## 실행 방법

```bash
cd /path/to/hafnium01
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

기본값:

- `HANIUM_DATA_DIR` 기본값은 저장소 안의 `reference_data/`
- `HANIUM_RADAR_SCRIPT` 기본값은 저장소 루트의 `radar_to_esp32.py`
- `HANIUM_OLLAMA_MODEL` 기본값은 `qwen3:14b`

다른 컴퓨터에서 실시간 수집할 때는 대시보드에서 포트를 반드시 새로 맞추세요.

## 현재 이 컴퓨터에서 쓴 포트

다른 컴퓨터에서는 달라질 수 있습니다.

```text
IWR6843 CLI  = /dev/cu.usbserial-011D1A570
IWR6843 DATA = /dev/cu.usbserial-011D1A571
ESP32-S3     = /dev/cu.usbserial-0001
DATA baud    = 921600
ESP baud     = 115200
```

Windows에서 handoff 문서 기준 포트는 보통 다음이었습니다.

```text
COM3 = IWR6843 Enhanced COM Port, CLI
COM5 = IWR6843 Standard COM Port, DATA
COM7 = ESP32
```

## 최근 문제 원인

최근 이 컴퓨터에서 "잘 안 나옴"처럼 보였던 직접 원인은 다음이었습니다.

```text
sensorStart did not return Done
```

이 경우 마지막 CSV가 21행, VITAL 1행뿐이었고, 그 1행도 다음처럼 생체신호로 보기 어려웠습니다.

```text
breath_rate = 0.0
breath_deviation = 0.0016
```

즉 "수집을 오래 안 해서"라기보다는 IWR6843 demo 상태가 꼬여 시작이 제대로 되지 않은 상황입니다.

해결 순서:

1. IWR6843 보드의 `RST.SW` 누르기
2. 5초 기다리기
3. 대시보드에서 `수집 시작`
4. `/vitals`에서 30초 이상 확인

## 예전 정상 데이터 기준

`reference_data/` 기준 대략적인 양성 데이터 특성:

```text
person_breathing_trimmed_from_text.csv
  rows = 189
  vital_rows = 17
  vital_ratio = 0.0899
  breath_dev median = 0.0816
  top range_bin = 15

dataset_aop6m.csv
  rows = 5623
  vital_rows = 344
  vital_ratio = 0.0612
  breath_dev median = 0.0379

one_breathing_one_walking_from_text.csv
  vital_ratio = 0.0509
  breath_dev median = 0.0283
```

따라서 `vital_ratio_min=0.08`을 하드 기준으로 쓰면 정상 약신호/복합상황을 놓칠 수 있습니다.

현재 PC 실시간 판정은 예전 데이터에 맞춰 다음 기준을 씁니다.

```text
recent window = 최근 180 rows
vital_ratio_min = 0.045
breath_dev_min = 0.025
valid_vital_min = 3
range_stability_min = 0.30
fresh_frame_gap_max = 96 frames
```

## ESP32 프로토콜

지원 명령:

```text
PING
PROFILE_JSON {...}
PC_RESULT_JSON {...}
RESULT?
PROFILE?
RESET_PROFILE
```

현재 중요한 흐름:

```text
PC -> ESP32: PC_RESULT_JSON {"status":"SURVIVOR_CANDIDATE",...}
PC -> ESP32: RESULT?
ESP32 -> PC: RESULT_JSON {... "source":"pc_vital_window", "simulated":false ...}
```

ESP32가 PC 판정을 받은 지 5초가 넘으면 자체 시뮬레이션 결과를 반환할 수 있습니다. 그 경우:

```json
{"source":"esp32_profile_simulator","simulated":true}
```

이 값은 실제 IWR6843 판정이 아니므로 UI/판정에서는 보조 정보로만 봐야 합니다.

## 다른 컴퓨터에서 우선 확인할 것

1. IWR6843 포트 매핑이 맞는지 확인
   - Enhanced/CLI 포트와 Standard/DATA 포트가 바뀌면 안 됩니다.

2. 정상 센서 환경에서 `/vitals`의 PC 실시간 판정 확인
   - `valid_vital_rows`
   - `range_stability`
   - `breath_deviation_p75`
   - `frame_gap`

3. 수집이 잘 되는 컴퓨터에서 `configs/vital_signs_AOP_6m.cfg`가 같은지 비교

4. 정상 환경에서 1~3분 정도 새 CSV 수집 후, 이쪽 기준과 비교

5. 센서가 이미 `sensorStart` 된 상태라면 `radar_to_esp32.py --no-cfg` 방식도 테스트
   - 단, 보드 리셋 직후에는 반드시 cfg를 다시 보내야 합니다.

## 수집량 가이드

센서 동작 확인만 할 때:

```text
30초~1분
```

한 위치에서 판정 기준을 잡을 때:

```text
2~3분
```

최소 비교 데이터:

```text
no_person
person_breathing
person_moving
one_breathing_one_walking
```

상황별로 3~5분씩 있으면 LLM/ML 보정에 훨씬 유리합니다.

## 현재 상태 메모

이 저장소에 runtime CSV/log는 올리지 않습니다. 실시간 실행하면 `runtime/` 아래에 새로 생성됩니다.

마지막으로 이쪽에서 확인한 정상 동기화 상태:

```text
PC 판정: SURVIVOR_CANDIDATE
PC confidence: 0.823
ESP32 반환: SURVIVOR_CANDIDATE
ESP32 source: pc_vital_window
simulated: false
valid_vital_rows: 11
range_stability: 0.7273
```

단, 이후 IWR6843 `sensorStart` 실패가 다시 발생하면 이 값은 이전 정상 CSV 기준일 수 있으니, 반드시 `csv_mtime`과 `collection_running`을 확인하세요.
