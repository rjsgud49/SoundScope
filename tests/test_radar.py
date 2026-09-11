"""Expanded unit tests for detection gate, direction ITD, distance, pipeline."""

from __future__ import annotations

import numpy as np

from app.audio.buffer import AudioRingBuffer
from app.audio.detection import SoundEventDetector
from app.audio.direction import estimate_direction, fuse_absolute_bearing
from app.audio.distance import estimate_distance
from app.audio.pipeline import EventPipeline, _circular_median
from app.audio.preprocessing import compute_spectral_features, stereo_itd_samples
from app.config.settings import Settings
from app.vision.compass import _parse_heading


def test_fuse_absolute_bearing_example() -> None:
    assert fuse_absolute_bearing(180.0, 90.0) == 270.0
    assert fuse_absolute_bearing(350.0, 90.0) == 80.0


def test_stereo_right_direction() -> None:
    frames = np.zeros((2048, 2), dtype=np.float32)
    frames[:, 1] = 0.4
    frames[:, 0] = 0.05
    est = estimate_direction(frames)
    assert est.label in ("RIGHT", "FRONT_RIGHT")
    assert est.relative_offset_deg > 40.0


def test_stereo_left_direction() -> None:
    frames = np.zeros((2048, 2), dtype=np.float32)
    frames[:, 0] = 0.4
    frames[:, 1] = 0.05
    est = estimate_direction(frames)
    assert est.label in ("LEFT", "FRONT_LEFT")
    assert est.relative_offset_deg < -40.0


def test_stereo_mild_right_is_diagonal() -> None:
    rng = np.random.default_rng(2)
    base = rng.standard_normal(2048).astype(np.float32)
    frames = np.zeros((2048, 2), dtype=np.float32)
    frames[:, 0] = base * 0.2
    frames[:, 1] = base * 0.28
    est = estimate_direction(frames)
    assert -10 < est.relative_offset_deg < 90
    assert est.channel_mode == "stereo"


def test_stereo_itd_positive_for_right_earlier() -> None:
    n = 1024
    t = np.arange(n, dtype=np.float32)
    pulse = np.exp(-0.5 * ((t - 400) / 8.0) ** 2).astype(np.float32)
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)
    left[20:] = pulse[:-20]
    right[:] = pulse
    lag = stereo_itd_samples(left, right, max_lag=40)
    assert lag > 0


def test_surround_rear_direction() -> None:
    frames = np.zeros((2048, 6), dtype=np.float32)
    frames[:, 4] = 0.5  # BL
    frames[:, 5] = 0.45  # BR
    est = estimate_direction(frames)
    assert est.channel_mode == "surround"
    assert abs(est.relative_offset_deg) > 90 or est.label in (
        "REAR",
        "REAR_LEFT",
        "REAR_RIGHT",
        "BACK",
    )


def test_distance_louder_is_closer() -> None:
    # Broadband-ish bursts so spectral brightness doesn't dominate
    rng = np.random.default_rng(0)
    loud = (rng.standard_normal(4096) * 0.45).astype(np.float32)
    quiet = (rng.standard_normal(4096) * 0.03).astype(np.float32)
    d_loud = estimate_distance(loud, noise_floor=0.01)
    d_quiet = estimate_distance(quiet, noise_floor=0.01)
    order = [
        "DIST_0_100",
        "DIST_100_200",
        "DIST_200_300",
        "DIST_300_400",
        "DIST_400_500",
        "DIST_500_600",
        "DIST_600_PLUS",
    ]
    assert order.index(d_loud.distance_class) <= order.index(d_quiet.distance_class)


def test_gunshot_score_higher_for_transient_hf() -> None:
    rng = np.random.default_rng(1)
    n = 2048
    t = np.arange(n) / 48000.0
    # Impulse-like broadband click
    click = np.zeros(n, dtype=np.float32)
    click[200:220] = rng.standard_normal(20).astype(np.float32) * 0.8
    # Sustained bass tone
    bass = (0.35 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
    s_click = compute_spectral_features(click, 48000)
    s_bass = compute_spectral_features(bass, 48000)
    assert s_click.gunshot_score > s_bass.gunshot_score


def test_detector_triggers_on_burst() -> None:
    det = SoundEventDetector(
        sample_rate=48000,
        rise_db=10.0,
        min_peak=0.05,
        cooldown_ms=100.0,
        warmup_blocks=5,
        spectral_gate=False,
    )
    noise = np.random.randn(1024).astype(np.float32) * 0.005
    for _ in range(8):
        assert det.process(noise) is None
    burst = np.random.randn(1024).astype(np.float32) * 0.3
    ev = det.process(burst)
    assert ev is not None
    assert ev.confidence > 0


def test_detector_spectral_gate_rejects_bass() -> None:
    det = SoundEventDetector(
        sample_rate=48000,
        rise_db=6.0,
        min_peak=0.05,
        cooldown_ms=50.0,
        warmup_blocks=5,
        spectral_gate=True,
        gunshot_min_score=0.35,
    )
    n = 1024
    t = np.arange(n) / 48000.0
    noise = (np.sin(2 * np.pi * 80 * t) * 0.01).astype(np.float32)
    for _ in range(8):
        det.process(noise)
    bass_burst = (np.sin(2 * np.pi * 90 * t) * 0.4).astype(np.float32)
    ev = det.process(bass_burst)
    # Pure low sine should usually fail spectral gate
    assert ev is None
    assert det.last_rejected_spectral >= 1


def test_parse_heading() -> None:
    assert _parse_heading("180") == 180.0
    assert _parse_heading("N 45") == 45.0
    assert _parse_heading("bearing NE") == 45.0


def test_circular_median() -> None:
    assert _circular_median([10.0, 20.0, 15.0]) is not None
    m = _circular_median([350.0, 10.0, 0.0])
    assert m is not None
    assert m < 30 or m > 330


def test_simulate_sound_only_uses_relative_bearing() -> None:
    settings = Settings(radar_mode="sound_only")
    buf = AudioRingBuffer(4800, 2)
    pipe = EventPipeline(settings=settings, audio_buffer=buf, sample_rate=48000)
    try:
        payload = pipe.simulate(relative_label="RIGHT", compass_heading=180.0)
        assert payload["radar_mode"] == "sound_only"
        assert payload["bearing_deg"] == 90.0
        assert payload["compass_heading_deg"] is None
    finally:
        pipe.close()


def test_simulate_absolute_fuses() -> None:
    settings = Settings(radar_mode="absolute")
    buf = AudioRingBuffer(4800, 2)
    pipe = EventPipeline(settings=settings, audio_buffer=buf, sample_rate=48000)
    try:
        payload = pipe.simulate(relative_label="RIGHT", compass_heading=180.0)
        assert payload["bearing_deg"] == 270.0
    finally:
        pipe.close()
