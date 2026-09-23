"""Persistent, non-secret cache metadata for MiniMax system voice previews."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PREVIEW_SAMPLE_TEXT = "欢迎来到本期访谈。今天我们聊聊风险、选择，以及一个人为什么会坚持自己的判断。"

# MiniMax's current catalogue mixes legacy IDs with language-prefixed IDs.
# These 30 legacy entries are the Mandarin system voices that precede the
# newer Chinese (Mandarin) block in get_voice.
LEGACY_MANDARIN_VOICE_IDS = frozenset(
    {
        "male-qn-qingse",
        "male-qn-jingying",
        "male-qn-badao",
        "male-qn-daxuesheng",
        "female-shaonv",
        "female-yujie",
        "female-chengshu",
        "female-tianmei",
        "male-qn-qingse-jingpin",
        "male-qn-jingying-jingpin",
        "male-qn-badao-jingpin",
        "male-qn-daxuesheng-jingpin",
        "female-shaonv-jingpin",
        "female-yujie-jingpin",
        "female-chengshu-jingpin",
        "female-tianmei-jingpin",
        "clever_boy",
        "cute_boy",
        "lovely_girl",
        "cartoon_pig",
        "bingjiao_didi",
        "junlang_nanyou",
        "chunzhen_xuedi",
        "lengdan_xiongzhang",
        "badao_shaoye",
        "tianxin_xiaoling",
        "qiaopi_mengmei",
        "wumei_yujie",
        "diadia_xuemei",
        "danya_xuejie",
    }
)
SPECIAL_MANDARIN_VOICE_IDS = frozenset({"Arrogant_Miss", "Robot_Armor"})


def is_mandarin_system_voice(voice: dict[str, Any]) -> bool:
    """Return whether an account-catalogue row is an official Mandarin voice."""

    if voice.get("category") != "system":
        return False
    voice_id = str(voice.get("voice_id") or "")
    return (
        voice_id in LEGACY_MANDARIN_VOICE_IDS
        or voice_id in SPECIAL_MANDARIN_VOICE_IDS
        or voice_id.startswith("Chinese (Mandarin)_")
    )


def load_preview_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "voices": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "voices": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("voices"), dict):
        return {"version": 1, "voices": {}}
    return payload


def _ready_entry(entry: Any, preview_dir: Path) -> bool:
    if not isinstance(entry, dict) or entry.get("status") != "ready":
        return False
    filename = str(entry.get("filename") or "")
    if not filename or Path(filename).name != filename:
        return False
    path = preview_dir / filename
    return path.is_file() and path.stat().st_size > 0


def enrich_voices_with_previews(
    voices: Iterable[dict[str, Any]],
    manifest_path: Path,
    preview_dir: Path,
    *,
    media_prefix: str = "/media/minimax/catalog",
) -> list[dict[str, Any]]:
    manifest = load_preview_manifest(manifest_path)
    entries = manifest["voices"]
    rows: list[dict[str, Any]] = []
    for voice in voices:
        row = dict(voice)
        entry = entries.get(str(row.get("voice_id") or ""))
        ready = _ready_entry(entry, preview_dir)
        row.update(
            {
                "language": "zh-CN" if is_mandarin_system_voice(row) else None,
                "preview_ready": ready,
                "preview_url": (
                    f"{media_prefix}/{entry['filename']}" if ready else None
                ),
                "preview_model": entry.get("model") if ready else None,
                "preview_sample_text": entry.get("sample_text") if ready else None,
                "preview_duration_ms": entry.get("audio_length_ms") if ready else None,
            }
        )
        rows.append(row)
    return rows


def cached_voice_rows(
    manifest_path: Path,
    preview_dir: Path,
) -> list[dict[str, Any]]:
    manifest = load_preview_manifest(manifest_path)
    entries = sorted(
        (entry for entry in manifest["voices"].values() if isinstance(entry, dict)),
        key=lambda entry: int(entry.get("catalog_index") or 0),
    )
    voices = [
        {
            "voice_id": entry.get("voice_id"),
            "voice_name": entry.get("voice_name") or entry.get("voice_id"),
            "description": entry.get("description") or "普通话系统音色。",
            "category": entry.get("category") or "system",
            "created_time": entry.get("created_time"),
        }
        for entry in entries
        if entry.get("voice_id") and _ready_entry(entry, preview_dir)
    ]
    return enrich_voices_with_previews(voices, manifest_path, preview_dir)
