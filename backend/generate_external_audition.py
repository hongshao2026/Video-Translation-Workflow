"""Place imported sentence-level audio on the prepared absolute timeline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from backend.audition_data import TARGET_SAMPLE_RATE, srt_time
from backend.timeline_audio import prepare_natural_timing, timing_summary


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("pack_json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    payload = json.loads(args.pack_json.read_text(encoding="utf-8"))
    manifest = payload["manifest"]
    segments = manifest["segments"]
    pack_dir = args.pack_json.parent
    normalized_dir = pack_dir / "normalized"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rendered_dir = args.output_dir / "segments"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    clip_start = float(manifest["clip_start_seconds"])
    clip_end = float(manifest["clip_end_seconds"])
    timeline = np.zeros(round((clip_end - clip_start) * TARGET_SAMPLE_RATE), dtype=np.float32)
    metrics = []

    for index, segment in enumerate(segments, 1):
        segment_id = segment["segment_id"]
        source = normalized_dir / f"{segment_id}.wav"
        if not source.exists():
            raise FileNotFoundError(f"缺少音频：{segment_id}.wav")
        audio, current_sr = sf.read(source, dtype="float32", always_2d=False)
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)
        if current_sr != TARGET_SAMPLE_RATE:
            audio = librosa.resample(
                audio, orig_sr=current_sr, target_sr=TARGET_SAMPLE_RATE
            )

        window_duration = float(segment["slot_seconds"])
        next_start = (
            float(segments[index]["start_seconds"])
            if index < len(segments)
            else clip_end
        )
        timing = prepare_natural_timing(
            audio,
            TARGET_SAMPLE_RATE,
            window_seconds=window_duration,
            available_until_next_seconds=max(
                0.0, next_start - float(segment["start_seconds"])
            ),
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

        offset = max(
            0,
            round(
                (float(segment["start_seconds"]) - clip_start) * TARGET_SAMPLE_RATE
            ),
        )
        end = min(len(timeline), offset + len(audio))
        timeline[offset:end] += audio[: end - offset]
        sf.write(
            rendered_dir / f"{segment_id}_{segment['role_id']}.wav",
            audio,
            TARGET_SAMPLE_RATE,
            subtype="PCM_16",
        )
        row = {
            "segment_id": segment_id,
            "role": segment["role_id"],
            "engine": "external",
            "raw_seconds": round(timing.raw_seconds, 3),
            "rendered_seconds": round(timing.rendered_seconds, 3),
            "window_seconds": round(window_duration, 3),
            "stretch_rate": 1.0,
            "time_stretched": False,
            "trailing_silence_removed": round(timing.trailing_silence_removed, 3),
            "overflow_seconds": round(timing.overflow_seconds, 3),
            "overlap_seconds": round(timing.overlap_seconds, 3),
            "synthesis_seconds": 0,
        }
        metrics.append(row)
        print(
            json.dumps(
                {
                    "event": "external_progress",
                    "current": index,
                    "total": len(segments),
                    **row,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

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
        for number, segment in enumerate(segments, 1):
            start = float(segment["start_seconds"]) - clip_start
            end = float(segment["end_seconds"]) - clip_start
            handle.write(
                f"{number}\n{srt_time(start)} --> {srt_time(end)}\n"
                f"{segment.get('subtitle_text', segment['text'])}\n\n"
            )

    report = {
        "clip_start": clip_start,
        "clip_end": clip_end,
        "duration": clip_end - clip_start,
        "source": "external_audio_pack",
        "model_load_seconds": {},
        "synthesis_seconds": 0,
        "pipeline_seconds": round(time.perf_counter() - started, 3),
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
