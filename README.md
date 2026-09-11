# SoundScope

Windows **WASAPI Loopback**으로 시스템 오디오만 분석하고, (선택) 배그 상단 나침반 OCR과 합성해 **방향·거리 레이더**를 localhost에 표시하는 accessibility / visualization 도구입니다.

> **중요:** 게임 프로세스 후킹, DLL injection, 메모리 읽기, 파일 수정을 하지 않습니다.  
> 사용하는 입력은 **시스템 오디오(WASAPI)** 와 **화면 픽셀(나침반 ROI)** 뿐입니다.

## 동작 모드

| 모드 | 설명 |
|------|------|
| **Sound only** (기본) | 나침반 OCR 없이 플레이어 기준 상대 방향만 표시 (위 = 내 앞) |
| **Absolute** | `absolute = (compass_heading + relative_offset) % 360` |

예: 시선 **180°** + 오디오 **RIGHT(+90°)** → 레이더 **270°**.

## 현재 기능

| 기능 | 상태 |
|------|------|
| WASAPI Loopback 캡처 / RMS / Peak / Waveform / WAV | ✅ |
| Adaptive 감지 + 스펙트럼 게이트 (음악·저음 오탐 억제) | ✅ |
| Stereo 방향 (ILD + ITD, 전방 반구 연속 각도) | ✅ |
| 7.1 surround 시 8방향 + 연속 각도 (뒤쪽 포함) | ✅ |
| 거리 100m class (peak/SNR + spectral brightness) | ✅ |
| 캡처 스톨 감지·자동 복구 (Bluetooth 끊김 등) | ✅ |
| 모니터/ROI 캘리브 + Tesseract OCR / Manual heading | ✅ |
| Sound-only / Absolute 레이더 UI + 감지 파라미터 튜닝 | ✅ |
| localhost 듀얼 모니터용 웹 UI (3페이지) | ✅ |

### 한계 (의도적)

- **2ch 스테레오**에서는 앞·뒤 구분이 불가능합니다. 뒤쪽(REAR)은 **7.1** 출력이 필요합니다.
- 거리·무기 분류 ML은 아직 휴리스틱 baseline입니다. (`PUBG-Gun-Sound-Dataset-main` CSV 참고용, 오디오 zip 미포함)

## 요구 사항

- Windows 10/11
- Python 3.11+ (권장 3.12)
- 재생 중인 오디오 출력 장치 (loopback = 실제 듣는 장치와 동일해야 함)
- Absolute 모드: [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)

### Tesseract (Windows)

1. UB Mannheim 빌드 설치 (예: `C:\Program Files\Tesseract-OCR`)
2. PATH에 추가하거나 `calibration/settings.json`의 `tesseract_cmd`에 `tesseract.exe` 경로 지정
3. 확인: `tesseract --version`

Tesseract가 없어도 **Sound only** 모드와 **Manual heading**으로 테스트할 수 있습니다.

## 설치

```bash
cd SoundScope
py -3.12 -m venv .venv
source .venv/Scripts/activate   # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 실행

```bash
python -m app.main
```

브라우저 (두 번째 모니터 권장): **http://127.0.0.1:8765**

| 페이지 | URL | 용도 |
|--------|-----|------|
| 레이더 | `/` | 방향·거리 마커, 모드 전환, 감지 튜닝, Simulate |
| 캡처 테스트 | `/capture` | 모니터·나침반 ROI·OCR |
| 사운드 테스트 | `/sound` | loopback 장치, 레벨, waveform |

종료: 터미널 `Ctrl+C` (최근 3초 버퍼 WAV 저장 시도)

## 빠른 사용 순서

1. **사운드 테스트** → ★ 기본 출력(듣는 장치) 선택 → **Apply device** → waveform 확인  
   - AirPods로 듣는데 Realtek을 고르면 **무음**입니다.
2. **레이더** → 기본 **Sound only**로 게임/유튜브 소리 재생 → 마커 확인  
   - 민감도: Detection tune (`rise_db`, `min_peak`, spectral gate) → **Apply detection**
3. Absolute가 필요하면 **캡처 테스트**에서 Auto ROI → Save → Test OCR 후 레이더에서 Absolute 전환  
   - OCR이 불안정하면 Manual facing 사용

## 테스트

```bash
python -m pytest tests/ -q
```

```bash
curl http://127.0.0.1:8765/api/devices
curl http://127.0.0.1:8765/api/status
curl -X POST http://127.0.0.1:8765/api/events/simulate ^
  -H "Content-Type: application/json" ^
  -d "{\"relative_label\":\"RIGHT\",\"compass_heading\":180}"
```

Sound only에서는 Simulate RIGHT → **90°**, Absolute + facing 180 → **270°**.

## 프로젝트 구조

```
SoundScope/
├── app/
│   ├── main.py                 # FastAPI + WebSocket + stall watchdog
│   ├── audio/
│   │   ├── capture.py          # WASAPI Loopback
│   │   ├── detection.py        # adaptive + spectral gate
│   │   ├── direction.py        # stereo ILD/ITD, surround, fuse
│   │   ├── distance.py         # 100m class + brightness
│   │   ├── pipeline.py         # detect → analyze → event
│   │   └── preprocessing.py    # waveform / spectral / ITD helpers
│   ├── vision/                 # mss ROI + Tesseract compass
│   ├── web/static/             # radar / capture / sound UI
│   └── config/settings.py
├── calibration/                # settings.json (로컬, gitignore)
├── recordings/                 # WAV 저장 (gitignore)
├── tests/
├── requirements.txt
└── README.md
```

## 주요 설정 (`calibration/settings.json`)

| 키 | 설명 |
|----|------|
| `radar_mode` | `sound_only` \| `absolute` |
| `preferred_device_id` | loopback 장치 이름 |
| `monitor_index` / `compass_roi` | OCR 모니터·ROI |
| `detection_rise_db` / `detection_min_peak` | 감지 민감도 |
| `spectral_gate` / `gunshot_min_score` | 스펙트럼 게이트 |
| `event_cooldown_ms` / `event_fade_ms` | 연발 억제 / 마커 fade |
| `manual_compass_heading` | OCR 대신 고정 시야각 |
| `tesseract_cmd` | tesseract.exe 경로 (optional) |

## 로드맵

1. ~~WASAPI Loopback + 웹 미터~~
2. ~~Sound event detection + spectral gate~~
3. ~~Stereo / 7.1 direction + absolute bearing~~
4. ~~Distance baseline + detection UI~~
5. Transparent always-on-top overlay (현재는 localhost)
6. Dataset loader + 거리/방향 캘리브 (오디오 zip 필요)
7. Mel / 경량 분류 모델
8. Packaging

## 문제 해결

| 증상 | 확인 |
|------|------|
| ON인데 buffer 0 / −80dB | 캡처 스톨 → **Start** 또는 자동 복구 대기 (BT AirPods 자주 끊김) |
| 무음 (레벨만 평평) | 듣는 장치와 같은 loopback인지 / **기본 출력 사용** |
| 마커가 안 뜸 | rise_db 낮추기, gate score 낮추기, Simulate로 UI 확인 |
| 음악에도 반응 | spectral gate ON, `gunshot_min_score` 올리기 |
| OCR 실패 | ROI를 숫자만 보이게 좁히기, Manual heading, Absolute만 해당 |
| 포트 사용 중 | 기존 `python -m app.main` 종료 |

## 라이선스 / 용도

Accessibility / visualization 목적. 게임 ToS를 확인하고, 경쟁 환경 사용은 각 게임 정책을 따릅니다.
