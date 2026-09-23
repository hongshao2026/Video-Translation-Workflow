"""Device-local paths for optional local speech engines.

No path in this module is tied to a developer account or drive.  Cloud-only
installations can leave every variable unset; optional engine checks will then
report the conventional ignored ``.device`` location as unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path

WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
DEVICE_ROOT = Path(
    os.environ.get("DUB_DEVICE_DIR", str(WORKBENCH_ROOT / ".device"))
).expanduser()


def device_path(variable: str, relative_default: str) -> Path:
    value = os.environ.get(variable, "").strip()
    return Path(value).expanduser() if value else DEVICE_ROOT / relative_default


QWEN_MODEL_DIR = device_path(
    "DUB_QWEN_MODEL_DIR", "models/Qwen3-TTS-12Hz-0.6B-CustomVoice-HF"
)
KOKORO_MODEL_DIR = device_path(
    "DUB_KOKORO_MODEL_DIR", "models/Kokoro-82M-v1.1-zh"
)
COSYVOICE_MODEL_DIR = device_path(
    "DUB_COSYVOICE_MODEL_DIR", "models/CosyVoice-300M-SFT"
)
COSYVOICE_REPO_DIR = device_path("DUB_COSYVOICE_REPO_DIR", "tools/CosyVoice")

QWEN_PYTHON = device_path("DUB_QWEN_PYTHON", "venvs/qwen3_tts/Scripts/python.exe")
KOKORO_PYTHON = device_path("DUB_KOKORO_PYTHON", "venvs/kokoro/Scripts/python.exe")
COSYVOICE_PYTHON = device_path(
    "DUB_COSYVOICE_PYTHON", "venvs/cosyvoice/Scripts/python.exe"
)
