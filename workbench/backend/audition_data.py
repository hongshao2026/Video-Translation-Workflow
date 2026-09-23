"""Shared timeline data for the prepared USHR-lJ25Qo voice audition."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from backend.project_config import CONFIG, project_path

PROJECT_ID = CONFIG.get("video_id", "USHR-lJ25Qo")
WORKSPACE_DIR = Path(__file__).resolve().parents[2]
TRANSLATION_JSON = (
    WORKSPACE_DIR
    / f"{PROJECT_ID}_run"
    / "work"
    / "translation_adjudicated_v2_final.json"
)
TRANSLATION_JSON = project_path("translation", TRANSLATION_JSON)
_AUDITION_PATH = project_path("audition_items", Path()) if CONFIG else None
_AUDITION = json.loads(_AUDITION_PATH.read_text(encoding="utf-8-sig")) if _AUDITION_PATH and _AUDITION_PATH.is_file() else None

# The frozen sixty-second window contains both hosts and Fedor. Stable slots 25
# and 26 cross speaker boundaries, so their approved Chinese subtitle text is
# split only for derived audition speech. The approved subtitle JSON is not
# modified. No paid synthesis is authorized at this stage.
AUDITION_START = 100.0
AUDITION_END = 160.0
if CONFIG:
    AUDITION_START = float((_AUDITION or {}).get("start", 0))
    AUDITION_END = float((_AUDITION or {}).get("end", 60))
AUDITION_DURATION = AUDITION_END - AUDITION_START
TARGET_SAMPLE_RATE = 24_000

ROLE_NAMES = {
    "host_rene": "René Kuhlman（主持人）",
    "host_adam": "Adam Carmichael（联合主持人）",
    "guest_fedor": "Fedor Holz（嘉宾）",
}
AUDITION_ROLE_IDS = {"host_rene", "host_adam", "guest_fedor"}
if CONFIG:
    ROLE_NAMES = (_AUDITION or {}).get("roles", {})
    AUDITION_ROLE_IDS = set(ROLE_NAMES)


def _item_id(parent_id: int, part: int = 1) -> int:
    if not 0 < part < 10:
        raise ValueError(f"Unsupported utterance part: {parent_id}.{part}")
    return parent_id * 10 + part


def _load_translation_slots() -> dict[int, dict]:
    payload = json.loads(TRANSLATION_JSON.read_text(encoding="utf-8"))
    slots = payload if isinstance(payload, list) else payload.get("slots") or payload.get("items")
    if not isinstance(slots, list):
        raise TypeError("The approved translation has no slots/items array")
    # Numeric audition parent keys are a derived mapping; approved stable IDs stay intact.
    result = {}
    for item in slots:
        stable_id = str(item["id"])
        key = int(stable_id.removeprefix("S"))
        if key in result:
            raise ValueError("Duplicate derived audition parent ID")
        result[key] = item
    return result


def _full_slot(slot: dict, role_id: str) -> dict:
    parent_id = int(slot["id"])
    source_text = str(slot.get("source") or slot.get("source_text") or "")
    zh = str(slot.get("subtitle_zh") or slot.get("zh") or slot.get("translation_zh") or "")
    if not source_text or not zh:
        raise ValueError(f"Audition slot {parent_id} has incomplete text")
    return {
        "id": _item_id(parent_id),
        "utterance_id": f"{parent_id:04d}.1",
        "parent_id": parent_id,
        "part": 1,
        "start": float(slot["start"]),
        "end": float(slot["end"]),
        "window_end": float(slot["end"]),
        "source_text": source_text,
        "zh": zh,
        "subtitle_zh": zh,
        "role_id": role_id,
    }


def _split_slot(
    parent_id: int,
    part: int,
    start: float,
    end: float,
    role_id: str,
    source_text: str,
    zh: str,
) -> dict:
    return {
        "id": _item_id(parent_id, part),
        "utterance_id": f"{parent_id:04d}.{part}",
        "parent_id": parent_id,
        "part": part,
        "start": start,
        "end": end,
        "window_end": end,
        "source_text": source_text,
        "zh": zh,
        "subtitle_zh": zh,
        "role_id": role_id,
    }


def _load_selected_items() -> tuple[list[dict], dict[int, str]]:
    # A clean checkout is a multi-project library, not a snapshot of one user's
    # prepared video.  Keep legacy audition support available when its frozen
    # translation exists, but never make importing the server depend on that
    # machine-local file.
    if not TRANSLATION_JSON.is_file():
        return [], {}
    if CONFIG:
        rows = (_AUDITION or {}).get("items", [])
        slots = _load_translation_slots()
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(int(row["parent_id"]), []).append(row)
            if row["role_id"] not in AUDITION_ROLE_IDS or not (AUDITION_START <= float(row["start"]) < float(row["end"]) <= AUDITION_END):
                raise ValueError("Invalid configured audition role or time range")
        for parent_id, parts in grouped.items():
            parts.sort(key=lambda row: float(row["start"]))
            if "".join(row["subtitle_zh"] for row in parts) != slots[parent_id]["subtitle_zh"]:
                raise ValueError(f"Audition changed approved subtitle text: {parent_id}")
        return rows, {int(row["id"]): str(row["role_id"]) for row in rows}
    slots = _load_translation_slots()
    rows: list[dict] = []
    for slot_id in range(15, 24):
        rows.append(_full_slot(slots[slot_id], "host_adam"))
    rows.append(_full_slot(slots[24], "host_rene"))
    rows.extend(
        [
            _split_slot(
                25,
                1,
                149.37,
                150.49,
                "host_rene",
                "Fedor Holz.",
                "Fedor Holz。",
            ),
            _split_slot(
                25,
                2,
                150.93,
                152.21,
                "host_adam",
                "All right, Fedor, welcome to the pod.",
                "好了，Fedor，欢迎来到节目。",
            ),
            _split_slot(
                26,
                1,
                152.99,
                153.77,
                "guest_fedor",
                "Thanks for having me.",
                "谢谢邀请。",
            ),
            _split_slot(
                26,
                2,
                154.55,
                159.37,
                "host_adam",
                "Well, you've had an amazing career, to put it lightly, and it's hard to know where to begin with your story.",
                "要说你拥有一段精彩的职业生涯，都算是轻描淡写了；你的故事丰富到让人不知道该从哪里说起。",
            ),
        ]
    )
    rows.sort(key=lambda item: float(item["start"]))
    roles = {int(item["id"]): str(item["role_id"]) for item in rows}
    active_roles = set(roles.values())
    if active_roles != AUDITION_ROLE_IDS:
        raise ValueError(f"Audition must contain all three casting roles, got: {sorted(active_roles)}")
    if any(float(item["start"]) < AUDITION_START or float(item["end"]) > AUDITION_END for item in rows):
        raise ValueError("Audition item falls outside the frozen sixty-second window")
    return rows, roles


_SELECTED_ITEMS, _ROLE_BY_ITEM = _load_selected_items()
AUDITION_SUBTITLE_TEXT = {
    int(item["id"]): str(item["subtitle_zh"]) for item in _SELECTED_ITEMS
}
AUDITION_TEXT = {int(item["id"]): str(item["zh"]) for item in _SELECTED_ITEMS}
AUDITION_SEGMENT_COUNT = len(_SELECTED_ITEMS)


def srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def display_time(seconds: float) -> str:
    return srt_time(seconds).replace(",", ".")


def role_for_item(item_id: int) -> str:
    try:
        return _ROLE_BY_ITEM[item_id]
    except KeyError:
        raise ValueError(f"Unknown audition item: {item_id}") from None


def load_audition_items(translation_json: Path) -> list[dict]:
    if translation_json.resolve() != TRANSLATION_JSON.resolve():
        raise ValueError(f"Audition is frozen to {TRANSLATION_JSON}")
    if not translation_json.exists():
        raise FileNotFoundError(f"Translation file not found: {translation_json}")
    return [dict(item) for item in _SELECTED_ITEMS]


def build_external_manifest(
    translation_json: Path,
    role_names: dict[str, str] | None = None,
) -> dict:
    names = {**ROLE_NAMES, **(role_names or {})}
    rows = []
    for sequence, item in enumerate(load_audition_items(translation_json), 1):
        item_id = int(item["id"])
        role_id = role_for_item(item_id)
        start = float(item["start"])
        end = float(item["window_end"])
        segment_id = f"segment_{sequence:04d}"
        rows.append(
            {
                "segment_id": segment_id,
                "source_id": str(item["utterance_id"]),
                "filename": f"{segment_id}.wav",
                "role_id": role_id,
                "role_name": str(names.get(role_id) or role_id).strip()[:80],
                "start_time": display_time(start),
                "end_time": display_time(end),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "slot_seconds": round(end - start, 3),
                "text": AUDITION_TEXT[item_id],
                "subtitle_text": AUDITION_SUBTITLE_TEXT[item_id],
                "tts_text_mode": "approved_translation_or_manual_speaker_split",
            }
        )
    return {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "clip_start_seconds": AUDITION_START,
        "clip_end_seconds": AUDITION_END,
        "clip_duration_seconds": round(AUDITION_DURATION, 3),
        "audio_naming": "segment_0001.wav",
        "accepted_audio": ["wav", "mp3", "m4a", "aac", "flac", "ogg"],
        "segments": rows,
    }


def manifest_csv_bytes(manifest: dict) -> bytes:
    fields = [
        "segment_id",
        "filename",
        "role_id",
        "role_name",
        "start_time",
        "end_time",
        "slot_seconds",
        "text",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(manifest["segments"])
    return buffer.getvalue().encode("utf-8-sig")
