"""Generate an aligned audition track with user-selected voices."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

from backend.local_runtime import QWEN_MODEL_DIR
from backend.timeline_audio import prepare_natural_timing, timing_summary
from backend.voice_catalog import VOICE_IDS

AUDITION_TEXT = {
    18: "赢利三千五百万，职业十年。",
    19: "你凭哪些特质脱颖而出？",
    20: "两位好。",
    21: "我想，许多特质让我在扑克中屡屡获胜。",
    22: "首先是同理心。它很重要，却常被扑克圈低估。",
    23: "了解对手在想什么、感受如何，非常重要。",
    24: "这是我区别于其他对手的优势之一。",
    25: "我心算也很快，从小就擅长数学和数字计算。",
    26: "简单、快速的心算一直是我的强项，还有坚持。",
    27: "毕竟，这是十年的长跑。",
    28: "我希望还能更久。平时生活中，",
}


def srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def role_for_item(item_id: int) -> str:
    if item_id in (18, 19):
        return "host_a"
    return "guest"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("translation_json", type=Path)
    parser.add_argument("assignments_json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start", type=float, default=61.42)
    parser.add_argument("--end", type=float, default=120.66)
    parser.add_argument(
        "--model",
        default=str(QWEN_MODEL_DIR),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = args.output_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    assignments = json.loads(args.assignments_json.read_text(encoding="utf-8"))
    for role in ("host_a", "host_b", "guest"):
        voice_id = str(assignments.get(role, "")).lower()
        if voice_id not in VOICE_IDS:
            raise ValueError(f"Unsupported voice for {role}: {voice_id}")
        assignments[role] = voice_id

    payload = json.loads(args.translation_json.read_text(encoding="utf-8"))
    items = [
        item
        for item in payload["items"]
        if int(item["id"]) in AUDITION_TEXT
        and float(item["start"]) >= args.start
        and float(item["window_end"]) <= args.end + 0.001
    ]
    if not items:
        raise ValueError("No complete translated slots selected")

    loaded_at = time.perf_counter()
    model = Qwen3TTSModel.from_pretrained(
        args.model,
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    load_seconds = time.perf_counter() - loaded_at
    print(json.dumps({"event": "model_loaded", "seconds": round(load_seconds, 3)}), flush=True)

    sample_rate = None
    generated = []
    metrics = []
    synth_started = time.perf_counter()
    for index, item in enumerate(items, 1):
        item_id = int(item["id"])
        role = role_for_item(item_id)
        speaker = assignments[role]
        instruction = (
            "像播客主持人一样自然清晰地交谈，有好奇心，不要播音腔，语速稍快。"
            if role.startswith("host")
            else "像访谈嘉宾一样自信自然地交流，有思考感，不要播音腔，语速明快。"
        )
        started = time.perf_counter()
        torch.manual_seed(20260821 + item_id)
        wavs, current_sr = model.generate_custom_voice(
            text=AUDITION_TEXT[item_id],
            language="Chinese",
            speaker=speaker,
            instruct=instruction,
            do_sample=True,
            temperature=0.8,
            top_p=0.95,
            max_new_tokens=2048,
        )
        audio = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        sample_rate = int(current_sr) if sample_rate is None else sample_rate
        if int(current_sr) != sample_rate:
            audio = librosa.resample(audio, orig_sr=int(current_sr), target_sr=sample_rate)
        window_duration = float(item["window_end"]) - float(item["start"])
        next_start = float(items[index]["start"]) if index < len(items) else args.end
        timing = prepare_natural_timing(
            audio,
            sample_rate,
            window_seconds=window_duration,
            available_until_next_seconds=max(0.0, next_start - float(item["start"])),
        )
        audio = timing.audio
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0.88:
            audio *= 0.88 / peak
        fade = min(round(sample_rate * 0.008), len(audio) // 2)
        if fade:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            audio[:fade] *= ramp
            audio[-fade:] *= ramp[::-1]
        sf.write(segments_dir / f"{item_id:04d}_{role}_{speaker}.wav", audio, sample_rate, subtype="PCM_16")
        generated.append((item, audio))
        row = {
            "id": item_id,
            "role": role,
            "voice": speaker,
            "raw_seconds": round(timing.raw_seconds, 3),
            "rendered_seconds": round(timing.rendered_seconds, 3),
            "window_seconds": round(window_duration, 3),
            "stretch_rate": 1.0,
            "time_stretched": False,
            "trailing_silence_removed": round(timing.trailing_silence_removed, 3),
            "overflow_seconds": round(timing.overflow_seconds, 3),
            "overlap_seconds": round(timing.overlap_seconds, 3),
            "synthesis_seconds": round(time.perf_counter() - started, 3),
        }
        metrics.append(row)
        print(json.dumps({"event": "progress", "current": index, "total": len(items), **row}, ensure_ascii=False), flush=True)

    assert sample_rate is not None
    timeline = np.zeros(round((args.end - args.start) * sample_rate), dtype=np.float32)
    for item, audio in generated:
        offset = max(0, round((float(item["start"]) - args.start) * sample_rate))
        end = min(len(timeline), offset + len(audio))
        timeline[offset:end] += audio[: end - offset]
    peak = float(np.max(np.abs(timeline))) if timeline.size else 0.0
    if peak > 0.89:
        timeline *= 0.89 / peak
    sf.write(args.output_dir / "voice_track.wav", timeline, sample_rate, subtype="PCM_16")

    with (args.output_dir / "subtitles.srt").open("w", encoding="utf-8-sig", newline="\n") as handle:
        for number, item in enumerate(items, 1):
            start = float(item["start"]) - args.start
            end = float(item["window_end"]) - args.start
            handle.write(f"{number}\n{srt_time(start)} --> {srt_time(end)}\n{AUDITION_TEXT[int(item['id'])]}\n\n")
    report = {
        "clip_start": args.start,
        "clip_end": args.end,
        "duration": args.end - args.start,
        "voices": assignments,
        "model_load_seconds": round(load_seconds, 3),
        "synthesis_seconds": round(time.perf_counter() - synth_started, 3),
        **timing_summary(metrics),
        "segments": metrics,
    }
    (args.output_dir / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"event": "complete", "report": report}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
