"""Ring buffer for multi-channel float32 audio."""

from __future__ import annotations

import threading
from typing import Tuple

import numpy as np


class AudioRingBuffer:
    """Fixed-capacity circular buffer storing interleaved float32 frames.

    Shape convention for stored data: (frames, channels)
    """

    def __init__(self, capacity_frames: int, channels: int) -> None:
        if capacity_frames <= 0:
            raise ValueError("capacity_frames must be > 0")
        if channels <= 0:
            raise ValueError("channels must be > 0")

        self.capacity = int(capacity_frames)
        self.channels = int(channels)
        self._buf = np.zeros((self.capacity, self.channels), dtype=np.float32)
        self._write = 0
        self._filled = 0
        self._lock = threading.Lock()

    def resize_channels(self, channels: int) -> None:
        with self._lock:
            if channels == self.channels:
                return
            self.channels = int(channels)
            self._buf = np.zeros((self.capacity, self.channels), dtype=np.float32)
            self._write = 0
            self._filled = 0

    def write(self, frames: np.ndarray) -> None:
        """Append frames. Accepts (n,) mono or (n, ch) multi-channel."""
        data = np.asarray(frames, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, np.newaxis]
        if data.ndim != 2:
            raise ValueError("frames must be 1D or 2D")

        n, ch = data.shape
        if ch != self.channels:
            # Soft adapt: take min channels or pad
            if ch > self.channels:
                data = data[:, : self.channels]
            else:
                pad = np.zeros((n, self.channels - ch), dtype=np.float32)
                data = np.concatenate([data, pad], axis=1)

        with self._lock:
            if n >= self.capacity:
                self._buf[:] = data[-self.capacity :]
                self._write = 0
                self._filled = self.capacity
                return

            end = self._write + n
            if end <= self.capacity:
                self._buf[self._write : end] = data
            else:
                first = self.capacity - self._write
                self._buf[self._write :] = data[:first]
                self._buf[: n - first] = data[first:]
            self._write = (self._write + n) % self.capacity
            self._filled = min(self.capacity, self._filled + n)

    def read_latest(self, num_frames: int | None = None) -> np.ndarray:
        """Return chronological latest samples, shape (frames, channels)."""
        with self._lock:
            if self._filled == 0:
                return np.zeros((0, self.channels), dtype=np.float32)

            n = self._filled if num_frames is None else min(int(num_frames), self._filled)
            start = (self._write - n) % self.capacity
            if start + n <= self.capacity:
                return self._buf[start : start + n].copy()
            first = self.capacity - start
            return np.concatenate(
                [self._buf[start:], self._buf[: n - first]],
                axis=0,
            )

    def snapshot(self) -> Tuple[np.ndarray, int]:
        """Full chronological snapshot and filled frame count."""
        data = self.read_latest(None)
        return data, data.shape[0]

    @property
    def filled_frames(self) -> int:
        with self._lock:
            return self._filled
