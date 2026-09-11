"""List WASAPI loopback devices (CLI smoke test)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audio.capture import get_default_loopback_device, list_loopback_devices


def main() -> None:
    devices = list_loopback_devices()
    default = get_default_loopback_device()
    print(json.dumps(
        {
            "count": len(devices),
            "default": None if default is None else default.to_dict(),
            "devices": [d.to_dict() for d in devices],
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
