"""Screen / monitor capture helpers (mss)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int
    name: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoiBox:
    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RoiBox | None":
        if not data:
            return None
        return cls(
            x=int(data["x"]),
            y=int(data["y"]),
            w=int(data["w"]),
            h=int(data["h"]),
        )


def list_monitors() -> list[MonitorInfo]:
    import mss

    with mss.mss() as sct:
        # sct.monitors[0] is the virtual combined desktop
        out: list[MonitorInfo] = []
        for i, mon in enumerate(sct.monitors):
            if i == 0:
                name = "All monitors (virtual)"
            else:
                name = f"Monitor {i}"
            out.append(
                MonitorInfo(
                    index=i,
                    left=int(mon["left"]),
                    top=int(mon["top"]),
                    width=int(mon["width"]),
                    height=int(mon["height"]),
                    name=name,
                )
            )
        return out


def default_compass_roi(monitor_index: int = 1) -> RoiBox:
    """Tight top-center ROI around PUBG heading digits only (fast OCR)."""
    monitors = list_monitors()
    if monitor_index < 0 or monitor_index >= len(monitors):
        monitor_index = primary_monitor_index()
    mon = monitors[monitor_index]
    # Narrow box: center number under the caret, not the whole compass strip
    w = max(140, int(mon.width * 0.09))
    h = max(40, int(mon.height * 0.04))
    x = int((mon.width - w) / 2)
    y = max(6, int(mon.height * 0.015))
    return RoiBox(x=x, y=y, w=w, h=h)


def primary_monitor_index() -> int:
    """Prefer the monitor whose origin is (0,0); else first physical monitor."""
    monitors = list_monitors()
    for m in monitors:
        if m.index == 0:
            continue
        if m.left == 0 and m.top == 0:
            return m.index
    return 1 if len(monitors) > 1 else 0


def capture_top_strip(monitor_index: int, height: int = 90) -> np.ndarray:
    """Capture a wide top strip to help the user locate the compass."""
    monitors = list_monitors()
    if monitor_index < 0 or monitor_index >= len(monitors):
        raise ValueError(f"Invalid monitor_index: {monitor_index}")
    mon = monitors[monitor_index]
    h = max(40, min(int(height), mon.height))
    roi = RoiBox(x=0, y=0, w=mon.width, h=h)
    return capture_roi_bgr(monitor_index, roi)


def capture_roi_bgra(monitor_index: int, roi: RoiBox) -> np.ndarray:
    """Capture ROI as BGRA uint8 array shaped (h, w, 4).

    ROI x/y are relative to the selected monitor's top-left.
    """
    import mss

    monitors = list_monitors()
    if monitor_index < 0 or monitor_index >= len(monitors):
        raise ValueError(f"Invalid monitor_index: {monitor_index}")
    mon = monitors[monitor_index]

    left = mon.left + int(roi.x)
    top = mon.top + int(roi.y)
    width = max(1, int(roi.w))
    height = max(1, int(roi.h))

    region = {"left": left, "top": top, "width": width, "height": height}
    with mss.mss() as sct:
        shot = sct.grab(region)
        # mss returns BGRA
        frame = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return frame.copy()


def capture_roi_bgr(monitor_index: int, roi: RoiBox) -> np.ndarray:
    bgra = capture_roi_bgra(monitor_index, roi)
    return bgra[:, :, :3].copy()


def encode_png_base64(bgr: np.ndarray) -> str:
    import base64

    import cv2

    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("Failed to encode PNG")
    return base64.b64encode(buf.tobytes()).decode("ascii")
