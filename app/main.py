"""SoundScope - WASAPI Loopback + absolute bearing radar (localhost)."""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.audio.capture import LoopbackCapture, list_loopback_devices
from app.audio.pipeline import EventPipeline
from app.audio.preprocessing import downsample_waveform, linear_to_db
from app.config.settings import (
    RECORDINGS_DIR,
    RoiSettings,
    Settings,
    ensure_dirs,
    load_settings,
    save_settings,
)
from app.vision.compass import read_compass_heading
from app.vision.screen_capture import (
    RoiBox,
    capture_roi_bgr,
    capture_top_strip,
    default_compass_roi,
    encode_png_base64,
    list_monitors,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

settings: Settings = load_settings()
capture: LoopbackCapture | None = None
pipeline: EventPipeline | None = None
_ws_clients: set[WebSocket] = set()
_meter_task: asyncio.Task | None = None
_main_loop: asyncio.AbstractEventLoop | None = None
_pending_events: asyncio.Queue[dict[str, Any]] | None = None


class DeviceSelectRequest(BaseModel):
    device_id: str


class SaveWavRequest(BaseModel):
    label: str | None = None


class CompassRoiRequest(BaseModel):
    monitor_index: int | None = None
    x: int
    y: int
    w: int
    h: int


class ManualCompassRequest(BaseModel):
    heading_deg: float | None = None


class DetectionSettingsRequest(BaseModel):
    detection_rise_db: float | None = None
    detection_min_peak: float | None = None
    event_cooldown_ms: float | None = None
    event_fade_ms: int | None = None
    tesseract_cmd: str | None = None
    distance_confidence_threshold: float | None = None
    radar_mode: str | None = None
    spectral_gate: bool | None = None
    gunshot_min_score: float | None = None


class RadarModeRequest(BaseModel):
    radar_mode: str  # absolute | sound_only


class SimulateEventRequest(BaseModel):
    relative_label: str = "RIGHT"
    compass_heading: float = 180.0
    distance_class: str = "DIST_100_200"


def get_capture() -> LoopbackCapture:
    if capture is None:
        raise RuntimeError("Capture engine is not initialized")
    return capture


def get_pipeline() -> EventPipeline:
    if pipeline is None:
        raise RuntimeError("Event pipeline is not initialized")
    return pipeline


def _queue_event(payload: dict[str, Any]) -> None:
    """Thread-safe: push event onto asyncio queue for WS broadcast."""
    loop = _main_loop
    q = _pending_events
    if loop is None or q is None:
        return
    try:
        loop.call_soon_threadsafe(q.put_nowait, payload)
    except Exception:
        pass


def init_capture(device_id: str | None = None) -> LoopbackCapture:
    global capture, pipeline
    if capture is not None:
        capture.stop()
    if pipeline is not None:
        try:
            pipeline.close()
        except Exception:
            pass

    eng = LoopbackCapture(
        sample_rate=settings.sample_rate,
        block_size=settings.block_size,
        buffer_seconds=settings.buffer_seconds,
        device_id=device_id or settings.preferred_device_id,
    )

    pipe = EventPipeline(
        settings=settings,
        audio_buffer=eng.buffer,
        sample_rate=settings.sample_rate,
        on_event=_queue_event,
    )

    def _on_block(frames: np.ndarray) -> None:
        pipe.audio_buffer = eng.buffer
        pipe.sample_rate = eng.sample_rate
        pipe.on_audio_block(frames)

    eng.on_block = _on_block
    eng.start()
    capture = eng
    pipeline = pipe
    return eng


def save_buffer_wav(label: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    eng = get_capture()
    audio, filled = eng.buffer.snapshot()
    if filled == 0:
        raise RuntimeError("Audio buffer is empty - wait for capture data")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = (label or "capture").replace(" ", "_")[:40]
    filename = f"soundscope_{safe_label}_{stamp}.wav"
    path = RECORDINGS_DIR / filename
    sf.write(str(path), audio, eng.sample_rate, subtype="PCM_16")
    duration = filled / float(eng.sample_rate)
    return {
        "ok": True,
        "path": str(path),
        "filename": filename,
        "frames": filled,
        "channels": eng.channels,
        "sample_rate": eng.sample_rate,
        "duration_sec": round(duration, 3),
    }


def build_meter_payload() -> dict[str, Any]:
    eng = get_capture()
    window = eng.buffer.read_latest(max(1, int(eng.sample_rate * 0.1)))
    waveform_src = eng.buffer.read_latest()
    waveform = downsample_waveform(waveform_src, settings.waveform_points)
    status = eng.get_status()
    pipe = pipeline
    return {
        "type": "meter",
        "ts": time.time(),
        "running": status["running"],
        "stalled": status.get("stalled", False),
        "error": status["error"],
        "device_name": status["device_name"],
        "channels": status["channels"],
        "sample_rate": status["sample_rate"],
        "blocks_captured": status["blocks_captured"],
        "buffer_filled_frames": status["buffer_filled_frames"],
        "buffer_seconds": status["buffer_seconds"],
        "rms": status["last_rms"],
        "peak": status["last_peak"],
        "rms_db": linear_to_db(status["last_rms"]),
        "peak_db": linear_to_db(status["last_peak"]),
        "rms_per_channel": status["last_rms_per_channel"],
        "peak_per_channel": status["last_peak_per_channel"],
        "waveform": waveform,
        "window_peak": float(np.max(np.abs(window))) if window.size else 0.0,
        "compass_heading_deg": (
            None
            if pipe is None or pipe.last_compass is None
            else pipe.last_compass.get("heading_deg")
        ),
        "manual_compass_heading": settings.manual_compass_heading,
        "radar_mode": settings.radar_mode,
        "detection": None if pipe is None else pipe.detection_debug(),
        "events_emitted": 0 if pipe is None else pipe.events_emitted,
    }


async def _broadcast_json(payload: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def _broadcast_meters() -> None:
    assert _pending_events is not None
    interval = 1.0 / max(1.0, settings.ui_push_hz)
    last_recover_ts = 0.0
    while True:
        # Drain pending gunshot events first
        while True:
            try:
                evt = _pending_events.get_nowait()
            except asyncio.QueueEmpty:
                break
            await _broadcast_json(evt)

        eng = capture
        if eng is not None and eng.is_stalled():
            now = time.time()
            # Bluetooth/WASAPI can hang after Apply/device sleep — auto recover
            if now - last_recover_ts > 3.0:
                last_recover_ts = now
                try:
                    init_capture(settings.preferred_device_id or eng.device_id)
                except Exception:
                    pass

        if _ws_clients and capture is not None:
            await _broadcast_json(build_meter_payload())
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _meter_task, _main_loop, _pending_events
    ensure_dirs()
    _main_loop = asyncio.get_running_loop()
    _pending_events = asyncio.Queue()
    init_capture()
    _meter_task = asyncio.create_task(_broadcast_meters())
    try:
        yield
    finally:
        if _meter_task is not None:
            _meter_task.cancel()
            try:
                await _meter_task
            except asyncio.CancelledError:
                pass
            _meter_task = None
        if capture is not None:
            capture.stop()
        if pipeline is not None:
            try:
                pipeline.close()
            except Exception:
                pass
        _main_loop = None
        _pending_events = None


app = FastAPI(title="SoundScope", version="0.2.0-radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/radar")
async def radar_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/capture")
async def capture_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "capture.html")


@app.get("/sound")
async def sound_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "sound.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "step": "radar", "name": "SoundScope"}


@app.get("/api/devices")
async def devices() -> dict[str, Any]:
    items = [d.to_dict() for d in list_loopback_devices()]
    return {"devices": items, "count": len(items)}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    eng = get_capture()
    pipe = get_pipeline()
    return {
        "settings": settings.model_dump(),
        "capture": eng.get_status(),
        "latest_event": pipe.latest_event,
        "last_compass": pipe.last_compass,
    }


@app.post("/api/devices/select")
async def select_device(body: DeviceSelectRequest) -> dict[str, Any]:
    global settings
    settings = settings.model_copy(update={"preferred_device_id": body.device_id})
    save_settings(settings)
    eng = get_capture()
    same = (
        eng.running
        and not eng.is_stalled()
        and eng.error is None
        and (
            eng.device_name == body.device_id
            or eng.device_id == body.device_id
        )
    )
    if same:
        return {"ok": True, "capture": eng.get_status(), "reused": True}
    eng = init_capture(body.device_id)
    await asyncio.sleep(0.2)
    return {"ok": True, "capture": eng.get_status(), "reused": False}


@app.post("/api/capture/start")
async def capture_start() -> dict[str, Any]:
    eng = get_capture()
    if eng.running and not eng.is_stalled() and eng.error is None:
        return {"ok": True, "capture": eng.get_status(), "reused": True}
    # Full re-init is more reliable than restarting a stopped soundcard thread
    eng = init_capture(settings.preferred_device_id or eng.device_id)
    await asyncio.sleep(0.25)
    return {"ok": True, "capture": eng.get_status(), "reused": False}


@app.post("/api/capture/stop")
async def capture_stop() -> dict[str, Any]:
    eng = get_capture()
    eng.stop()
    return {"ok": True, "capture": eng.get_status()}


@app.post("/api/save_wav")
async def save_wav(body: SaveWavRequest | None = None) -> JSONResponse:
    try:
        result = save_buffer_wav(None if body is None else body.label)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/monitors")
async def monitors() -> dict[str, Any]:
    items = [m.to_dict() for m in list_monitors()]
    return {"monitors": items, "count": len(items), "selected": settings.monitor_index}


@app.post("/api/compass/roi")
async def set_compass_roi(body: CompassRoiRequest) -> dict[str, Any]:
    global settings
    updates: dict[str, Any] = {
        "compass_roi": RoiSettings(x=body.x, y=body.y, w=body.w, h=body.h),
    }
    if body.monitor_index is not None:
        updates["monitor_index"] = body.monitor_index
    settings = settings.model_copy(update=updates)
    save_settings(settings)
    if pipeline is not None:
        pipeline.reconfigure(settings)
    return {"ok": True, "settings": settings.model_dump()}


@app.post("/api/compass/roi/default")
async def set_default_roi(monitor_index: int | None = None) -> dict[str, Any]:
    global settings
    idx = settings.monitor_index if monitor_index is None else monitor_index
    roi = default_compass_roi(idx)
    settings = settings.model_copy(
        update={
            "monitor_index": idx,
            "compass_roi": RoiSettings(x=roi.x, y=roi.y, w=roi.w, h=roi.h),
        }
    )
    save_settings(settings)
    if pipeline is not None:
        pipeline.reconfigure(settings)
    return {"ok": True, "roi": roi.to_dict(), "monitor_index": idx}


@app.post("/api/compass/test")
async def test_compass() -> dict[str, Any]:
    roi = RoiBox(
        x=settings.compass_roi.x,
        y=settings.compass_roi.y,
        w=settings.compass_roi.w,
        h=settings.compass_roi.h,
    )
    try:
        bgr = capture_roi_bgr(settings.monitor_index, roi)
        reading = read_compass_heading(bgr, tesseract_cmd=settings.tesseract_cmd)
        png = encode_png_base64(bgr)
        if pipeline is not None:
            pipeline.last_compass = reading.to_dict()
        return {
            "ok": reading.heading_deg is not None,
            "reading": reading.to_dict(),
            "preview_png_base64": png,
            "monitor_index": settings.monitor_index,
            "roi": roi.to_dict(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "reading": None}


@app.post("/api/compass/top_strip")
async def compass_top_strip(monitor_index: int | None = None, height: int = 90) -> dict[str, Any]:
    idx = settings.monitor_index if monitor_index is None else monitor_index
    try:
        bgr = capture_top_strip(idx, height=height)
        return {
            "ok": True,
            "monitor_index": idx,
            "preview_png_base64": encode_png_base64(bgr),
            "hint": "상단 전체 미리보기입니다. 나침반 숫자가 보이는 모니터/위치를 확인하세요.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/compass/manual")
async def set_manual_compass(body: ManualCompassRequest) -> dict[str, Any]:
    global settings
    heading = None if body.heading_deg is None else float(body.heading_deg) % 360.0
    settings = settings.model_copy(update={"manual_compass_heading": heading})
    save_settings(settings)
    if pipeline is not None:
        pipeline.reconfigure(settings)
    return {"ok": True, "manual_compass_heading": heading}


@app.post("/api/detection/settings")
async def update_detection_settings(body: DetectionSettingsRequest) -> dict[str, Any]:
    global settings
    raw = body.model_dump(exclude_none=True)
    if "radar_mode" in raw and raw["radar_mode"] not in ("absolute", "sound_only"):
        return {"ok": False, "error": "radar_mode must be absolute or sound_only"}
    settings = settings.model_copy(update=raw)
    save_settings(settings)
    if pipeline is not None:
        pipeline.reconfigure(settings)
    return {"ok": True, "settings": settings.model_dump()}


@app.post("/api/radar/mode")
async def set_radar_mode(body: RadarModeRequest) -> dict[str, Any]:
    global settings
    mode = body.radar_mode
    if mode not in ("absolute", "sound_only"):
        return {"ok": False, "error": "radar_mode must be absolute or sound_only"}
    settings = settings.model_copy(update={"radar_mode": mode})
    save_settings(settings)
    if pipeline is not None:
        pipeline.reconfigure(settings)
    return {"ok": True, "radar_mode": mode}


@app.post("/api/events/simulate")
async def simulate_event(body: SimulateEventRequest) -> dict[str, Any]:
    pipe = get_pipeline()
    payload = pipe.simulate(
        relative_label=body.relative_label,
        compass_heading=body.compass_heading,
        distance_class=body.distance_class,
    )
    return {"ok": True, "event": payload}


@app.websocket("/ws/meter")
async def ws_meter(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "step": "radar",
                "message": "SoundScope meter + event stream",
            }
        )
        while True:
            msg = await websocket.receive_json()
            if not isinstance(msg, dict):
                continue
            cmd = msg.get("cmd")
            if cmd == "save_wav":
                try:
                    result = save_buffer_wav(msg.get("label"))
                    await websocket.send_json({"type": "saved", **result})
                except Exception as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})
            elif cmd == "ping":
                await websocket.send_json({"type": "pong", "ts": time.time()})
            elif cmd == "simulate":
                pipe = get_pipeline()
                payload = pipe.simulate(
                    relative_label=str(msg.get("relative_label", "RIGHT")),
                    compass_heading=float(msg.get("compass_heading", 180.0)),
                    distance_class=str(msg.get("distance_class", "DIST_100_200")),
                )
                await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(websocket)


def main() -> None:
    ensure_dirs()
    host = settings.host
    port = settings.port
    url = f"http://{host}:{port}"
    print("=" * 60)
    print(" SoundScope - Absolute Bearing Radar")
    print("=" * 60)
    print(f" Open in browser (second monitor):  {url}")
    print(" Gunshot detect -> compass OCR + audio dir -> radar")
    print(" Ctrl+C saves the latest 3s buffer as WAV, then exits.")
    print("=" * 60)

    config = uvicorn.Config(app, host=host, port=port, log_level="info", reload=False)
    server = uvicorn.Server(config)

    def _win_handler(signum, frame):  # type: ignore[no-untyped-def]
        print("\n[SoundScope] Ctrl+C - saving 3s buffer to WAV...")
        try:
            if capture is not None:
                result = save_buffer_wav("ctrlc")
                print(f"[SoundScope] Saved: {result['path']}")
            else:
                print("[SoundScope] Capture not ready; nothing saved.")
        except Exception as exc:
            print(f"[SoundScope] Save failed: {exc}")
        server.should_exit = True

    signal.signal(signal.SIGINT, _win_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _win_handler)

    server.run()


if __name__ == "__main__":
    main()
