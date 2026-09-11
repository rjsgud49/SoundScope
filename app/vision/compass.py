"""PUBG-style compass heading OCR from a top HUD ROI (latency-focused)."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class CompassReading:
    heading_deg: float | None
    raw_text: str
    confidence: float
    method: str
    error: str | None = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_tesseract_cmd(explicit: str | None = None) -> str | None:
    """Return a usable tesseract.exe path, or None."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)

    found = shutil.which("tesseract")
    if found:
        return found

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Tesseract-OCR"
        / "tesseract.exe",
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _center_crops(gray: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Only the center heading digits — skip the full compass strip."""
    h, w = gray.shape[:2]
    crops: list[tuple[str, np.ndarray]] = []
    specs = (
        ("tight", 0.44, 0.56, 0.35),
        ("center", 0.40, 0.60, 0.25),
    )
    # If ROI is already narrow, just use whole image first
    if w <= 220:
        crops.append(("roi", gray))
    for name, x0r, x1r, y0r in specs:
        x0, x1 = int(w * x0r), int(w * x1r)
        y0 = int(h * y0r)
        crop = gray[y0:h, x0:x1]
        if crop.size:
            crops.append((name, crop))
    return crops


def _prep(gray_crop: np.ndarray) -> list[tuple[str, np.ndarray]]:
    import cv2

    # Cap upscale — large ROIs were 5x and very slow
    h, w = gray_crop.shape[:2]
    target_h = 64
    scale = max(2.0, min(4.0, target_h / max(h, 1)))
    big = cv2.resize(gray_crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (3, 3), 0)
    _, otsu_inv = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bright = cv2.bitwise_not(cv2.inRange(big, 150, 255))
    return [("otsu_inv", otsu_inv), ("bright", bright)]


def _parse_heading(text: str) -> float | None:
    raw = text.upper()

    def collect(s: str) -> list[int]:
        out: list[int] = []
        compact = re.sub(r"\D", "", s)
        if len(compact) >= 2:
            for n in (3, 2):
                for i in range(0, len(compact) - n + 1):
                    val = int(compact[i : i + n])
                    if 0 <= val <= 359:
                        out.append(val)
        for m in re.finditer(r"\d{1,3}", re.sub(r"[^0-9]", " ", s)):
            val = int(m.group(0))
            if 0 <= val <= 359:
                out.append(val)
        return out

    candidates = collect(raw)
    if candidates:
        ranked = sorted(
            candidates,
            key=lambda v: (
                0 if 100 <= v <= 359 else 1 if 10 <= v <= 99 else 2,
                abs(v - 180),
            ),
        )
        return float(ranked[0])

    cleaned_card = re.sub(r"[^NSEW]", " ", raw)
    cardinals = {
        "NE": 45.0,
        "SE": 135.0,
        "SW": 225.0,
        "NW": 315.0,
        "N": 0.0,
        "E": 90.0,
        "S": 180.0,
        "W": 270.0,
    }
    tokens = cleaned_card.split()
    for key in ("NE", "SE", "SW", "NW", "N", "E", "S", "W"):
        if key in tokens:
            return cardinals[key]

    compact = re.sub(r"\s+", "", raw)
    if len(compact) <= 4:
        confused = (
            compact.replace("O", "0")
            .replace("Q", "0")
            .replace("D", "0")
            .replace("I", "1")
            .replace("|", "1")
            .replace("L", "1")
            .replace("Z", "2")
            .replace("S", "5")
            .replace("B", "8")
        )
        candidates = collect(confused)
        if candidates:
            return float(candidates[0])
    return None


def _is_good_hit(text: str, heading: float) -> bool:
    digits = re.sub(r"\D", "", text)
    if not (0 <= heading <= 359):
        return False
    # Strong hit: clean 2-3 digit read
    return len(digits) in (2, 3) and digits == str(int(heading))


def read_compass_heading(
    bgr: np.ndarray,
    tesseract_cmd: str | None = None,
) -> CompassReading:
    """OCR degrees from compass ROI. Fast path with early exit."""
    import time

    t0 = time.perf_counter()

    try:
        import cv2
        import pytesseract
    except ImportError as exc:
        return CompassReading(
            heading_deg=None,
            raw_text="",
            confidence=0.0,
            method="ocr",
            error=str(exc),
            elapsed_ms=0.0,
        )

    cmd = resolve_tesseract_cmd(tesseract_cmd)
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    else:
        return CompassReading(
            heading_deg=None,
            raw_text="",
            confidence=0.0,
            method="ocr",
            error=(
                "Tesseract OCR is not installed or not on PATH. "
                "Install from https://github.com/UB-Mannheim/tesseract/wiki "
                "or set tesseract_cmd in settings."
            ),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    if bgr is None or bgr.size == 0:
        return CompassReading(
            heading_deg=None,
            raw_text="",
            confidence=0.0,
            method="ocr",
            error="empty ROI image",
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    if float(np.mean(bgr)) < 8.0:
        return CompassReading(
            heading_deg=None,
            raw_text="",
            confidence=0.0,
            method="ocr",
            error=(
                "ROI가 거의 검은 화면입니다. 배그가 보이는 모니터를 선택하고 "
                "상단 나침반 위치에 ROI를 맞추세요."
            ),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    try:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        texts: list[str] = []
        # Few attempts, early exit on clean 2-3 digit hit
        for crop_name, crop in _center_crops(gray):
            for prep_name, img in _prep(crop):
                for psm in (7, 8):  # single line / single word only
                    config = f"--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789"
                    text = (pytesseract.image_to_string(img, config=config) or "").strip()
                    if not text:
                        continue
                    label = f"{crop_name}/{prep_name}/psm{psm}:{text}"
                    texts.append(label)
                    heading = _parse_heading(text)
                    if heading is None:
                        continue
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    digit_len = len(re.sub(r"\D", "", text))
                    # Ignore lone single digits (often caret/noise)
                    if digit_len < 2 and not _is_good_hit(text, heading):
                        continue
                    conf = 0.92 if _is_good_hit(text, heading) else 0.75
                    if digit_len <= 4 or _is_good_hit(text, heading):
                        return CompassReading(
                            heading_deg=float(heading),
                            raw_text=text,
                            confidence=conf,
                            method=f"ocr:{crop_name}:{prep_name}:psm{psm}",
                            error=None,
                            elapsed_ms=elapsed,
                        )

        return CompassReading(
            heading_deg=None,
            raw_text=" | ".join(texts[:6]),
            confidence=0.0,
            method="ocr",
            error=(
                "ROI에서 각도 숫자를 빠르게 읽지 못했습니다. "
                "중앙 숫자만 남기도록 ROI를 더 좁히세요 (권장 w≈160)."
            ),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )
    except pytesseract.TesseractNotFoundError:
        return CompassReading(
            heading_deg=None,
            raw_text="",
            confidence=0.0,
            method="ocr",
            error=(
                "Tesseract OCR is not installed or not on PATH. "
                "or set tesseract_cmd in settings."
            ),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )
    except Exception as exc:
        return CompassReading(
            heading_deg=None,
            raw_text="",
            confidence=0.0,
            method="ocr",
            error=str(exc),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )
