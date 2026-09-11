"""Lightweight audio feature helpers for metering, gating, and distance."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


def to_mono(frames: np.ndarray) -> np.ndarray:
    data = np.asarray(frames, dtype=np.float32)
    if data.ndim == 1:
        return data
    return np.mean(data, axis=1)


def downsample_waveform(frames: np.ndarray, points: int = 512) -> list[float]:
    """Downsample mono waveform to a fixed number of display points."""
    mono = to_mono(frames)
    if mono.size == 0:
        return [0.0] * points
    if mono.size <= points:
        out = np.zeros(points, dtype=np.float32)
        out[: mono.size] = mono
        return out.tolist()

    # Peak envelope style: max abs in each bin (better for visualizer)
    edges = np.linspace(0, mono.size, points + 1, dtype=np.int64)
    values = np.empty(points, dtype=np.float32)
    for i in range(points):
        segment = mono[edges[i] : edges[i + 1]]
        if segment.size == 0:
            values[i] = 0.0
        else:
            idx = int(np.argmax(np.abs(segment)))
            values[i] = float(segment[idx])
    return values.tolist()


def linear_to_db(value: float, floor_db: float = -80.0) -> float:
    if value <= 1e-10:
        return floor_db
    return max(floor_db, float(20.0 * np.log10(value)))


@dataclass
class SpectralFeatures:
    crest: float
    centroid_hz: float
    rolloff_hz: float
    hf_ratio: float
    band_ratio_2k8k: float
    flatness: float
    gunshot_score: float

    def to_dict(self) -> dict:
        return asdict(self)


def compute_spectral_features(
    frames: np.ndarray,
    sample_rate: int = 48000,
) -> SpectralFeatures:
    """Cheap FFT features for gunshot gating + distance heuristics."""
    mono = to_mono(frames).astype(np.float64)
    n = mono.size
    if n < 32:
        return SpectralFeatures(
            crest=0.0,
            centroid_hz=0.0,
            rolloff_hz=0.0,
            hf_ratio=0.0,
            band_ratio_2k8k=0.0,
            flatness=0.0,
            gunshot_score=0.0,
        )

    peak = float(np.max(np.abs(mono)) + 1e-12)
    rms = float(np.sqrt(np.mean(np.square(mono)) + 1e-20))
    crest = peak / rms

    # Hann window + real FFT (power)
    window = np.hanning(n)
    spec = np.fft.rfft(mono * window)
    mag = np.abs(spec) + 1e-20
    power = mag * mag
    freqs = np.fft.rfftfreq(n, d=1.0 / float(sample_rate))

    total_p = float(np.sum(power))
    centroid = float(np.sum(freqs * power) / total_p)

    cum = np.cumsum(power)
    rolloff_idx = int(np.searchsorted(cum, 0.85 * cum[-1]))
    rolloff = float(freqs[min(rolloff_idx, len(freqs) - 1)])

    # Band energies
    def _band(lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        return float(np.sum(power[mask])) if np.any(mask) else 0.0

    e_full = _band(80.0, min(12000.0, sample_rate * 0.45)) + 1e-20
    e_hf = _band(2000.0, min(10000.0, sample_rate * 0.45))
    e_2_8 = _band(2000.0, 8000.0)
    e_low = _band(80.0, 500.0)
    hf_ratio = e_hf / e_full
    band_ratio = e_2_8 / e_full

    # Spectral flatness (geometric / arithmetic mean of magnitude)
    log_mean = float(np.mean(np.log(mag)))
    arith = float(np.mean(mag))
    flatness = float(np.exp(log_mean) / (arith + 1e-20))

    # Gunshot-ish: high crest, HF content, not pure tone (moderate flatness),
    # and not bass-dominated music/thump.
    bass_dom = e_low / e_full
    crest_n = float(np.clip((crest - 3.0) / 8.0, 0.0, 1.0))
    hf_n = float(np.clip((hf_ratio - 0.12) / 0.35, 0.0, 1.0))
    band_n = float(np.clip((band_ratio - 0.08) / 0.3, 0.0, 1.0))
    flat_n = float(np.clip((flatness - 0.02) / 0.2, 0.0, 1.0))
    bass_pen = float(np.clip((bass_dom - 0.55) / 0.35, 0.0, 1.0))
    centroid_n = float(np.clip((centroid - 800.0) / 3500.0, 0.0, 1.0))

    score = (
        0.28 * crest_n
        + 0.22 * hf_n
        + 0.18 * band_n
        + 0.12 * flat_n
        + 0.12 * centroid_n
        - 0.25 * bass_pen
    )
    score = float(np.clip(score, 0.0, 1.0))

    return SpectralFeatures(
        crest=float(crest),
        centroid_hz=centroid,
        rolloff_hz=rolloff,
        hf_ratio=float(hf_ratio),
        band_ratio_2k8k=float(band_ratio),
        flatness=float(flatness),
        gunshot_score=score,
    )


def stereo_itd_samples(left: np.ndarray, right: np.ndarray, max_lag: int = 40) -> int:
    """Best lag (positive => right earlier / sound from right) via cross-corr."""
    L = np.asarray(left, dtype=np.float64)
    R = np.asarray(right, dtype=np.float64)
    n = min(L.size, R.size)
    if n < 16:
        return 0
    L = L[:n] - np.mean(L[:n])
    R = R[:n] - np.mean(R[:n])
    denom = float(np.sqrt(np.sum(L * L) * np.sum(R * R)) + 1e-20)
    if denom < 1e-12:
        return 0

    best_lag = 0
    best_val = -1e99
    scores: list[tuple[int, float]] = []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a = L[: n - lag] if lag else L
            b = R[lag:] if lag else R
        else:
            a = L[-lag:]
            b = R[: n + lag]
        if a.size < 8:
            continue
        # Compare L[t] with R[t+lag]: positive lag => R delayed => left earlier
        val = float(np.dot(a, b)) / denom
        scores.append((lag, val))
        if val > best_val:
            best_val = val
            best_lag = lag

    # Require a clear peak vs zero-lag; otherwise ILD-only
    zero_val = next((v for lag, v in scores if lag == 0), 0.0)
    if best_val < 0.15 or (best_val - zero_val) < 0.03:
        return 0
    # Flip so positive = right earlier (sound from right)
    return int(-best_lag)
