"""Generate raw CosyVoice segments in its isolated Python 3.10 environment."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

from backend.cosyvoice_runtime import load_cosyvoice, synthesize_cosyvoice
from backend.voice_catalog import COSYVOICE_MODEL_DIR


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = manifest.get("items", [])
    if not items:
        raise ValueError("CosyVoice manifest contains no items")

    loaded_at = time.perf_counter()
    model = load_cosyvoice(COSYVOICE_MODEL_DIR)
    load_seconds = round(time.perf_counter() - loaded_at, 3)
    available = set(model.list_available_spks())
    print(
        json.dumps(
            {
                "event": "model_loaded",
                "engine": "cosyvoice",
                "device": "cuda" if torch.cuda.is_available() else "cpu",
                "seconds": load_seconds,
                "speakers": sorted(available),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    metrics = {}
    for index, item in enumerate(items, 1):
        speaker = str(item["speaker"])
        if speaker not in available:
            raise ValueError(f"CosyVoice speaker is not installed: {speaker}")
        started = time.perf_counter()
        audio = synthesize_cosyvoice(model, str(item["text"]), speaker)
        elapsed = round(time.perf_counter() - started, 3)
        item_id = int(item["id"])
        sf.write(
            args.output_dir / f"{item_id:04d}.wav",
            np.asarray(audio, dtype=np.float32),
            int(model.sample_rate),
            subtype="PCM_16",
        )
        metrics[str(item_id)] = {
            "seconds": elapsed,
            "sample_rate": int(model.sample_rate),
            "voice": item["voice_id"],
        }
        print(
            json.dumps(
                {
                    "event": "cosy_progress",
                    "current": index,
                    "total": len(items),
                    "id": item_id,
                    "seconds": elapsed,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    report = {
        "model_load_seconds": load_seconds,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "segments": metrics,
    }
    (args.output_dir / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"event": "complete", **report}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
