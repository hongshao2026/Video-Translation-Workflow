"""Create a sentence-level pack through MiniMax without exposing credentials."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from backend.media_utils import normalize_audio
from backend.minimax_client import MiniMaxClient, SpeechConfig, estimate_cost

ProgressCallback = Callable[[int, int, dict], None]


def synthesize_minimax_pack(
    *,
    client: MiniMaxClient,
    manifest: dict,
    assignments: dict[str, str],
    config: SpeechConfig,
    pack_dir: Path,
    project_root: Path,
    requests_per_minute: int,
    progress: ProgressCallback,
) -> dict:
    if requests_per_minute not in (10, 20):
        raise ValueError("MiniMax 请求频率只能选择 10 或 20 RPM")
    raw_dir = pack_dir / "raw"
    normalized_dir = pack_dir / "normalized"
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total = len(manifest["segments"])
    interval = 60.0 / requests_per_minute

    for index, segment in enumerate(manifest["segments"], 1):
        role_id = str(segment["role_id"])
        voice_id = str(assignments.get(role_id) or "").strip()
        if not voice_id:
            raise ValueError(f"角色 {segment['role_name']} 尚未选择 MiniMax 音色")
        started = time.perf_counter()
        result = client.synthesize(str(segment["text"]), voice_id, config)
        extension = result.audio_format if result.audio_format in {"mp3", "wav", "flac"} else config.format
        raw_path = raw_dir / f"{segment['segment_id']}.{extension}"
        raw_path.write_bytes(result.audio)
        duration = normalize_audio(
            raw_path,
            normalized_dir / f"{segment['segment_id']}.wav",
            project_root,
        )
        elapsed = time.perf_counter() - started
        row = {
            "segment_id": segment["segment_id"],
            "role": role_id,
            "voice_id": voice_id,
            "trace_id": result.trace_id,
            "raw_seconds": round(duration, 3),
            "synthesis_seconds": round(elapsed, 3),
            "usage_characters": int(
                result.extra_info.get("usage_characters")
                or estimate_cost(str(segment["text"]), config.model)["billable_characters"]
            ),
        }
        rows.append(row)
        progress(index, total, row)
        if index < total:
            time.sleep(max(0.0, interval - elapsed))

    payload = {
        "schema_version": 1,
        "created_at": time.time(),
        "manifest": manifest,
        "report": {
            "ready": True,
            "required_count": total,
            "ready_count": total,
            "missing": [],
            "invalid": [],
            "extras": [],
            "warning_count": 0,
        },
    }
    pack_json = pack_dir / "pack.json"
    pack_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "pack_json": str(pack_json),
        "segments": rows,
        "synthesis_seconds": round(sum(row["synthesis_seconds"] for row in rows), 3),
        "usage_characters": sum(row["usage_characters"] for row in rows),
    }
