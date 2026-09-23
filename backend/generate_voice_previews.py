"""Generate one reusable Chinese preview for every built-in Qwen3-TTS voice."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

from backend.local_runtime import QWEN_MODEL_DIR
from backend.voice_catalog import VOICES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default=str(QWEN_MODEL_DIR),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "voice_previews",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    model = Qwen3TTSModel.from_pretrained(
        args.model,
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    rows = []
    sample = "你好，欢迎来到声轨工坊。我会用自然的中文，为这个角色完成配音。"
    for index, voice in enumerate(VOICES):
        output = args.output_dir / f"{voice['id']}.wav"
        if output.exists() and not args.force:
            rows.append({"voice": voice["id"], "path": str(output), "cached": True})
            continue
        started = time.perf_counter()
        torch.manual_seed(90310 + index)
        wavs, sample_rate = model.generate_custom_voice(
            text=sample,
            language="Chinese",
            speaker=voice["speaker"],
            instruct=f"{voice['tone']}。像真人对话一样自然，不要播音腔，语速适中。",
            do_sample=True,
            temperature=0.78,
            top_p=0.95,
            max_new_tokens=1400,
        )
        sf.write(output, wavs[0], sample_rate, subtype="PCM_16")
        row = {
            "voice": voice["id"],
            "path": str(output.resolve()),
            "duration": round(len(wavs[0]) / sample_rate, 3),
            "seconds": round(time.perf_counter() - started, 3),
            "cached": False,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    manifest = args.output_dir / "manifest.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
