"""Shared ffmpeg helpers for uploaded and API-generated sentence audio."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_media_command(args: list[str], cwd: Path, timeout: int = 180) -> str:
    result = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout or "音频无法解码").strip()[-800:])
    return result.stdout.strip()


def normalize_audio(source: Path, destination: Path, cwd: Path) -> float:
    temporary = destination.with_name(f".{destination.stem}.incoming.wav")
    try:
        run_media_command(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(source),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "24000",
                "-c:a", "pcm_s16le", str(temporary),
            ],
            cwd,
        )
        duration_text = run_media_command(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(temporary),
            ],
            cwd,
            timeout=30,
        )
        duration = float(duration_text)
        if duration <= 0.05:
            raise ValueError("音频为空或短于 0.05 秒")
        if duration > 300:
            raise ValueError("单句音频超过 5 分钟")
        temporary.replace(destination)
        return duration
    finally:
        temporary.unlink(missing_ok=True)
