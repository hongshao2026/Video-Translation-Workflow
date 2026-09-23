"""Deterministic, bounded voice-audition planning and audio assembly.

The audition is deliberately a separate authorization scope from full TTS.  It
uses a small projection of the frozen ``tts_segments`` document, covers every
active role, and never changes the provider-native speaking speed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .gates import sha256_file
from .library import atomic_json

AUDITION_SCOPE = "audition_current_inputs"
AUDITION_CAPTURE_MODE = "voice_selection_implies_audition"
DEFAULT_TARGET_SECONDS = 60.0
# A conservative Chinese speech budget.  At roughly two visible characters a
# second this targets one minute while leaving room for punctuation/pauses.
DEFAULT_MAX_CHARACTER_UNITS = 120
DEFAULT_MAX_SEGMENTS = 24
DEFAULT_MAX_ROLES = 24


class AuditionError(RuntimeError):
    pass


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _character_units(text: str) -> int:
    return sum(1 for value in text if not value.isspace())


def _clip_units(text: str, limit: int) -> tuple[str, bool]:
    if limit < 1:
        return "", bool(text.strip())
    out: list[str] = []
    used = 0
    for value in text.strip():
        unit = 0 if value.isspace() else 1
        if used + unit > limit:
            break
        out.append(value)
        used += unit
    clipped = "".join(out).strip()
    return clipped, clipped != text.strip()


def plan_audition_segments(
    full_segments: Sequence[Mapping[str, Any]],
    locked_roles: Mapping[str, str],
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    max_character_units: int = DEFAULT_MAX_CHARACTER_UNITS,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> dict[str, Any]:
    """Select a deterministic, role-covering sample without authorizing full TTS."""

    if not math.isfinite(target_seconds) or target_seconds <= 0 or target_seconds > 60:
        raise AuditionError("试听目标时长必须大于 0 且不超过 60 秒")
    if max_character_units < 1 or max_character_units > DEFAULT_MAX_CHARACTER_UNITS:
        raise AuditionError("试听字符预算超出后端安全上限")
    if max_segments < 1 or max_segments > DEFAULT_MAX_SEGMENTS:
        raise AuditionError("试听片段数量预算超出后端安全上限")
    roles = {
        str(role).strip(): str(voice).strip()
        for role, voice in locked_roles.items()
        if str(role).strip() and str(voice).strip()
    }
    if not roles or len(roles) != len(locked_roles):
        raise AuditionError("冻结角色或音色映射无效")
    if len(roles) > DEFAULT_MAX_ROLES:
        raise AuditionError("角色数量超过一分钟试听的安全上限")
    if len(roles) > max_character_units or len(roles) > max_segments:
        raise AuditionError("试听预算不足以覆盖全部角色")

    normalized: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    rows_by_role: dict[str, list[dict[str, Any]]] = {role: [] for role in roles}
    for index, value in enumerate(full_segments):
        segment_id = str(value.get("segment_id") or "").strip()
        role_id = str(value.get("role_id") or "").strip()
        voice_id = str(value.get("voice_id") or "").strip()
        text = str(value.get("text") or "").strip()
        if not segment_id or segment_id in seen_source_ids:
            raise AuditionError("完整 TTS 输入包含缺失或重复的片段 ID")
        if not role_id or role_id not in roles:
            raise AuditionError(f"完整 TTS 输入包含未锁定角色：{role_id or segment_id}")
        if voice_id != roles[role_id]:
            raise AuditionError(f"完整 TTS 输入的音色与音色锁不一致：{segment_id}")
        if not text:
            raise AuditionError(f"完整 TTS 输入缺少录音文本：{segment_id}")
        if value.get("speed", 1.0) != 1.0:
            raise AuditionError("试听只能使用 Provider 原生 speed=1.0")
        row = {
            "source_index": index,
            "source_segment_id": segment_id,
            "role_id": role_id,
            "voice_id": voice_id,
            "text": text,
        }
        normalized.append(row)
        rows_by_role[role_id].append(row)
        seen_source_ids.add(segment_id)

    missing_roles = [role for role, rows in rows_by_role.items() if not rows]
    if missing_roles:
        raise AuditionError("完整 TTS 输入没有覆盖音色锁角色：" + ", ".join(missing_roles))

    # Reserve a useful short utterance for every role first.  Supplemental
    # lines are added only from the remaining bounded budget.
    # Keep the mandatory role-covering portion to at most half the global
    # character budget.  Supplemental clips may then be dropped after their
    # real duration is known without ever losing role coverage.
    reserved_units = max(len(roles), max_character_units // 2)
    per_role_budget = max(1, min(30, reserved_units // len(roles)))
    choices: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    remaining = max_character_units
    for role in sorted(roles):
        source = rows_by_role[role][0]
        budget = min(per_role_budget, remaining - (len(roles) - len(choices) - 1))
        text, truncated = _clip_units(source["text"], budget)
        if not text:
            raise AuditionError(f"角色 {role} 没有可用的试听文本")
        choices.append(
            {
                **source,
                "text": text,
                "required_role_sample": True,
                "source_text_truncated": truncated,
            }
        )
        used_sources.add(source["source_segment_id"])
        remaining -= _character_units(text)

    for source in normalized:
        if remaining <= 0 or len(choices) >= max_segments:
            break
        if source["source_segment_id"] in used_sources:
            continue
        text, truncated = _clip_units(source["text"], min(30, remaining))
        if not text:
            continue
        choices.append(
            {
                **source,
                "text": text,
                "required_role_sample": False,
                "source_text_truncated": truncated,
            }
        )
        used_sources.add(source["source_segment_id"])
        remaining -= _character_units(text)

    choices.sort(key=lambda row: int(row["source_index"]))
    selected: list[dict[str, Any]] = []
    for index, row in enumerate(choices, 1):
        selected.append(
            {
                "segment_id": f"AUD{index:03d}",
                "source_segment_id": row["source_segment_id"],
                "role_id": row["role_id"],
                "voice_id": row["voice_id"],
                "text": row["text"],
                "speed": 1.0,
                "required_role_sample": row["required_role_sample"],
                "source_text_truncated": row["source_text_truncated"],
            }
        )

    selected_roles = sorted({str(row["role_id"]) for row in selected})
    selected_units = sum(_character_units(str(row["text"])) for row in selected)
    if selected_roles != sorted(roles):
        raise AuditionError("试听选择没有覆盖全部角色")
    if selected_units > max_character_units or len(selected) > max_segments:
        raise AuditionError("试听选择超过安全预算")
    return {
        "target_seconds": target_seconds,
        "max_character_units": max_character_units,
        "estimated_duration_seconds": round(selected_units / 2.0, 3),
        "full_segment_count": len(normalized),
        "selected_segment_count": len(selected),
        "selected_character_units": selected_units,
        "role_count": len(roles),
        "selected_role_ids": selected_roles,
        "all_roles_covered": True,
        "speed": 1.0,
        "offline_rate": 1.0,
        "segments": selected,
    }


def write_json_once_or_same(path: Path, payload: Mapping[str, Any]) -> None:
    """Write an immutable JSON artifact, accepting a byte-equivalent retry."""

    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuditionError(f"现有试听制品无法读取：{path.name}") from exc
        if current == dict(payload):
            return
        raise AuditionError(f"试听版本已存在且输入不同，请使用新版本：{path.name}")
    atomic_json(path, dict(payload))


def _inside(root: Path, value: Path | str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise AuditionError("试听制品路径超出项目目录")
    if must_exist and not resolved.is_file():
        raise AuditionError(f"试听制品不存在：{resolved.name}")
    return resolved


def _binding(root: Path, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AuditionError(f"试听制品不存在：{path.name}")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "byte_size": path.stat().st_size,
    }


def _run(command: Sequence[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise AuditionError(f"无法启动 {label}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise AuditionError(f"{label} 失败（exit={result.returncode}）：{detail}")
    return result


def _audio_duration(path: Path, ffprobe: str) -> float:
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        label="FFprobe 试听检查",
    )
    try:
        value = json.loads(result.stdout)
        duration = float((value.get("format") or {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuditionError("FFprobe 没有返回有效试听时长") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise AuditionError("试听音频时长无效")
    return duration


def _publish_once(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise AuditionError("试听输出已存在但尚无匹配清单，禁止覆盖")
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise AuditionError("试听输出发生并发冲突，禁止覆盖") from exc
    except OSError:
        try:
            with source.open("rb") as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        except FileExistsError as exc:
            raise AuditionError("试听输出发生并发冲突，禁止覆盖") from exc


def assemble_audition_audio(
    *,
    project_root: Path | str,
    selection_path: Path | str,
    tts_manifest_path: Path | str,
    output_path: Path | str,
    manifest_path: Path | str,
    authorization_path: Path | str,
    voice_lock_path: Path | str,
    translation_gate_path: Path | str,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    """Concatenate provider-native clips into a verified mono PCM WAV audition."""

    root = Path(project_root).resolve()
    selection_file = _inside(root, selection_path, must_exist=True)
    tts_manifest_file = _inside(root, tts_manifest_path, must_exist=True)
    authorization_file = _inside(root, authorization_path, must_exist=True)
    voice_lock_file = _inside(root, voice_lock_path, must_exist=True)
    translation_gate_file = _inside(root, translation_gate_path, must_exist=True)
    output_file = _inside(root, output_path)
    manifest_file = _inside(root, manifest_path)

    expected_inputs = {
        "selection": _binding(root, selection_file),
        "tts_manifest": _binding(root, tts_manifest_file),
        "authorization": _binding(root, authorization_file),
        "voice_lock": _binding(root, voice_lock_file),
        "translation_gate": _binding(root, translation_gate_file),
    }
    if manifest_file.exists():
        try:
            cached = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuditionError("现有试听清单无法读取") from exc
        output_binding = cached.get("output") if isinstance(cached, Mapping) else None
        if (
            cached.get("status") == "pass"
            and cached.get("inputs") == expected_inputs
            and isinstance(output_binding, Mapping)
            and _inside(root, str(output_binding.get("path") or "")) == output_file
            and output_file.is_file()
            and output_binding.get("sha256") == sha256_file(output_file)
            and int(output_binding.get("byte_size") or -1) == output_file.stat().st_size
        ):
            return dict(cached)
        raise AuditionError("现有试听清单与当前冻结输入不一致，请使用新版本")
    if output_file.exists():
        raise AuditionError("试听输出已存在但缺少匹配清单，禁止覆盖")

    try:
        selection = json.loads(selection_file.read_text(encoding="utf-8-sig"))
        tts_manifest = json.loads(tts_manifest_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditionError("试听选择或语音清单无法读取") from exc
    selected = selection.get("segments") if isinstance(selection, Mapping) else None
    manifest_segments = tts_manifest.get("segments") if isinstance(tts_manifest, Mapping) else None
    locks = tts_manifest.get("locks") if isinstance(tts_manifest, Mapping) else None
    if (
        selection.get("status") != "pass"
        or selection.get("speed") != 1.0
        or not isinstance(selected, list)
        or not selected
        or tts_manifest.get("status") != "ready"
        or not isinstance(manifest_segments, Mapping)
        or not isinstance(locks, Mapping)
        or locks.get("authorization_scope") != AUDITION_SCOPE
        or locks.get("speed") != 1.0
        or locks.get("offline_rate") != 1.0
    ):
        raise AuditionError("试听选择或 TTS 清单没有锁定试听范围与原生速度")

    tts_root = tts_manifest_file.parent.resolve()
    audio_files: list[Path] = []
    selected_ids: list[str] = []
    required_by_id: dict[str, bool] = {}
    selected_roles: set[str] = set()
    required_roles: set[str] = set()
    for row in selected:
        if not isinstance(row, Mapping):
            raise AuditionError("试听选择包含无效片段")
        segment_id = str(row.get("segment_id") or "")
        entry = manifest_segments.get(segment_id)
        if not isinstance(entry, Mapping) or entry.get("status") != "ready":
            raise AuditionError(f"试听语音片段尚未就绪：{segment_id}")
        relative = Path(str(entry.get("audio_path") or ""))
        audio = (tts_root / relative).resolve()
        if (
            not str(relative)
            or relative.is_absolute()
            or ".." in relative.parts
            or not audio.is_relative_to(tts_root)
            or not audio.is_file()
            or entry.get("sha256") != sha256_file(audio)
            or int(entry.get("byte_size") or -1) != audio.stat().st_size
        ):
            raise AuditionError(f"试听语音片段缺失、越界或哈希不匹配：{segment_id}")
        selected_ids.append(segment_id)
        audio_files.append(audio)
        required_by_id[segment_id] = row.get("required_role_sample") is True
        role_id = str(row.get("role_id") or "").strip()
        if not role_id:
            raise AuditionError(f"试听语音片段缺少角色：{segment_id}")
        selected_roles.add(role_id)
        if required_by_id[segment_id]:
            required_roles.add(role_id)

    if required_roles != selected_roles or len(selected_roles) != int(
        selection.get("role_count") or 0
    ):
        raise AuditionError("试听选择没有为每个角色保留不可省略的样句")

    durations = {
        segment_id: _audio_duration(audio, ffprobe)
        for segment_id, audio in zip(selected_ids, audio_files, strict=True)
    }
    target_seconds = float(selection.get("target_seconds") or 60.0)
    required_duration = sum(
        durations[segment_id]
        for segment_id in selected_ids
        if required_by_id[segment_id]
    )
    if required_duration > target_seconds * 1.1:
        raise AuditionError(
            "覆盖全部角色的原生语速试听已超过约一分钟；请用新版本缩短角色样句"
        )
    included_ids = {
        segment_id for segment_id in selected_ids if required_by_id[segment_id]
    }
    included_duration = required_duration
    for segment_id in selected_ids:
        if segment_id in included_ids:
            continue
        duration = durations[segment_id]
        if included_duration + duration <= target_seconds:
            included_ids.add(segment_id)
            included_duration += duration
    included_pairs = [
        (segment_id, audio)
        for segment_id, audio in zip(selected_ids, audio_files, strict=True)
        if segment_id in included_ids
    ]
    included_segment_ids = [segment_id for segment_id, _audio in included_pairs]
    audio_files = [audio for _segment_id, audio in included_pairs]

    output_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_file.parent,
        prefix=f".{output_file.stem}.",
        suffix=".wav",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        command: list[str] = [ffmpeg, "-v", "error", "-nostdin", "-n"]
        for audio in audio_files:
            command.extend(["-i", str(audio)])
        filters = [
            f"[{index}:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=mono[a{index}]"
            for index in range(len(audio_files))
        ]
        joined = "".join(f"[a{index}]" for index in range(len(audio_files)))
        filters.append(f"{joined}concat=n={len(audio_files)}:v=0:a=1[out]")
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[out]",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "1",
                str(temporary),
            ]
        )
        _run(command, label="FFmpeg 试听组装")
        duration = _audio_duration(temporary, ffprobe)
        # The selection is intentionally conservative.  A small tolerance is
        # allowed for vendor pauses, but voiced content is never time-stretched
        # or truncated to force the limit.
        if duration > float(selection.get("target_seconds") or 60.0) * 1.1:
            raise AuditionError("原生语速试听超过约一分钟；请使用新版本缩短选择，禁止变速或截断")
        _run(
            [
                ffmpeg,
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-i",
                str(temporary),
                "-f",
                "null",
                os.devnull,
            ],
            label="FFmpeg 试听完整解码",
        )
        _publish_once(temporary, output_file)
    finally:
        temporary.unlink(missing_ok=True)

    payload = {
        "schema_version": 1,
        "status": "pass",
        "scope": AUDITION_SCOPE,
        "audio_only": True,
        "full_tts_authorized": False,
        "tts_native_speed": 1.0,
        "offline_rate": 1.0,
        "audio_time_stretch": False,
        "target_seconds": selection.get("target_seconds"),
        "duration_seconds": round(duration, 6),
        "role_count": selection.get("role_count"),
        "all_roles_covered": selection.get("all_roles_covered") is True,
        "synthesized_segment_ids": selected_ids,
        "included_segment_ids": included_segment_ids,
        "supplemental_segments_omitted_for_duration": [
            segment_id for segment_id in selected_ids if segment_id not in included_ids
        ],
        "inputs": expected_inputs,
        "output": _binding(root, output_file),
        "machine_checks": {
            "ffprobe_duration_valid": True,
            "full_decode_pass": True,
            "mono_pcm_48000": True,
            "duration_within_approximately_one_minute": True,
        },
    }
    write_json_once_or_same(manifest_file, payload)
    return payload
