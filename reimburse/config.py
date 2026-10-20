from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "报销材料整理助手"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "ReimburseOrganizer"
CONFIG_PATH = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "last_folder": "",
    "applicant": "",
    "department": "",
    "event_type": "出差",
    "use_ocr": True,
    "include_subfolders": True,
    "output_dir": "",
    "window_geometry": "",
}


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception:
        pass
    return data


def save_settings(settings: dict[str, Any]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings)
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
