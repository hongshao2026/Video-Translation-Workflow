"""Generate browser previews for all official CosyVoice SFT speakers."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from backend.cosyvoice_runtime import load_cosyvoice, synthesize_cosyvoice
from backend.voice_catalog import COSYVOICE_MODEL_DIR, COSYVOICE_VOICES

SAMPLE_TEXT = "你好，欢迎来到声轨工坊，这是我的中文声音试听。"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "voice_previews",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pending = [
        voice
        for voice in COSYVOICE_VOICES
        if args.force or not (args.output_dir / voice["preview_filename"]).exists()
    ]
    if not pending:
        print(json.dumps({"event": "complete", "generated": 0, "total": len(COSYVOICE_VOICES)}))
        return 0

    loaded_at = time.perf_counter()
    model = load_cosyvoice(COSYVOICE_MODEL_DIR)
    print(
        json.dumps(
            {
                "event": "model_loaded",
                "seconds": round(time.perf_counter() - loaded_at, 3),
                "speakers": model.list_available_spks(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    available = set(model.list_available_spks())
    started = time.perf_counter()
    for index, voice in enumerate(pending, 1):
        if voice["speaker"] not in available:
            raise ValueError(f"CosyVoice speaker is not installed: {voice['speaker']}")
        voice_started = time.perf_counter()
        audio = synthesize_cosyvoice(model, SAMPLE_TEXT, voice["speaker"])
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0.9:
            audio *= 0.9 / peak
        sf.write(
            args.output_dir / voice["preview_filename"],
            audio,
            int(model.sample_rate),
            subtype="PCM_16",
        )
        print(
            json.dumps(
                {
                    "event": "progress",
                    "current": index,
                    "total": len(pending),
                    "voice": voice["id"],
                    "seconds": round(time.perf_counter() - voice_started, 3),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "event": "complete",
                "generated": len(pending),
                "total": len(COSYVOICE_VOICES),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
