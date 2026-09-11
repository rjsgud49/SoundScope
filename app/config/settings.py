"""SoundScope application settings (JSON-backed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "calibration" / "settings.json"
RECORDINGS_DIR = ROOT_DIR / "recordings"


class RoiSettings(BaseModel):
    x: int = 850
    y: int = 20
    w: int = 220
    h: int = 48


class Settings(BaseModel):
    sample_rate: int = 48000
    block_size: int = 1024  # ~21ms at 48kHz
    buffer_seconds: float = 3.0
    preferred_device_id: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    waveform_points: int = 512
    ui_push_hz: float = 30.0

    # Detection / radar
    monitor_index: int = 2
    compass_roi: RoiSettings = Field(default_factory=RoiSettings)
    detection_rise_db: float = 8.0
    detection_min_peak: float = 0.03
    event_cooldown_ms: float = 350.0
    event_fade_ms: int = 900
    analysis_window_ms: int = 250
    spectral_gate: bool = True
    gunshot_min_score: float = 0.28
    manual_compass_heading: float | None = None
    tesseract_cmd: str | None = None
    distance_confidence_threshold: float = 0.55
    # "absolute" = compass + audio, "sound_only" = relative audio direction only
    radar_mode: str = "sound_only"

    model_config = ConfigDict(extra="ignore")


def ensure_dirs() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        settings = Settings()
        save_settings(settings)
        return settings
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return Settings.model_validate(data)


def save_settings(settings: Settings) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(
        settings.model_dump_json(indent=2),
        encoding="utf-8",
    )


def update_settings(**kwargs: Any) -> Settings:
    settings = load_settings()
    updated = settings.model_copy(update=kwargs)
    save_settings(updated)
    return updated
