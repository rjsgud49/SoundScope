"""Gunshot event pipeline: detect (audio thread) -> analyze (worker, non-blocking OCR)."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from app.audio.buffer import AudioRingBuffer
from app.audio.detection import SoundEvent, SoundEventDetector
from app.audio.direction import estimate_direction, fuse_absolute_bearing
from app.audio.distance import estimate_distance
from app.config.settings import Settings
from app.vision.compass import read_compass_heading
from app.vision.screen_capture import RoiBox, capture_roi_bgr


@dataclass
class _PendingEvent:
    event: SoundEvent
    window: np.ndarray
    sample_rate: int


def _circular_median(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0]) % 360.0
    # Map to complex mean for circular stats, then nearest sample
    rad = np.deg2rad(np.asarray(values, dtype=np.float64))
    mean_ang = float(np.rad2deg(np.arctan2(np.mean(np.sin(rad)), np.mean(np.cos(rad))))) % 360.0
    return mean_ang


class EventPipeline:
    """Detect on audio thread; fuse on worker. Compass OCR is cached / optional."""

    def __init__(
        self,
        settings: Settings,
        audio_buffer: AudioRingBuffer,
        sample_rate: int,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.audio_buffer = audio_buffer
        self.sample_rate = sample_rate
        self.on_event = on_event

        self.detector = self._make_detector(settings, sample_rate)
        self._lock = threading.Lock()
        self.latest_event: dict[str, Any] | None = None
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=30)
        self.enabled = True
        self.last_compass: dict[str, Any] | None = None
        self.cached_heading: float | None = (
            None
            if settings.manual_compass_heading is None
            else float(settings.manual_compass_heading) % 360.0
        )
        self._heading_history: deque[float] = deque(maxlen=5)
        if self.cached_heading is not None:
            self._heading_history.append(self.cached_heading)
        self.busy = False
        self.dropped_events = 0
        self.events_emitted = 0
        self.last_error: str | None = None
        self._ocr_lock = threading.Lock()
        self._ocr_inflight = False
        self._last_ocr_ts = 0.0

        self._q: queue.Queue[_PendingEvent | None] = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="event-worker",
            daemon=True,
        )
        self._worker.start()

    @staticmethod
    def _make_detector(settings: Settings, sample_rate: int) -> SoundEventDetector:
        return SoundEventDetector(
            sample_rate=sample_rate,
            rise_db=settings.detection_rise_db,
            min_peak=settings.detection_min_peak,
            cooldown_ms=settings.event_cooldown_ms,
            spectral_gate=bool(getattr(settings, "spectral_gate", True)),
            gunshot_min_score=float(getattr(settings, "gunshot_min_score", 0.28)),
        )

    def close(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=1.0)

    def reconfigure(self, settings: Settings, sample_rate: int | None = None) -> None:
        with self._lock:
            self.settings = settings
            if sample_rate is not None:
                self.sample_rate = sample_rate
            self.detector = self._make_detector(settings, self.sample_rate)
            if settings.manual_compass_heading is not None:
                self.cached_heading = float(settings.manual_compass_heading) % 360.0
                self._heading_history.clear()
                self._heading_history.append(self.cached_heading)

    def detection_debug(self) -> dict[str, Any]:
        d = self.detector
        return {
            "rms": d.last_rms,
            "peak": d.last_peak,
            "excess_db": d.last_excess_db,
            "peak_excess_db": d.last_peak_excess_db,
            "noise_rms": d._noise_rms,
            "rise_db": d.rise_db,
            "min_peak": d.min_peak,
            "gunshot_score": d.last_gunshot_score,
            "spectral_gate": d.spectral_gate,
            "gunshot_min_score": d.gunshot_min_score,
            "rejected_spectral": d.last_rejected_spectral,
            "busy": self.busy,
            "dropped_events": self.dropped_events,
            "events_emitted": self.events_emitted,
            "cached_heading": self.cached_heading,
            "queue_size": self._q.qsize(),
            "last_error": self.last_error,
        }

    def on_audio_block(self, frames: np.ndarray) -> None:
        if not self.enabled:
            return
        event = self.detector.process(frames)
        if event is None:
            return

        win_ms = max(50, int(self.settings.analysis_window_ms))
        n_frames = max(1, int(self.sample_rate * win_ms / 1000.0))
        window = self.audio_buffer.read_latest(n_frames)
        pending = _PendingEvent(
            event=event,
            window=window.copy() if window.size else window,
            sample_rate=self.sample_rate,
        )
        try:
            self._q.put_nowait(pending)
        except queue.Full:
            # Drop oldest by getting one, then put newest
            try:
                self._q.get_nowait()
                self.dropped_events += 1
                self._q.put_nowait(pending)
            except Exception:
                self.dropped_events += 1

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            self.busy = True
            try:
                payload = self._build_event_payload(item)
                with self._lock:
                    self.latest_event = payload
                    self.recent_events.appendleft(payload)
                    self.events_emitted += 1
                if self.on_event is not None:
                    try:
                        self.on_event(payload)
                    except Exception as exc:
                        self.last_error = f"on_event: {exc}"
                # Refresh compass in background occasionally (never blocks next events)
                self._maybe_refresh_compass()
            except Exception as exc:
                self.last_error = str(exc)
            finally:
                self.busy = False

    def _accept_heading(self, heading: float) -> float:
        """Smooth OCR headings; reject single-frame wild jumps."""
        h = float(heading) % 360.0
        if self._heading_history:
            last = self._heading_history[-1]
            delta = abs(((h - last + 180.0) % 360.0) - 180.0)
            if delta > 90.0 and len(self._heading_history) >= 2:
                # Keep last smoothed value until a second confirming reading
                return float(_circular_median(list(self._heading_history)) or last)
        self._heading_history.append(h)
        return float(_circular_median(list(self._heading_history)) or h)

    def _maybe_refresh_compass(self) -> None:
        if self.settings.radar_mode == "sound_only":
            return
        if self.settings.manual_compass_heading is not None:
            self.cached_heading = float(self.settings.manual_compass_heading) % 360.0
            return
        now = time.time()
        # At most ~2 OCR/sec
        if now - self._last_ocr_ts < 0.5:
            return
        with self._ocr_lock:
            if self._ocr_inflight:
                return
            self._ocr_inflight = True
            self._last_ocr_ts = now

        def _job() -> None:
            try:
                roi = RoiBox(
                    x=self.settings.compass_roi.x,
                    y=self.settings.compass_roi.y,
                    w=self.settings.compass_roi.w,
                    h=self.settings.compass_roi.h,
                )
                # Skip absurdly large ROIs (slow / freeze risk)
                if roi.w * roi.h > 120_000:
                    reading = {
                        "heading_deg": self.cached_heading,
                        "raw_text": "",
                        "confidence": 0.0,
                        "method": "ocr_skipped",
                        "error": "ROI too large for realtime OCR; use Auto ROI (narrow)",
                        "elapsed_ms": 0.0,
                    }
                    self.last_compass = reading
                    return
                bgr = capture_roi_bgr(self.settings.monitor_index, roi)
                result = read_compass_heading(bgr, tesseract_cmd=self.settings.tesseract_cmd)
                meta = result.to_dict()
                self.last_compass = meta
                if result.heading_deg is not None:
                    self.cached_heading = self._accept_heading(float(result.heading_deg))
                    meta["heading_deg"] = self.cached_heading
                    meta["method"] = "ocr_smoothed"
                    self.last_compass = meta
            except Exception as exc:
                self.last_compass = {
                    "heading_deg": self.cached_heading,
                    "raw_text": "",
                    "confidence": 0.0,
                    "method": "ocr",
                    "error": str(exc),
                    "elapsed_ms": 0.0,
                }
                self.last_error = f"ocr: {exc}"
            finally:
                with self._ocr_lock:
                    self._ocr_inflight = False

        threading.Thread(target=_job, name="compass-ocr-bg", daemon=True).start()

    def _build_event_payload(self, pending: _PendingEvent) -> dict[str, Any]:
        event = pending.event
        window = pending.window
        sr = int(pending.sample_rate or self.sample_rate)

        direction = estimate_direction(window, sample_rate=sr)
        distance = estimate_distance(
            window,
            noise_floor=event.noise_floor,
            confidence_threshold=self.settings.distance_confidence_threshold,
            sample_rate=sr,
        )

        mode = (
            self.settings.radar_mode
            if self.settings.radar_mode in ("absolute", "sound_only")
            else "absolute"
        )

        if mode == "sound_only":
            # Player-relative: FRONT=0 (up), RIGHT=90, REAR=180, LEFT=270
            absolute = float(direction.relative_offset_deg) % 360.0
            compass_used = None
            compass_meta = {
                "heading_deg": None,
                "method": "sound_only",
                "confidence": 0.0,
                "raw_text": "",
                "error": None,
            }
            bearing_note = "sound_only: player-relative direction"
        elif self.settings.manual_compass_heading is not None:
            compass_heading = float(self.settings.manual_compass_heading) % 360.0
            compass_meta = {
                "heading_deg": compass_heading,
                "method": "manual",
                "confidence": 1.0,
                "raw_text": "",
                "error": None,
            }
            self.cached_heading = compass_heading
            absolute = fuse_absolute_bearing(compass_heading, direction.relative_offset_deg)
            bearing_note = "ok"
            compass_used = float(compass_heading)
        else:
            compass_heading = self.cached_heading
            compass_meta = self.last_compass or {
                "heading_deg": compass_heading,
                "method": "cache",
                "confidence": 0.4 if compass_heading is not None else 0.0,
                "raw_text": "",
                "error": None if compass_heading is not None else "waiting for first OCR",
            }
            if compass_heading is None:
                absolute = float(direction.relative_offset_deg) % 360.0
                bearing_note = "compass cache empty; relative-as-absolute (unreliable)"
                compass_used = None
            else:
                absolute = fuse_absolute_bearing(
                    float(compass_heading), direction.relative_offset_deg
                )
                bearing_note = "ok"
                compass_used = float(compass_heading)

        self.last_compass = compass_meta

        overall_conf = float(
            np.clip(
                event.confidence * 0.4
                + direction.confidence * 0.35
                + distance.confidence * 0.25,
                0.0,
                1.0,
            )
        )

        return {
            "type": "event",
            "ts": event.timestamp,
            "fade_ms": int(self.settings.event_fade_ms),
            "radar_mode": mode,
            "detection": event.to_dict(),
            "relative": direction.to_dict(),
            "distance": distance.to_dict(),
            "compass": compass_meta,
            "bearing_deg": round(absolute, 1),
            "compass_heading_deg": compass_used,
            "display_distance": distance.display_label,
            "display_direction": direction.label,
            "confidence": round(overall_conf, 3),
            "note": bearing_note,
            "dropped_events": self.dropped_events,
            "debug": self.detection_debug(),
        }

    def simulate(
        self,
        relative_label: str = "RIGHT",
        compass_heading: float = 180.0,
        distance_class: str = "DIST_100_200",
    ) -> dict[str, Any]:
        from app.audio.direction import DIRECTION_OFFSETS_DEG
        from app.audio.distance import CLASS_APPROX_LABEL, CLASS_UI_LABEL

        offset = DIRECTION_OFFSETS_DEG.get(relative_label.upper(), 90.0)
        mode = (
            self.settings.radar_mode
            if self.settings.radar_mode in ("absolute", "sound_only")
            else "absolute"
        )
        if mode == "sound_only":
            absolute = float(offset) % 360.0
            compass_used = None
            compass_meta = {
                "heading_deg": None,
                "raw_text": "",
                "confidence": 0.0,
                "method": "simulate_sound_only",
                "error": None,
            }
            note = "simulated sound_only"
        else:
            absolute = fuse_absolute_bearing(compass_heading, offset)
            compass_used = float(compass_heading) % 360.0
            self.cached_heading = compass_used
            compass_meta = {
                "heading_deg": compass_used,
                "raw_text": str(int(compass_used)),
                "confidence": 1.0,
                "method": "simulate",
                "error": None,
            }
            note = "simulated"

        dist_conf = 0.7
        use_approx = dist_conf < self.settings.distance_confidence_threshold
        payload = {
            "type": "event",
            "ts": time.time(),
            "fade_ms": int(self.settings.event_fade_ms),
            "detection": {
                "timestamp": time.time(),
                "rms": 0.2,
                "peak": 0.5,
                "noise_floor": 0.01,
                "confidence": 0.9,
                "excess_db": 20.0,
                "peak_excess_db": 20.0,
                "gunshot_score": 0.9,
                "spectral_ok": True,
            },
            "relative": {
                "label": relative_label.upper(),
                "relative_offset_deg": offset,
                "confidence": 0.9,
                "channel_mode": "stereo",
                "lr_ratio_db": 8.0 if "RIGHT" in relative_label.upper() else -8.0,
                "note": "simulated",
                "itd_samples": 0,
                "itd_deg": 0.0,
            },
            "distance": {
                "distance_class": distance_class,
                "label": CLASS_UI_LABEL.get(distance_class, distance_class),
                "approx_label": CLASS_APPROX_LABEL.get(distance_class, "~?"),
                "confidence": dist_conf,
                "peak": 0.3,
                "rms": 0.12,
                "snr_db": 20.0,
                "use_approx": use_approx,
                "centroid_hz": 2500.0,
                "rolloff_hz": 6000.0,
                "brightness": 0.6,
            },
            "compass": compass_meta,
            "bearing_deg": round(absolute, 1),
            "compass_heading_deg": compass_used,
            "display_distance": (
                CLASS_APPROX_LABEL.get(distance_class)
                if use_approx
                else CLASS_UI_LABEL.get(distance_class, distance_class)
            ),
            "confidence": 0.9,
            "note": note,
            "radar_mode": mode,
            "display_direction": relative_label.upper(),
            "dropped_events": self.dropped_events,
        }
        with self._lock:
            self.latest_event = payload
            self.recent_events.appendleft(payload)
            self.events_emitted += 1
        if self.on_event is not None:
            self.on_event(payload)
        return payload
