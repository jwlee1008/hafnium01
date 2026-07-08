# 한이음 IWR6843 레이더 + 머신러닝 프로젝트 인수인계 문서

작성 목적: 형이 Git으로 전체 소스 파일을 받아서 현재 작업을 바로 이어갈 수 있도록, 지금까지의 하드웨어 설정, 코드 구조, 데이터 수집 방식, CSV 라벨링 기준, 문제 해결 기록, 다음 작업 방향을 한 문서에 정리한다.

작성 시점: 2026-07-07 KST 기준 작업 상태

---

## 1. 프로젝트 한 줄 요약

이 프로젝트는 IWR6843AOPEVM mmWave 레이더에서 들어오는 원시 바이너리 데이터를 PC Python에서 파싱하고, 상황별 정답 라벨과 함께 CSV 데이터셋으로 저장한 뒤, 머신러닝 모델을 학습시켜 생존자 또는 생체신호 존재 가능성을 판단하는 AI 활용 프로젝트다.

최종 목표 흐름은 다음과 같다.

```text
IWR6843 레이더
-> 원시 바이너리 데이터
-> PC Python 파싱
-> 정제 CSV 데이터셋
-> 머신러닝 학습
-> 실시간 추론
-> PC/ESP32 출력
```

중요한 현재 상태:

```text
현재 단계는 "판단/출력" 단계가 아니라 "데이터 수집/라벨링" 단계다.
따라서 지금 CSV에는 ALERT, survivor, confidence 같은 판단 결과를 넣지 않는 것이 원칙이다.
```

---

## 2. 하드웨어 구성

사용 장비:

```text
레이더: TI IWR6843AOPEVM
보조 MCU: ESP32-S3 계열 보드
PC: Windows + PowerShell + Python
```

현재 확인된 COM 포트:

```text
COM3: IWR6843 Enhanced COM Port
      레이더 CLI/설정 전송 포트

COM5: IWR6843 Standard COM Port
      레이더 데이터 수신 포트

COM7: ESP32 UART 포트
      ESP32와 PC 간 시리얼 통신 포트
```

포트 확인 명령:

```powershell
py -m serial.tools.list_ports -v
```

주의:

```text
ESP32가 COM6으로 잡힐 때도 있었지만, UART 모드에서 COM7로 안정화되었다.
레이더는 Enhanced COM Port가 CLI이고 Standard COM Port가 데이터 포트다.
```

---

## 3. 레이더 펌웨어와 cfg 상태

UniFlash로 업로드한 바이너리:

```text
vital_signs_tracking_6843AOP_demo.bin
```

현재 주로 사용하는 cfg:

```text
vital_signs_AOP_6m.cfg
```

기존에는 `vital_signs_AOP_2m.cfg`를 사용했지만, 이후 6m 측정용으로 바꿨다.

cfg 원본 경로:

```text
C:\한이음 프로젝트\configs\vital_signs_AOP_6m.cfg
```

현재 작업 폴더:

```text
C:\한이음 프로젝트
```

형이 Git으로 이어받는다면 cfg 파일도 프로젝트 안에 같이 넣는 것을 추천한다. 그래야 절대 경로 문제를 줄일 수 있다.

---

## 4. reset 버튼과 sensorStart 문제

현재 관찰된 문제:

```text
PowerShell에서 cfg를 보낼 때 sensorStart가 바로 Done으로 끝나지 않는 경우가 있다.
이 경우 RST.SW를 누르고 5초 정도 기다린 뒤 다시 실행하면 성공하는 경우가 많다.
```

원인 추정:

```text
이전 실행 상태가 완전히 정리되지 않았거나,
레이더 내부 데모 상태가 꼬였거나,
sensorStart가 이미 진행/중단된 상태일 가능성이 있다.
```

reset을 매번 누르지 않는 운영 방식:

```text
1. 처음에는 cfg 포함 명령으로 레이더를 시작한다.
2. RADAR/VITAL 데이터가 나오는지 확인한다.
3. 이후 레이더가 계속 동작 중이면 --no-cfg로 데이터만 읽는다.
```

단, 다음 상황에서는 `--no-cfg`를 쓰면 안 된다.

```text
레이더 리셋 버튼을 누른 직후
전원을 껐다 켠 직후
COM5에서 Waiting for COM5 data...만 반복되는 상태
sensorStart가 아직 성공하지 않은 상태
```

그때는 cfg를 다시 보내야 한다.

---

## 5. 현재 소스 파일 목록

현재 작업 폴더에 있는 주요 파일:

```text
C:\한이음 프로젝트\radar_to_esp32.py
C:\한이음 프로젝트\esp32_receiver.ino
C:\한이음 프로젝트\data\dataset_aop6m.csv
C:\한이음 프로젝트\data\person_breathing_trimmed_from_text.csv
C:\한이음 프로젝트\data\one_breathing_one_walking_from_text.csv
C:\한이음 프로젝트\data\two_people_one_lying_one_becomes_lying_from_text.csv
```

현재 파일 크기 기준으로 확인된 데이터 파일:

```text
dataset_aop6m.csv                                    약 672 KB
person_breathing_trimmed_from_text.csv                약 25 KB
one_breathing_one_walking_from_text.csv                약 71 KB
two_people_one_lying_one_becomes_lying_from_text.csv  약 113 KB
```

---

## 6. radar_to_esp32.py 현재 역할

`radar_to_esp32.py`는 현재 가장 중요한 Python 파일이다.

현재 역할:

```text
1. COM3으로 cfg 명령 전송
2. COM5에서 레이더 바이너리 데이터 수신
3. magic word 기준으로 프레임 파싱
4. TLV 1040 생체신호 데이터 파싱
5. RADAR/VITAL 로그 출력
6. CSV 저장
```

중요 상수:

```python
TLV_VITAL_SIGNS = 1040
VITAL_SIGNS_STRUCT = "<2H33f"
```

현재 CSV 컬럼:

```text
timestamp
session_id
label
cfg
frame
num_detected_obj
num_tlvs
packet_len
tlv_summary
has_vital
target_id
range_bin
breath_deviation
heart_rate
breath_rate
```

주의:

```text
코드 안에 AlertState가 남아 있지만, 현재 데이터 수집 단계에서는 사용하지 않는다.
기본 --esp-mode 값이 none이므로 ESP32 전송과 ALERT 판단은 꺼져 있다.
```

현재 기본 포트:

```text
--cli-port  COM3
--data-port COM5
--esp-port  COM7
```

현재 기본 baud:

```text
COM3 CLI: 115200
COM5 Data: 921600
ESP32: 115200
```

---

## 7. 현재 실행 명령

### 7.1 cfg까지 다시 보내면서 CSV 수집

레이더가 리셋됐거나 COM5 데이터가 안 나올 때 사용한다.

```powershell
cd "C:\한이음 프로젝트"

py radar_to_esp32.py --raw-debug --csv "data\dataset_aop6m.csv" --label person_breathing --session-id aop6m_person_breathing_01 --cfg-name vital_signs_AOP_6m.cfg --cfg "C:\한이음 프로젝트\configs\vital_signs_AOP_6m.cfg"
```

정상 흐름:

```text
Sending radar cfg...
CLI -> sensorStop
...
CLI -> sensorStart
Done
Cfg sent.
CSV logging enabled: dataset_aop6m.csv
Opening radar data port COM5 @ 921600
Reading radar data...
RADAR,...
VITAL,...
```

### 7.2 레이더가 이미 실행 중일 때 CSV만 수집

이미 `sensorStart`가 성공했고 COM5 데이터가 나오고 있을 때만 사용한다.

```powershell
cd "C:\한이음 프로젝트"

py radar_to_esp32.py --raw-debug --no-cfg --csv "dataset_aop6m.csv" --label person_breathing --session-id aop6m_person_breathing_01 --cfg-name vital_signs_AOP_6m.cfg
```

주의:

```text
--no-cfg는 이미 레이더가 돌아가고 있을 때만 쓴다.
Waiting for COM5 data...만 반복되면 cfg를 다시 보내야 한다.
```

### 7.3 Python 문법 검사

```powershell
py -m py_compile radar_to_esp32.py
```

---

## 8. ESP32 현재 상태

현재 `esp32_receiver.ino`는 `ALERT` 메시지를 받아 LED를 제어하는 형태로 수정되어 있다.

다만 현재 데이터셋 수집 단계에서는 ESP32를 쓰지 않아도 된다.

현재 원칙:

```text
CSV 수집 단계: ESP32 불필요
머신러닝 학습 단계: ESP32 불필요
실시간 추론/출력 단계: ESP32 사용
```

즉 지금은 다음이 핵심이다.

```text
레이더 -> PC -> CSV
```

나중에 모델이 완성되면:

```text
레이더 -> PC -> 모델 추론 -> ESP32 ALERT 출력
```

---

## 9. 로그 해석 기준

Python 출력은 크게 두 종류다.

### 9.1 RADAR 줄

예:

```text
RADAR,640,4,3,256,1020:52;1021:4;1040:136
```

형식:

```text
RADAR,frame,num_detected_obj,num_tlvs,packet_len,tlv_summary
```

의미:

```text
frame: 레이더 프레임 번호
num_detected_obj: 감지 객체/포인트 수
num_tlvs: TLV 블록 수
packet_len: 패킷 크기
tlv_summary: TLV 종류와 길이 요약
```

중요 TLV:

```text
1020: 감지 포인트 관련 데이터
1021: 보조/상태 데이터
1040: 생체신호 데이터
```

`1040`이 있어야 VITAL 줄이 나올 수 있다.

### 9.2 VITAL 줄

예:

```text
VITAL,640,id=0,rangeBin=15,breathDev=0.0363,heartRate=75.3,breathRate=16.7
```

의미:

```text
frame: 생체신호가 나온 프레임
id: 추적 대상 ID
rangeBin: 거리 구간 인덱스
breathDev: 호흡/미세 움직임 강도
heartRate: 추정 심박수
breathRate: 추정 호흡수
```

중요 해석:

```text
rangeBin이 일정하면 같은 위치/대상을 계속 잡는 것으로 볼 수 있다.
breathDev가 반복적으로 0.02 이상이면 호흡 움직임 가능성이 높다.
VITAL이 단발성 1회만 나오면 약한 감지 또는 잡음일 수 있다.
VITAL이 여러 번 반복되면 생체신호 데이터로 쓸 가능성이 높다.
```

---

## 10. 지금까지 만든 주요 CSV 데이터셋

### 10.1 dataset_aop6m.csv

설명:

```text
실시간 수집으로 누적된 원본 CSV.
여러 세션이 같은 파일에 누적되어 있을 수 있다.
```

주의:

```text
같은 session_id를 여러 번 재사용한 적이 있어, 시간 간격을 기준으로 실제 측정 구간을 다시 나눠야 할 수 있다.
```

### 10.2 person_breathing_trimmed_from_text.csv

생성 배경:

```text
2분간 한 명이 누워서 호흡한 로그에서 앞 약 20초 안정화 구간을 제거하고 새로 만든 CSV.
```

라벨:

```text
label = person_breathing
```

저장 결과:

```text
전체 저장 행: 189개
VITAL 포함 행: 17개
```

해석:

```text
한 명이 누워 있고 생체신호가 비교적 안정적으로 반복 감지된 데이터.
머신러닝에서 person_breathing 샘플로 사용 가능.
```

### 10.3 one_breathing_one_walking_from_text.csv

생성 배경:

```text
한 명은 누워서 호흡 중이고, 다른 한 명은 같은 경로를 왔다 갔다 한 상황.
```

라벨:

```text
main_label = one_breathing_one_walking
person_count = 2
lying_count = 1
moving_count = 1
breathing_count = 1
```

저장 결과:

```text
전체 행: 275개
VITAL 포함 행: 14개
```

해석:

```text
구조 상황에서 움직이는 사람과 누운 생존자 후보가 함께 있는 복합 상황 데이터.
person_breathing 단일 클래스에 섞으면 안 된다.
```

### 10.4 two_people_one_lying_one_becomes_lying_from_text.csv

생성 배경:

```text
한 명은 누워 있고, 다른 한 명은 근처에 머물다가 나중에 눕는 상황.
```

전체 라벨:

```text
main_label = two_people_one_lying_one_becomes_lying
```

구간 라벨:

```text
frame 3 ~ 706     : one_lying_one_nearby
frame 712 ~ 923   : transition_to_two_people_lying
frame 928 ~ 2127  : two_people_lying
```

저장 결과:

```text
전체 행: 327개
VITAL 포함 행: 27개

one_lying_one_nearby: 118행, VITAL 9행
transition_to_two_people_lying: 35행, VITAL 5행
two_people_lying: 174행, VITAL 13행
```

해석:

```text
후반 구간은 실제 실험 상황을 기준으로 two_people_lying으로 라벨링했다.
다만 레이더 VITAL id는 대부분 id=0 하나로 나오므로, 레이더만 보고 두 사람의 생체신호가 분리됐다고 말하면 안 된다.
학습 정답은 실험자가 알고 있는 실제 상황을 기준으로 붙이는 것이 맞다.
```

---

## 11. 현재 라벨링 원칙

이 프로젝트는 지도학습을 목표로 한다.

따라서 센서값만 저장하는 것이 아니라, 실제 실험 상황을 정답으로 함께 저장해야 한다.

추천 라벨 체계:

```text
no_person
person_breathing
person_breathing_unstable
one_breathing_one_walking
one_breathing_one_intermittent_walking
two_people_one_lying_one_becomes_lying
one_lying_one_nearby
transition_to_two_people_lying
two_people_lying
```

단일 label만 두는 것보다 여러 정답 컬럼을 두는 것이 좋다.

추천 정답 컬럼:

```text
main_label
phase_label
person_count
lying_count
moving_count
nearby_count
breathing_count
scenario_note
phase_note
```

이렇게 해두면 나중에 여러 문제로 바꿔 학습할 수 있다.

예:

```text
사람 존재 여부: person_count > 0
생체신호 존재 여부: breathing_count > 0
다중 인원 여부: person_count >= 2
움직임 포함 여부: moving_count > 0
누운 사람 존재 여부: lying_count > 0
```

---

## 12. 머신러닝 방향

현재는 아직 모델 학습 전 단계다.

바로 딥러닝으로 가기보다 다음 순서를 추천한다.

```text
1. CSV 데이터 충분히 수집
2. 세션/구간 단위 라벨 정리
3. 5~10초 단위 window feature 생성
4. RandomForest 같은 기본 분류 모델로 1차 학습
5. confusion matrix, recall, precision 확인
6. 실시간 추론 코드에 모델 연결
7. ESP32로 ALERT 전송
```

한 줄 단위 프레임을 그대로 학습시키는 것보다 window feature가 좋다.

추천 feature:

```text
window_duration_sec
frame_count
vital_count
vital_ratio
mean_num_detected_obj
max_num_detected_obj
std_num_detected_obj
mean_breath_deviation
max_breath_deviation
mean_heart_rate
std_heart_rate
mean_breath_rate
std_breath_rate
range_bin_mode
range_bin_unique_count
tlv1040_count
```

초기 모델 추천:

```text
Logistic Regression: 기준 모델
RandomForest: 첫 메인 모델 추천
SVM: 데이터가 적을 때 보조 실험
XGBoost/LightGBM: 데이터가 많아진 후 성능 개선용
```

초기 분류 목표 추천:

```text
1차: no_person vs person_present
2차: breathing_present vs no_breathing
3차: single_person vs multi_person
4차: lying_breathing_present vs only_moving
```

---

## 13. 중요한 해석 주의점

### 13.1 레이더가 사람 수를 완벽히 세는 것은 아니다

현재 로그에서 `id=0`만 나오는 경우가 많다.

따라서:

```text
VITAL id가 하나라고 사람이 한 명이라는 뜻은 아니다.
여러 사람이 있어도 레이더 데모가 생체신호 대상 하나만 추적할 수 있다.
```

### 13.2 num_detected_obj가 사람 수는 아니다

예:

```text
RADAR,749,149,...
```

이것은 사람이 149명이라는 뜻이 아니다.

의미:

```text
반사 포인트 또는 감지 포인트가 매우 많다는 뜻이다.
움직임, 자세 변화, 주변 반사, 여러 사람의 영향으로 커질 수 있다.
```

### 13.3 정답은 실험자가 알고 있는 실제 상황 기준

머신러닝은 지도학습이므로 실제 실험 상황을 정답으로 넣는다.

예:

```text
레이더만 보면 1명처럼 보여도 실제로 2명이 누워 있었다면 person_count=2로 라벨링한다.
단, 문서에는 "레이더가 두 명의 생체신호를 분리해서 본 것은 아님"이라고 기록한다.
```

---

## 14. 현재까지의 실험 해석 요약

### 한 명이 누워서 호흡

관찰:

```text
VITAL 반복 발생
rangeBin이 비교적 일정
breathDev가 중반 이후 안정적으로 증가
heartRate, breathRate가 현실적인 범위
```

판단:

```text
성공적인 person_breathing 데이터로 사용 가능
```

### 한 명 누움 + 한 명 왕복 이동

관찰:

```text
num_detected_obj가 큰 구간이 반복
VITAL은 중간중간 발생
rangeBin이 여러 구간으로 이동
```

판단:

```text
one_breathing_one_walking
```

### 한 명 누움 + 다른 한 명 근처 머뭄 후 누움

관찰:

```text
초반: 누워 있는 대상 + 근처 대상
중반: 두 번째 사람 자세 변화/움직임
후반: 두 명이 누운 상황으로 실험자가 확인
```

판단:

```text
main_label = two_people_one_lying_one_becomes_lying
phase_label = one_lying_one_nearby / transition_to_two_people_lying / two_people_lying
```

---

## 15. 다음 작업 제안

### 15.1 데이터 수집을 더 해야 하는 상황

최소한 아래 라벨별로 여러 세션을 모으는 것을 추천한다.

```text
no_person
person_breathing
person_moving
one_breathing_one_walking
two_people_lying
two_people_one_lying_one_becomes_lying
```

각 라벨별 권장:

```text
최소 3~5세션
세션당 1~3분
거리/각도/자세를 조금씩 바꿔서 수집
```

### 15.2 CSV 정리 스크립트 필요

다음으로 만들면 좋은 스크립트:

```text
build_dataset_windows.py
```

역할:

```text
원본 CSV 또는 텍스트 변환 CSV를 읽음
5초 또는 10초 window로 묶음
feature 평균/최대/표준편차 계산
label을 window 단위로 붙임
ml_dataset_windows.csv 생성
```

### 15.3 첫 모델 학습 스크립트 필요

다음으로 만들면 좋은 스크립트:

```text
train_model.py
```

역할:

```text
ml_dataset_windows.csv 읽기
train/test split
RandomForest 학습
confusion matrix 출력
모델 저장
```

저장 모델 예:

```text
models/radar_survivor_random_forest.joblib
```

---

## 16. Git으로 넘길 때 추천 구조

형에게 넘길 때 권장 폴더 구조:

```text
한이음 프로젝트/
  radar_to_esp32.py
  esp32_receiver.ino
  HANIIUM_RADAR_ML_HANDOFF.md
  data/
    dataset_aop6m.csv
    person_breathing_trimmed_from_text.csv
    one_breathing_one_walking_from_text.csv
    two_people_one_lying_one_becomes_lying_from_text.csv
  configs/
    vital_signs_AOP_6m.cfg
    vital_signs_AOP_2m.cfg
  scripts/
    future build_dataset_windows.py
    future train_model.py
  models/
    future trained model files
```

현재는 CSV들이 루트에 있으므로, 나중에 정리할 때 `data/` 폴더로 옮겨도 된다.

단, 옮긴 뒤에는 README나 실행 명령의 경로도 맞춰야 한다.

---

## 17. 형이 바로 확인해야 할 체크리스트

1. Python 환경 확인

```powershell
py --version
py -m serial.tools.list_ports -v
```

2. 포트 확인

```text
COM3 = IWR Enhanced
COM5 = IWR Standard
COM7 = ESP32 UART
```

3. Python 문법 확인

```powershell
py -m py_compile radar_to_esp32.py
```

4. 레이더 실행 확인

```powershell
py radar_to_esp32.py --raw-debug --csv "data\dataset_aop6m.csv" --label test_run --session-id test_run_01 --cfg-name vital_signs_AOP_6m.cfg --cfg "C:\한이음 프로젝트\configs\vital_signs_AOP_6m.cfg"
```

5. 정상 출력 확인

```text
RADAR,...
VITAL,...
```

6. 새 데이터 수집 시 label과 session_id를 반드시 바꿀 것

예:

```powershell
py radar_to_esp32.py --raw-debug --no-cfg --csv "dataset_aop6m.csv" --label no_person --session-id aop6m_no_person_02 --cfg-name vital_signs_AOP_6m.cfg
```

---

## 18. 핵심 결론

현재 프로젝트는 하드웨어 초기 구동과 레이더 데이터 수신 단계는 성공했다.

현재 확실히 된 것:

```text
IWR6843 COM 포트 인식 성공
UniFlash 바이너리 업로드 성공
AOP 6m cfg 적용 성공
COM5 데이터 수신 성공
TLV 1040 생체신호 파싱 성공
VITAL 로그 출력 성공
CSV 저장 성공
여러 실제 상황을 라벨링한 CSV 생성 성공
```

현재 해야 할 일:

```text
더 많은 라벨별 데이터 수집
세션/구간 라벨 정리
window feature 생성
RandomForest 등으로 첫 머신러닝 모델 학습
모델 성능 확인 후 실시간 추론으로 확장
마지막에 ESP32 출력 연결
```

가장 중요한 원칙:

```text
지금은 판단 결과를 CSV에 넣지 않는다.
CSV에는 레이더에서 나온 값과 실제 상황 정답(label)을 저장한다.
AI 판단은 학습 이후 단계에서 붙인다.
```
