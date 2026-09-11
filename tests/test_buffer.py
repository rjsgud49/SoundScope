"""Basic unit tests for ring buffer (no audio hardware required)."""

from __future__ import annotations

import numpy as np

from app.audio.buffer import AudioRingBuffer


def test_ring_buffer_wrap() -> None:
    buf = AudioRingBuffer(capacity_frames=8, channels=2)
    a = np.ones((5, 2), dtype=np.float32)
    b = np.full((5, 2), 2.0, dtype=np.float32)
    buf.write(a)
    buf.write(b)
    out = buf.read_latest(8)
    assert out.shape == (8, 2)
    # last 8 frames: 2 ones + 5 twos? Wait: wrote 5 ones then 5 twos into capacity 8
    # chronological latest 8: last 3 of ones + 5 twos
    assert np.allclose(out[:3], 1.0)
    assert np.allclose(out[3:], 2.0)


def test_downsample_empty() -> None:
    from app.audio.preprocessing import downsample_waveform

    assert len(downsample_waveform(np.zeros((0, 2)), 16)) == 16
