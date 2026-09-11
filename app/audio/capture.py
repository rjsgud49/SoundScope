"""WASAPI Loopback device discovery and realtime capture (Windows)."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

import numpy as np
import soundcard as sc

from app.audio.buffer import AudioRingBuffer


@dataclass
class AudioDeviceInfo:
    id: str
    name: str
    channels: int
    is_loopback: bool
    is_default: bool

    def to_dict(self) -> dict:
        return asdict(self)


def list_loopback_devices() -> list[AudioDeviceInfo]:
    """List WASAPI loopback-capable microphones (system output mirrors)."""
    default_speaker = sc.default_speaker()
    default_name = getattr(default_speaker, "name", None)

    devices: list[AudioDeviceInfo] = []
    seen: set[str] = set()

    mics = sc.all_microphones(include_loopback=True)
    for mic in mics:
        name = str(mic.name)
        # soundcard marks loopbacks; also treat speaker-named mics as loopback candidates
        is_loopback = bool(getattr(mic, "isloopback", False))
        # Include all when include_loopback=True — prefer those flagged or matching speaker
        if not is_loopback and default_name and name != default_name:
            # Keep non-loopback physical mics out of the primary list for STEP 1
            continue

        device_id = name
        if device_id in seen:
            continue
        seen.add(device_id)

        channels = int(getattr(mic, "channels", 2) or 2)
        is_default = bool(default_name and name == default_name) or (
            is_loopback and default_name is not None and default_name in name
        )
        devices.append(
            AudioDeviceInfo(
                id=device_id,
                name=name,
                channels=channels,
                is_loopback=True,
                is_default=is_default,
            )
        )

    # Fallback: if filtering removed everything, expose all include_loopback mics
    if not devices:
        for mic in mics:
            name = str(mic.name)
            if name in seen:
                continue
            seen.add(name)
            devices.append(
                AudioDeviceInfo(
                    id=name,
                    name=name,
                    channels=int(getattr(mic, "channels", 2) or 2),
                    is_loopback=bool(getattr(mic, "isloopback", False)),
                    is_default=bool(default_name and name == default_name),
                )
            )

    # Ensure exactly one default flag when possible
    if devices and not any(d.is_default for d in devices):
        devices[0].is_default = True

    return devices


def get_default_loopback_device() -> Optional[AudioDeviceInfo]:
    devices = list_loopback_devices()
    for d in devices:
        if d.is_default:
            return d
    return devices[0] if devices else None


def _resolve_microphone(device_id: str | None):
    if device_id:
        try:
            return sc.get_microphone(id=device_id, include_loopback=True)
        except Exception:
            pass

    default = get_default_loopback_device()
    if default is None:
        raise RuntimeError(
            "No WASAPI loopback device found. "
            "Ensure an audio output device is available on Windows."
        )
    return sc.get_microphone(id=default.id, include_loopback=True)


class LoopbackCapture:
    """Background WASAPI loopback capture into a ring buffer."""

    def __init__(
        self,
        sample_rate: int = 48000,
        block_size: int = 1024,
        buffer_seconds: float = 3.0,
        device_id: str | None = None,
        on_block: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.buffer_seconds = float(buffer_seconds)
        self.device_id = device_id
        self.on_block = on_block

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._generation = 0

        self.channels = 2
        capacity = max(1, int(self.sample_rate * self.buffer_seconds))
        self.buffer = AudioRingBuffer(capacity, self.channels)

        self.running = False
        self.error: str | None = None
        self.device_name: str | None = None
        self.blocks_captured = 0
        self.last_block_ts = 0.0
        self.started_ts = 0.0
        self.last_rms = 0.0
        self.last_peak = 0.0
        self.last_rms_per_channel: list[float] = []
        self.last_peak_per_channel: list[float] = []

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._generation += 1
            gen = self._generation
            self._stop.clear()
            self.error = None
            self.blocks_captured = 0
            self.last_block_ts = 0.0
            self.started_ts = time.time()
            self._thread = threading.Thread(
                target=self._run,
                name=f"wasapi-loopback-{gen}",
                daemon=True,
                args=(gen,),
            )
            self.running = True
            self._thread.start()

    def stop(self) -> None:
        # Invalidate current generation so a hung record() thread cannot
        # clear running=False on a newer capture instance.
        with self._lock:
            self._generation += 1
            self._stop.set()
            thread = self._thread
            self._thread = None
            self.running = False
        if thread and thread.is_alive():
            thread.join(timeout=1.5)

    def restart(self, device_id: str | None = None) -> None:
        self.stop()
        if device_id is not None:
            self.device_id = device_id
        self.start()

    def is_stalled(self, grace_s: float = 2.5, stall_s: float = 1.5) -> bool:
        """True when marked running but no audio blocks arrived recently."""
        if not self.running:
            return False
        now = time.time()
        if self.started_ts and (now - self.started_ts) < grace_s:
            return False
        if self.blocks_captured <= 0:
            return True
        if self.last_block_ts <= 0:
            return True
        return (now - self.last_block_ts) >= stall_s

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "stalled": self.is_stalled(),
            "error": self.error,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "sample_rate": self.sample_rate,
            "block_size": self.block_size,
            "channels": self.channels,
            "buffer_seconds": self.buffer_seconds,
            "buffer_filled_frames": self.buffer.filled_frames,
            "blocks_captured": self.blocks_captured,
            "last_rms": self.last_rms,
            "last_peak": self.last_peak,
            "last_rms_per_channel": self.last_rms_per_channel,
            "last_peak_per_channel": self.last_peak_per_channel,
        }

    def _run(self, generation: int) -> None:
        try:
            mic = _resolve_microphone(self.device_id)
            if generation != self._generation:
                return
            self.device_name = str(mic.name)
            self.device_id = self.device_name
            self.channels = max(1, int(getattr(mic, "channels", 2) or 2))
            capacity = max(1, int(self.sample_rate * self.buffer_seconds))
            self.buffer = AudioRingBuffer(capacity, self.channels)

            with mic.recorder(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.block_size,
            ) as recorder:
                while not self._stop.is_set() and generation == self._generation:
                    data = recorder.record(numframes=self.block_size)
                    if generation != self._generation:
                        break
                    frames = np.asarray(data, dtype=np.float32)
                    if frames.ndim == 1:
                        frames = frames[:, np.newaxis]

                    self.buffer.write(frames)
                    self.blocks_captured += 1
                    self.last_block_ts = time.time()
                    self._update_levels(frames)

                    if self.on_block is not None:
                        try:
                            self.on_block(frames)
                        except Exception:
                            pass
        except Exception as exc:
            if generation == self._generation:
                self.error = str(exc)
        finally:
            with self._lock:
                if generation == self._generation:
                    self.running = False

    def _update_levels(self, frames: np.ndarray) -> None:
        # frames: (n, ch)
        peak_ch = np.max(np.abs(frames), axis=0)
        rms_ch = np.sqrt(np.mean(np.square(frames), axis=0) + 1e-20)
        self.last_peak_per_channel = [float(x) for x in peak_ch]
        self.last_rms_per_channel = [float(x) for x in rms_ch]
        self.last_peak = float(np.max(peak_ch)) if len(peak_ch) else 0.0
        self.last_rms = float(np.mean(rms_ch)) if len(rms_ch) else 0.0
