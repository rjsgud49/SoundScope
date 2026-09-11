"""Distance classification (100m bins) from peak/SNR + spectral muffling."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from app.audio.preprocessing import compute_spectral_features


DISTANCE_CLASSES = (
    "DIST_0_100",
    "DIST_100_200",
    "DIST_200_300",
    "DIST_300_400",
    "DIST_400_500",
    "DIST_500_600",
    "DIST_600_PLUS",
)

# Representative label for UI mid/upper edge display
CLASS_UI_LABEL = {
    "DIST_0_100": "0~100m",
    "DIST_100_200": "100~200m",
    "DIST_200_300": "200~300m",
    "DIST_300_400": "300~400m",
    "DIST_400_500": "400~500m",
    "DIST_500_600": "500~600m",
    "DIST_600_PLUS": "600m+",
}

CLASS_APPROX_LABEL = {
    "DIST_0_100": "~50m",
    "DIST_100_200": "~200m",
    "DIST_200_300": "~300m",
    "DIST_300_400": "~400m",
    "DIST_400_500": "~500m",
    "DIST_500_600": "~600m",
    "DIST_600_PLUS": "~600m+",
}


@dataclass
class DistanceEstimate:
    distance_class: str
    label: str
    approx_label: str
    confidence: float
    peak: float
    rms: float
    snr_db: float
    use_approx: bool
    centroid_hz: float = 0.0
    rolloff_hz: float = 0.0
    brightness: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def display_label(self) -> str:
        return self.approx_label if self.use_approx else self.label


def estimate_distance(
    frames: np.ndarray,
    noise_floor: float = 1e-4,
    confidence_threshold: float = 0.55,
    sample_rate: int = 48000,
) -> DistanceEstimate:
    """Peak/SNR score + spectral brightness (far shots are mufflier)."""
    data = np.asarray(frames, dtype=np.float32)
    if data.ndim == 2:
        mono = np.mean(data, axis=1)
    else:
        mono = data

    if mono.size == 0:
        cls = "DIST_600_PLUS"
        return DistanceEstimate(
            distance_class=cls,
            label=CLASS_UI_LABEL[cls],
            approx_label=CLASS_APPROX_LABEL[cls],
            confidence=0.0,
            peak=0.0,
            rms=0.0,
            snr_db=0.0,
            use_approx=True,
        )

    peak = float(np.max(np.abs(mono)))
    rms = float(np.sqrt(np.mean(np.square(mono)) + 1e-20))
    snr_db = float(20.0 * np.log10((rms + 1e-9) / max(noise_floor, 1e-6)))

    feats = compute_spectral_features(mono, sample_rate)
    # Brightness 0..1: close shots keep HF; far ones roll off
    bright = float(
        np.clip(
            0.45 * feats.hf_ratio / 0.4
            + 0.35 * (feats.centroid_hz / 4000.0)
            + 0.2 * (feats.rolloff_hz / 8000.0),
            0.0,
            1.2,
        )
    )

    # Louder / higher SNR / brighter => closer
    score = 0.55 * peak + 0.25 * min(rms * 3.0, 1.0) + 0.2 * min(bright, 1.0)

    if score >= 0.48 and snr_db >= 26 and bright >= 0.35:
        cls = "DIST_0_100"
        conf = 0.78
    elif score >= 0.30 and snr_db >= 20:
        cls = "DIST_100_200"
        conf = 0.72
    elif score >= 0.18 and snr_db >= 15:
        cls = "DIST_200_300"
        conf = 0.64
    elif score >= 0.10 and snr_db >= 11:
        cls = "DIST_300_400"
        conf = 0.56
    elif score >= 0.055 and snr_db >= 7:
        cls = "DIST_400_500"
        conf = 0.5
    elif score >= 0.028 and snr_db >= 4:
        cls = "DIST_500_600"
        conf = 0.45
    else:
        cls = "DIST_600_PLUS"
        conf = 0.4

    # Muffled-but-loud: nudge one bin farther
    order = list(DISTANCE_CLASSES)
    idx = order.index(cls)
    if bright < 0.22 and idx < len(order) - 1 and cls != "DIST_600_PLUS":
        cls = order[min(idx + 1, len(order) - 1)]
        conf *= 0.92

    conf = float(np.clip(conf + (snr_db - 15.0) * 0.01 + (bright - 0.4) * 0.05, 0.2, 0.95))
    use_approx = conf < confidence_threshold

    return DistanceEstimate(
        distance_class=cls,
        label=CLASS_UI_LABEL[cls],
        approx_label=CLASS_APPROX_LABEL[cls],
        confidence=conf,
        peak=peak,
        rms=rms,
        snr_db=snr_db,
        use_approx=use_approx,
        centroid_hz=round(feats.centroid_hz, 1),
        rolloff_hz=round(feats.rolloff_hz, 1),
        brightness=round(bright, 3),
    )
