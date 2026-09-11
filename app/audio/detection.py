"""Adaptive-threshold sound event detection (gunshot candidates)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import numpy as np

from app.audio.preprocessing import compute_spectral_features


@dataclass
class SoundEvent:
    timestamp: float
    rms: float
    peak: float
    noise_floor: float
    confidence: float
    excess_db: float
    peak_excess_db: float = 0.0
    gunshot_score: float = 0.0
    spectral_ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class SoundEventDetector:
    """Detect sudden energy rises above an adaptive noise floor.

    Uses both RMS and peak envelopes so short gunshot transients still fire
    even when background music keeps RMS elevated. Optional spectral gate
    rejects bass-heavy / sustained music-like bursts.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        noise_alpha: float = 0.02,
        rise_db: float = 8.0,
        min_peak: float = 0.03,
        cooldown_ms: float = 350.0,
        warmup_blocks: int = 15,
        spectral_gate: bool = True,
        gunshot_min_score: float = 0.28,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.noise_alpha = float(noise_alpha)
        self.rise_db = float(rise_db)
        self.min_peak = float(min_peak)
        self.cooldown_ms = float(cooldown_ms)
        self.warmup_blocks = int(warmup_blocks)
        self.spectral_gate = bool(spectral_gate)
        self.gunshot_min_score = float(gunshot_min_score)

        self._noise_rms = 1e-4
        self._noise_peak = 1e-4
        self._blocks = 0
        self._last_event_ts = 0.0
        # Fast-attack / slow-release peak tracker for transient edges
        self._peak_env = 1e-4
        self.last_excess_db = 0.0
        self.last_peak_excess_db = 0.0
        self.last_rms = 0.0
        self.last_peak = 0.0
        self.last_gunshot_score = 0.0
        self.last_rejected_spectral = 0

    def reset(self) -> None:
        self._noise_rms = 1e-4
        self._noise_peak = 1e-4
        self._peak_env = 1e-4
        self._blocks = 0
        self._last_event_ts = 0.0
        self.last_gunshot_score = 0.0
        self.last_rejected_spectral = 0

    def process(self, frames: np.ndarray) -> SoundEvent | None:
        data = np.asarray(frames, dtype=np.float32)
        if data.size == 0:
            return None
        mono = data if data.ndim == 1 else np.mean(data, axis=1)

        rms = float(np.sqrt(np.mean(np.square(mono)) + 1e-20))
        peak = float(np.max(np.abs(mono)))
        self.last_rms = rms
        self.last_peak = peak
        self._blocks += 1

        # Peak envelope: attack fast, release slower
        if peak > self._peak_env:
            self._peak_env = 0.55 * self._peak_env + 0.45 * peak
        else:
            self._peak_env = 0.92 * self._peak_env + 0.08 * peak

        if self._blocks <= self.warmup_blocks:
            a = self.noise_alpha
            self._noise_rms = (1.0 - a) * self._noise_rms + a * rms
            self._noise_peak = (1.0 - a) * self._noise_peak + a * peak
            return None

        noise_rms = max(self._noise_rms, 1e-6)
        noise_peak = max(self._noise_peak, 1e-6)
        excess_db = float(20.0 * np.log10((rms + 1e-9) / noise_rms))
        peak_excess_db = float(20.0 * np.log10((peak + 1e-9) / noise_peak))
        # Edge: current peak vs recent envelope (catches attacks)
        edge_db = float(20.0 * np.log10((peak + 1e-9) / max(self._peak_env, 1e-6)))

        self.last_excess_db = excess_db
        self.last_peak_excess_db = peak_excess_db

        now = time.time()
        in_cooldown = (now - self._last_event_ts) * 1000.0 < self.cooldown_ms

        # Trigger if RMS OR peak jumps enough (gunshot = short peak spike)
        triggered = (
            peak >= self.min_peak
            and not in_cooldown
            and (
                excess_db >= self.rise_db
                or peak_excess_db >= self.rise_db - 1.0
                or (edge_db >= 4.0 and peak_excess_db >= self.rise_db - 3.0)
            )
        )

        # Adapt noise only on non-transient frames
        quiet = excess_db < self.rise_db * 0.45 and peak_excess_db < self.rise_db * 0.45
        a = self.noise_alpha
        if quiet:
            self._noise_rms = (1.0 - a) * self._noise_rms + a * rms
            self._noise_peak = (1.0 - a) * self._noise_peak + a * peak
        else:
            # very slow creep so floor doesn't latch onto gunshots
            self._noise_rms = (1.0 - a * 0.08) * self._noise_rms + (a * 0.08) * min(
                rms, noise_rms * 3.0
            )
            self._noise_peak = (1.0 - a * 0.08) * self._noise_peak + (a * 0.08) * min(
                peak, noise_peak * 3.0
            )

        if not triggered:
            return None

        gunshot_score = 1.0
        spectral_ok = True
        if self.spectral_gate:
            feats = compute_spectral_features(mono, self.sample_rate)
            gunshot_score = feats.gunshot_score
            self.last_gunshot_score = gunshot_score
            # Soft gate: very loud peaks still pass with slightly lower score
            threshold = self.gunshot_min_score
            if peak >= self.min_peak * 3.5:
                threshold = max(0.15, threshold - 0.08)
            spectral_ok = gunshot_score >= threshold
            if not spectral_ok:
                self.last_rejected_spectral += 1
                # Don't consume cooldown for rejected music/bass — allow real shot soon
                return None

        self._last_event_ts = now
        score = max(excess_db, peak_excess_db)
        confidence = float(np.clip((score - self.rise_db) / 16.0 + 0.55, 0.0, 1.0))
        confidence = float(np.clip(confidence * (0.7 + 0.3 * gunshot_score), 0.0, 1.0))
        return SoundEvent(
            timestamp=now,
            rms=rms,
            peak=peak,
            noise_floor=noise_rms,
            confidence=confidence,
            excess_db=excess_db,
            peak_excess_db=peak_excess_db,
            gunshot_score=gunshot_score,
            spectral_ok=spectral_ok,
        )
