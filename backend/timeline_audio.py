"""Natural-speed timeline preparation shared by every dubbing engine."""

from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np


@dataclass(frozen=True)
class NaturalTiming:
    audio: np.ndarray
    raw_seconds: float
    rendered_seconds: float
    trailing_silence_removed: float
    overflow_seconds: float
    overlap_seconds: float


def prepare_natural_timing(
    audio: np.ndarray,
    sample_rate: int,
    *,
    window_seconds: float,
    available_until_next_seconds: float,
    top_db: float = 40.0,
    tail_pad_seconds: float = 0.04,
) -> NaturalTiming:
    """Trim only trailing silence; never stretch, resample, or truncate speech."""

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    raw_seconds = len(samples) / sample_rate
    rendered = samples
    intervals = librosa.effects.split(
        samples,
        top_db=top_db,
        frame_length=1024,
        hop_length=256,
    )
    if len(intervals):
        tail_pad = round(tail_pad_seconds * sample_rate)
        trim_at = min(len(samples), int(intervals[-1][1]) + tail_pad)
        if trim_at < len(samples):
            rendered = samples[:trim_at].copy()

    rendered_seconds = len(rendered) / sample_rate
    return NaturalTiming(
        audio=rendered,
        raw_seconds=raw_seconds,
        rendered_seconds=rendered_seconds,
        trailing_silence_removed=max(0.0, raw_seconds - rendered_seconds),
        overflow_seconds=max(0.0, rendered_seconds - window_seconds),
        overlap_seconds=max(0.0, rendered_seconds - available_until_next_seconds),
    )


def timing_summary(metrics: list[dict]) -> dict[str, int | float | str]:
    overflow_rows = [row for row in metrics if float(row.get("overflow_seconds") or 0) > 0.05]
    overlap_rows = [row for row in metrics if float(row.get("overlap_seconds") or 0) > 0.05]
    return {
        "timing_policy": "natural_no_stretch",
        # Kept for old clients; natural-speed rendering always reports 1.0.
        "max_stretch_rate": 1.0,
        "timing_overflow_count": len(overflow_rows),
        "timing_overlap_count": len(overlap_rows),
        "max_overflow_seconds": round(
            max((float(row.get("overflow_seconds") or 0) for row in metrics), default=0.0),
            3,
        ),
    }
