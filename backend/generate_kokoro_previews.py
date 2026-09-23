"""Generate a short local preview for every installed Kokoro Chinese voice."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from kokoro import KModel, KPipeline

from backend.voice_catalog import KOKORO_MODEL_DIR, KOKORO_VOICES

SAMPLE_TEXT = "你好，欢迎来到声轨工坊，这是我的中文声音试听。"


def main() -> int:
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
        for voice in KOKORO_VOICES
        if args.force or not (args.output_dir / voice["preview_filename"]).exists()
    ]
    if not pending:
        print(json.dumps({"event": "complete", "generated": 0, "total": len(KOKORO_VOICES)}))
        return 0

    device = "cuda" if torch.cuda.is_available() else "cpu"
    loaded_at = time.perf_counter()
    model = KModel(
        repo_id="hexgrad/Kokoro-82M-v1.1-zh",
        config=str(KOKORO_MODEL_DIR / "config.json"),
        model=str(KOKORO_MODEL_DIR / "kokoro-v1_1-zh.pth"),
    ).to(device).eval()
    pipeline = KPipeline(
        lang_code="z",
        repo_id="hexgrad/Kokoro-82M-v1.1-zh",
        model=model,
    )
    print(
        json.dumps(
            {"event": "model_loaded", "device": device, "seconds": round(time.perf_counter() - loaded_at, 3)}
        ),
        flush=True,
    )

    started = time.perf_counter()
    for index, voice in enumerate(pending, 1):
        voice_started = time.perf_counter()
        results = list(
            pipeline(
                SAMPLE_TEXT,
                voice=voice["voice_path"],
                speed=1.04,
            )
        )
        audio = np.concatenate(
            [np.asarray(result.audio, dtype=np.float32).reshape(-1) for result in results]
        )
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0.9:
            audio *= 0.9 / peak
        sf.write(args.output_dir / voice["preview_filename"], audio, 24000, subtype="PCM_16")
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
                "total": len(KOKORO_VOICES),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
