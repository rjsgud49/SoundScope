"""Relative direction estimation from multi-channel audio."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from app.audio.preprocessing import stereo_itd_samples


# Player-relative offsets used for absolute bearing fusion / labels
DIRECTION_OFFSETS_DEG: dict[str, float] = {
    "FRONT": 0.0,
    "CENTER": 0.0,
    "FRONT_RIGHT": 45.0,
    "RIGHT": 90.0,
    "REAR_RIGHT": 135.0,
    "REAR": 180.0,
    "BACK": 180.0,
    "REAR_LEFT": -135.0,
    "LEFT": -90.0,
    "FRONT_LEFT": -45.0,
}

# Unit circle angles for surround channels (player-relative, 0=front)
_SURROUND_ANGLES: dict[str, float] = {
    "FRONT": 0.0,
    "FRONT_RIGHT": 45.0,
    "RIGHT": 90.0,
    "REAR_RIGHT": 135.0,
    "REAR": 180.0,
    "REAR_LEFT": -135.0,
    "LEFT": -90.0,
    "FRONT_LEFT": -45.0,
}


@dataclass
class DirectionEstimate:
    label: str
    relative_offset_deg: float
    confidence: float
    channel_mode: str  # "stereo" | "surround"
    lr_ratio_db: float
    note: str
    itd_samples: int = 0
    itd_deg: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_direction(
    frames: np.ndarray,
    sample_rate: int = 48000,
) -> DirectionEstimate:
    """Estimate relative gunshot direction from a short multi-channel window."""
    data = np.asarray(frames, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, np.newaxis]

    n, ch = data.shape
    if n == 0 or ch == 0:
        return DirectionEstimate(
            label="CENTER",
            relative_offset_deg=0.0,
            confidence=0.0,
            channel_mode="stereo",
            lr_ratio_db=0.0,
            note="empty frame",
        )

    rms = np.sqrt(np.mean(np.square(data), axis=0) + 1e-20)

    if ch >= 6:
        return _estimate_surround(rms)
    return _estimate_stereo(data, rms, sample_rate)


def _label_from_offset(offset_deg: float, surround: bool) -> str:
    """Snap continuous angle to nearest compass label."""
    ang = ((offset_deg + 180) % 360) - 180  # [-180, 180)
    if surround:
        keys = [
            "FRONT",
            "FRONT_RIGHT",
            "RIGHT",
            "REAR_RIGHT",
            "REAR",
            "REAR_LEFT",
            "LEFT",
            "FRONT_LEFT",
        ]
    else:
        # Stereo cannot resolve rear — only front semicircle labels
        keys = ["FRONT", "FRONT_RIGHT", "RIGHT", "LEFT", "FRONT_LEFT"]

    best = keys[0]
    best_d = 999.0
    for k in keys:
        ref = DIRECTION_OFFSETS_DEG[k]
        d = abs(((ang - ref + 180) % 360) - 180)
        if d < best_d:
            best_d = d
            best = k
    if not surround and best == "FRONT" and abs(ang) < 15:
        return "CENTER"
    return best


def _estimate_stereo(
    data: np.ndarray,
    rms: np.ndarray,
    sample_rate: int,
) -> DirectionEstimate:
    """ILD + ITD blend → continuous front-hemisphere angle (-90..+90).

    True rear is impossible with stereo; we stay in the front half-plane.
    """
    left = float(rms[0])
    right = float(rms[1] if rms.size > 1 else rms[0])
    lr_db = float(20.0 * np.log10((right + 1e-9) / (left + 1e-9)))

    # ILD → angle. ±12 dB ≈ full left/right.
    x = float(np.clip(lr_db / 12.0, -1.5, 1.5))
    ild_offset = float(np.tanh(x) * 90.0)

    # ITD via restricted cross-correlation (~±0.7ms head shadow)
    max_lag = max(8, int(0.0007 * sample_rate))
    itd_lag = stereo_itd_samples(data[:, 0], data[:, 1], max_lag=max_lag)
    # Positive lag (right earlier) → sound from right → positive angle
    itd_offset = float(np.clip((itd_lag / float(max_lag)) * 90.0, -90.0, 90.0))

    # Trust ITD more when correlation lag is clear and ILD is mild
    ild_weight = 0.62
    if abs(lr_db) < 2.5 and abs(itd_lag) >= 2:
        ild_weight = 0.4
    elif abs(lr_db) > 8.0:
        ild_weight = 0.78
    offset = float(ild_weight * ild_offset + (1.0 - ild_weight) * itd_offset)
    offset = float(np.clip(offset, -90.0, 90.0))

    label = _label_from_offset(offset, surround=False)
    conf = float(np.clip(0.45 + abs(lr_db) / 14.0 + abs(itd_lag) / (max_lag * 4.0), 0.4, 0.95))
    if abs(lr_db) < 1.5 and abs(itd_lag) < 2:
        conf = float(np.clip(0.65, 0.4, 0.75))

    return DirectionEstimate(
        label=label,
        relative_offset_deg=round(offset, 1),
        confidence=conf,
        channel_mode="stereo",
        lr_ratio_db=lr_db,
        note="stereo: ILD+ITD front hemisphere; rear not separable (2ch)",
        itd_samples=int(itd_lag),
        itd_deg=round(itd_offset, 1),
    )


def _estimate_surround(rms: np.ndarray) -> DirectionEstimate:
    """Energy-weighted angle over 5.1/7.1 channels → continuous 0..360 bearing.

    Assumed order (Windows/WASAPI common):
      0 FL, 1 FR, 2 C, 3 LFE, 4 BL/RL, 5 BR/RR, [6 SL, 7 SR]
    """
    energies = rms.astype(np.float64).copy()
    if energies.size > 3:
        energies[3] = 0.0  # ignore LFE

    bins = {
        "FRONT_LEFT": float(energies[0]) if energies.size > 0 else 0.0,
        "FRONT_RIGHT": float(energies[1]) if energies.size > 1 else 0.0,
        "FRONT": float(energies[2]) if energies.size > 2 else 0.0,
        "REAR_LEFT": float(energies[4]) if energies.size > 4 else 0.0,
        "REAR_RIGHT": float(energies[5]) if energies.size > 5 else 0.0,
        "LEFT": float(energies[6]) if energies.size > 6 else 0.0,
        "RIGHT": float(energies[7]) if energies.size > 7 else 0.0,
    }
    if energies.size < 8:
        bins["LEFT"] = max(bins["LEFT"], bins["FRONT_LEFT"] * 0.35 + bins["REAR_LEFT"] * 0.65)
        bins["RIGHT"] = max(
            bins["RIGHT"], bins["FRONT_RIGHT"] * 0.35 + bins["REAR_RIGHT"] * 0.65
        )
        bins["REAR"] = max(bins.get("REAR", 0.0), (bins["REAR_LEFT"] + bins["REAR_RIGHT"]) * 0.5)

    # Vector sum for continuous angle
    vx = 0.0
    vy = 0.0
    for name, e in bins.items():
        if e <= 0:
            continue
        ang = np.deg2rad(_SURROUND_ANGLES.get(name, DIRECTION_OFFSETS_DEG.get(name, 0.0)))
        # 0° = front = +Y, 90° = right = +X
        vx += e * float(np.sin(ang))
        vy += e * float(np.cos(ang))

    if abs(vx) < 1e-12 and abs(vy) < 1e-12:
        label = max(bins, key=bins.get)
        offset = DIRECTION_OFFSETS_DEG.get(label, 0.0)
    else:
        offset = float(np.rad2deg(np.arctan2(vx, vy)))  # [-180, 180]
        label = _label_from_offset(offset, surround=True)

    total = sum(bins.values()) + 1e-9
    top = max(bins.values()) if bins else 0.0
    conf = float(np.clip(top / total * 1.7, 0.4, 0.98))

    left = float(energies[0]) if energies.size else 1e-9
    right = float(energies[1]) if energies.size > 1 else 1e-9
    lr_db = float(20.0 * np.log10((right + 1e-9) / (left + 1e-9)))

    return DirectionEstimate(
        label=label if label != "FRONT" else "CENTER",
        relative_offset_deg=round(offset, 1),
        confidence=conf,
        channel_mode="surround",
        lr_ratio_db=lr_db,
        note="surround: 8-dir continuous angle",
    )


def fuse_absolute_bearing(compass_heading_deg: float, relative_offset_deg: float) -> float:
    """absolute = (facing + relative) mod 360."""
    return float(compass_heading_deg + relative_offset_deg) % 360.0
