"""Generate a timeline-aligned audition with Qwen, Kokoro, and CosyVoice."""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch

from backend.audition_data import (
    AUDITION_END,
    AUDITION_START,
    AUDITION_SUBTITLE_TEXT,
    AUDITION_TEXT,
    TARGET_SAMPLE_RATE,
    load_audition_items,
    role_for_item,
    srt_time,
)
from backend.local_runtime import COSYVOICE_PYTHON, QWEN_MODEL_DIR
from backend.timeline_audio import prepare_natural_timing, timing_summary
from backend.voice_catalog import KOKORO_MODEL_DIR, VOICE_IDS, VOICE_MAP


def load_kokoro():
    from kokoro import KModel, KPipeline

    device = "cuda" if torch.cuda.is_available() else "cpu"
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
    return model, pipeline


def synthesize_kokoro(text: str, voice: dict, pipeline) -> np.ndarray:
    results = list(pipeline(text, voice=voice["voice_path"], speed=1.0))
    if not results:
        raise RuntimeError(f"Kokoro returned no audio for {voice['id']}")
    return np.concatenate(
        [np.asarray(result.audio, dtype=np.float32).reshape(-1) for result in results]
    )


def load_qwen(model_path: str):
    from qwen_tts import Qwen3TTSModel

    return Qwen3TTSModel.from_pretrained(
        model_path,
        device_map="cuda:0" if torch.cuda.is_available() else "cpu",
        dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
        attn_implementation="sdpa",
    )


def synthesize_qwen(text: str, role: str, voice: dict, model, seed: int) -> tuple[np.ndarray, int]:
    instruction = (
        "像播客主持人一样自然清晰地交谈，有好奇心，不要播音腔，语速稍快。"
        if role.startswith("host") or role == "leon"
        else "像访谈嘉宾一样自信自然地交流，有思考感，不要播音腔，语速明快。"
    )
    torch.manual_seed(seed)
    wavs, sample_rate = model.generate_custom_voice(
        text=text,
        language="Chinese",
        speaker=voice["speaker"],
        instruct=instruction,
        do_sample=True,
        temperature=0.8,
        top_p=0.95,
        max_new_tokens=2048,
    )
    return np.asarray(wavs[0], dtype=np.float32).reshape(-1), int(sample_rate)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("translation_json", type=Path)
    parser.add_argument("assignments_json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start", type=float, default=AUDITION_START)
    parser.add_argument("--end", type=float, default=AUDITION_END)
    parser.add_argument(
        "--qwen-model",
        default=str(QWEN_MODEL_DIR),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = args.output_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    assignments = json.loads(args.assignments_json.read_text(encoding="utf-8"))

    items = load_audition_items(args.translation_json)
    items = [
        item
        for item in items
        if float(item["start"]) >= args.start
        and float(item["window_end"]) <= args.end + 0.001
    ]
    if not items:
        raise ValueError("No complete translated slots selected")

    active_roles = {role_for_item(int(item["id"])) for item in items}
    for role in active_roles:
        voice_id = str(assignments.get(role, "")).lower()
        if voice_id not in VOICE_IDS:
            raise ValueError(f"Unsupported voice for {role}: {voice_id}")
        assignments[role] = voice_id
    engines = {VOICE_MAP[assignments[role]]["engine"] for role in active_roles}
    model_load_seconds: dict[str, float] = {}
    kokoro_model = kokoro_pipeline = qwen_model = None
    cosy_report: dict = {"segments": {}}
    synth_started = time.perf_counter()

    if "cosyvoice" in engines:
        if not COSYVOICE_PYTHON.exists():
            raise FileNotFoundError(f"CosyVoice Python not found: {COSYVOICE_PYTHON}")
        cosy_dir = args.output_dir / "cosyvoice_raw"
        cosy_dir.mkdir(parents=True, exist_ok=True)
        cosy_items = []
        for item in items:
            item_id = int(item["id"])
            role = role_for_item(item_id)
            voice = VOICE_MAP[assignments[role]]
            if voice["engine"] == "cosyvoice":
                cosy_items.append(
                    {
                        "id": item_id,
                        "text": AUDITION_TEXT[item_id],
                        "voice_id": voice["id"],
                        "speaker": voice["speaker"],
                    }
                )
        manifest_path = cosy_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"items": cosy_items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                str(COSYVOICE_PYTHON),
                "-m",
                "backend.generate_cosyvoice_segments",
                str(manifest_path),
                "--output-dir",
                str(cosy_dir),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        cosy_tail = []
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            cosy_tail.append(line)
            cosy_tail = cosy_tail[-30:]
            if line.startswith("{"):
                print(line, flush=True)
        if process.wait() != 0:
            raise RuntimeError("CosyVoice segment generation failed:\n" + "\n".join(cosy_tail[-10:]))
        cosy_report = json.loads((cosy_dir / "metrics.json").read_text(encoding="utf-8"))
        model_load_seconds["cosyvoice"] = cosy_report["model_load_seconds"]
        print(
            json.dumps(
                {
                    "event": "model_loaded",
                    "engine": "cosyvoice",
                    "seconds": model_load_seconds["cosyvoice"],
                }
            ),
            flush=True,
        )

    if "kokoro" in engines:
        loaded_at = time.perf_counter()
        kokoro_model, kokoro_pipeline = load_kokoro()
        model_load_seconds["kokoro"] = round(time.perf_counter() - loaded_at, 3)
        print(json.dumps({"event": "model_loaded", "engine": "kokoro", "seconds": model_load_seconds["kokoro"]}), flush=True)
    if "qwen" in engines:
        loaded_at = time.perf_counter()
        qwen_model = load_qwen(args.qwen_model)
        model_load_seconds["qwen"] = round(time.perf_counter() - loaded_at, 3)
        print(json.dumps({"event": "model_loaded", "engine": "qwen", "seconds": model_load_seconds["qwen"]}), flush=True)

    generated = []
    metrics = []
    for index, item in enumerate(items, 1):
        item_id = int(item["id"])
        role = role_for_item(item_id)
        voice_id = assignments[role]
        voice = VOICE_MAP[voice_id]
        started = time.perf_counter()
        segment_synthesis_seconds = None
        if voice["engine"] == "kokoro":
            assert kokoro_pipeline is not None
            audio = synthesize_kokoro(AUDITION_TEXT[item_id], voice, kokoro_pipeline)
            current_sr = TARGET_SAMPLE_RATE
        elif voice["engine"] == "qwen":
            assert qwen_model is not None
            audio, current_sr = synthesize_qwen(
                AUDITION_TEXT[item_id], role, voice, qwen_model, 20260821 + item_id
            )
        else:
            cosy_path = args.output_dir / "cosyvoice_raw" / f"{item_id:04d}.wav"
            audio, current_sr = sf.read(cosy_path, dtype="float32", always_2d=False)
            audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim > 1:
                audio = np.mean(audio, axis=1)
            segment_synthesis_seconds = float(
                cosy_report["segments"][str(item_id)]["seconds"]
            )
        if current_sr != TARGET_SAMPLE_RATE:
            audio = librosa.resample(
                audio, orig_sr=current_sr, target_sr=TARGET_SAMPLE_RATE
            )
        window_duration = float(item["window_end"]) - float(item["start"])
        next_start = float(items[index]["start"]) if index < len(items) else args.end
        timing = prepare_natural_timing(
            audio,
            TARGET_SAMPLE_RATE,
            window_seconds=window_duration,
            available_until_next_seconds=max(0.0, next_start - float(item["start"])),
        )
        audio = timing.audio
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0.88:
            audio *= 0.88 / peak
        fade = min(round(TARGET_SAMPLE_RATE * 0.008), len(audio) // 2)
        if fade:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            audio[:fade] *= ramp
            audio[-fade:] *= ramp[::-1]
        sf.write(
            segments_dir / f"{item_id:04d}_{role}_{voice_id}.wav",
            audio,
            TARGET_SAMPLE_RATE,
            subtype="PCM_16",
        )
        generated.append((item, audio))
        row = {
            "id": item_id,
            "role": role,
            "voice": voice_id,
            "engine": voice["engine"],
            "raw_seconds": round(timing.raw_seconds, 3),
            "rendered_seconds": round(timing.rendered_seconds, 3),
            "window_seconds": round(window_duration, 3),
            "stretch_rate": 1.0,
            "time_stretched": False,
            "trailing_silence_removed": round(timing.trailing_silence_removed, 3),
            "overflow_seconds": round(timing.overflow_seconds, 3),
            "overlap_seconds": round(timing.overlap_seconds, 3),
            "synthesis_seconds": round(
                segment_synthesis_seconds
                if segment_synthesis_seconds is not None
                else time.perf_counter() - started,
                3,
            ),
        }
        metrics.append(row)
        print(
            json.dumps(
                {"event": "progress", "current": index, "total": len(items), **row},
                ensure_ascii=False,
            ),
            flush=True,
        )

    timeline = np.zeros(
        round((args.end - args.start) * TARGET_SAMPLE_RATE), dtype=np.float32
    )
    for item, audio in generated:
        offset = max(
            0, round((float(item["start"]) - args.start) * TARGET_SAMPLE_RATE)
        )
        end = min(len(timeline), offset + len(audio))
        timeline[offset:end] += audio[: end - offset]
    peak = float(np.max(np.abs(timeline))) if timeline.size else 0.0
    if peak > 0.89:
        timeline *= 0.89 / peak
    sf.write(
        args.output_dir / "voice_track.wav",
        timeline,
        TARGET_SAMPLE_RATE,
        subtype="PCM_16",
    )

    with (args.output_dir / "subtitles.srt").open(
        "w", encoding="utf-8-sig", newline="\n"
    ) as handle:
        for number, item in enumerate(items, 1):
            start = float(item["start"]) - args.start
            end = float(item["window_end"]) - args.start
            handle.write(
                f"{number}\n{srt_time(start)} --> {srt_time(end)}\n"
                f"{AUDITION_SUBTITLE_TEXT[int(item['id'])]}\n\n"
            )

    del qwen_model, kokoro_model, kokoro_pipeline
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    report = {
        "clip_start": args.start,
        "clip_end": args.end,
        "duration": args.end - args.start,
        "voices": assignments,
        "engines": {role: VOICE_MAP[voice]["engine"] for role, voice in assignments.items()},
        "model_load_seconds": model_load_seconds,
        "synthesis_seconds": round(sum(row["synthesis_seconds"] for row in metrics), 3),
        "pipeline_seconds": round(time.perf_counter() - synth_started, 3),
        **timing_summary(metrics),
        "segments": metrics,
    }
    (args.output_dir / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps({"event": "complete", "report": report}, ensure_ascii=False),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
