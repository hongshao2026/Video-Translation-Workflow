"""Build native-speed Chinese audio and a working-master retime timeline.

The module deliberately owns the deterministic bridge between paid TTS and
rendering.  Every TTS response is decoded to the same PCM format, but no tempo
filter, time stretch, voiced-content trim, or truncation is permitted.  The
source video absorbs every duration difference through a complete, monotonic
working-master -> Chinese-output mapping.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import wave
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

EPSILON = 1e-6
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
SAFE_SEGMENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class AudioTimelineError(RuntimeError):
    """Raised when frozen inputs cannot produce a trustworthy timeline."""


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioTimelineError(f"{label}必须是有限数字") from exc
    if not math.isfinite(number):
        raise AudioTimelineError(f"{label}必须是有限数字")
    return number


def _root_path(value: Path | str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise AudioTimelineError("项目目录不存在")
    return root


def _inside(
    root: Path,
    value: Path | str,
    *,
    must_exist: bool = False,
    require_file: bool = False,
) -> Path:
    raw = Path(value).expanduser()
    path = (raw if raw.is_absolute() else root / raw).resolve()
    if not path.is_relative_to(root):
        raise AudioTimelineError("制品路径超出项目目录")
    if must_exist and not path.exists():
        raise AudioTimelineError(f"制品不存在：{path.name}")
    if require_file and not path.is_file():
        raise AudioTimelineError(f"制品不是文件：{path.name}")
    return path


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise AudioTimelineError("制品路径无法转换为项目相对路径") from exc


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudioTimelineError(f"{label}不是可读取的 JSON 对象") from exc
    if not isinstance(value, dict):
        raise AudioTimelineError(f"{label}必须是 JSON 对象")
    return value


def _write_bytes_once_or_same(path: Path, data: bytes) -> None:
    """Publish immutable output, while allowing an identical interrupted retry."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and hashlib.sha256(data).hexdigest() == _sha256_file(path):
            return
        raise AudioTimelineError(f"版本化制品已存在且内容不同：{path.name}")
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, delete=False, prefix=f".{path.name}.", suffix=".tmp"
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    _publish_temp_once_or_same(temporary, path)


def _write_json_once_or_same(path: Path, value: object) -> None:
    _write_bytes_once_or_same(path, _canonical_json_bytes(value))


def _publish_temp_once_or_same(temporary: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if destination.exists():
            if destination.is_file() and _sha256_file(destination) == _sha256_file(temporary):
                temporary.unlink(missing_ok=True)
                return
            raise AudioTimelineError(
                f"版本化制品已存在且内容不同：{destination.name}"
            )
        try:
            # The temporary lives beside the destination, so an atomic hard
            # link gives us create-if-absent semantics on supported local
            # filesystems without ever replacing a published version.
            os.link(temporary, destination)
        except FileExistsError:
            if not destination.is_file() or _sha256_file(destination) != _sha256_file(
                temporary
            ):
                raise AudioTimelineError(
                    f"版本化制品并发写入冲突：{destination.name}"
                )
        except OSError:
            # Some removable/network filesystems disallow hard links.  The
            # exclusive destination handle still prevents replacement; a
            # failed copy is removed because it was never a valid artifact.
            try:
                with temporary.open("rb") as source, destination.open("xb") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
            except FileExistsError:
                if not destination.is_file() or _sha256_file(
                    destination
                ) != _sha256_file(temporary):
                    raise AudioTimelineError(
                        f"版本化制品并发写入冲突：{destination.name}"
                    )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
    finally:
        temporary.unlink(missing_ok=True)


def _artifact(root: Path, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AudioTimelineError(f"制品不存在：{path.name}")
    return {
        "path": _relative(root, path),
        "sha256": _sha256_file(path),
        "byte_size": path.stat().st_size,
    }


def _extract_rows(value: object, *, label: str) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    if isinstance(value, list):
        rows = value
        document: dict[str, Any] = {}
    elif isinstance(value, dict):
        document = value
        rows = (
            value.get("segments")
            or value.get("slots")
            or value.get("items")
            or value.get("translation")
        )
    else:
        raise AudioTimelineError(f"{label}必须是数组或 JSON 对象")
    if not isinstance(rows, list) or not rows:
        raise AudioTimelineError(f"{label}缺少非空 segments/slots/items 数组")
    if not all(isinstance(row, Mapping) for row in rows):
        raise AudioTimelineError(f"{label}包含无效条目")
    return list(rows), document


def _read_json_value(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudioTimelineError(f"{label}不是可读取的 JSON") from exc


def build_tts_segments(
    *,
    project_root: Path | str,
    translation_path: Path | str,
    output_path: Path | str,
    version: int,
    assignments: Mapping[str, str] | None = None,
    voice_lock_path: Path | str | None = None,
    time_basis: str = "working_master",
) -> dict[str, Any]:
    """Derive immutable paid-TTS inputs from approved translation and voices.

    ``recording_zh`` (or ``tts_text``) is used only for the spoken text.  The
    approved ``subtitle_zh`` remains separately frozen for the visible SRT.
    """

    root = _root_path(project_root)
    if int(version) < 1:
        raise AudioTimelineError("TTS segments 版本号必须大于 0")
    translation_file = _inside(root, translation_path, must_exist=True, require_file=True)
    output_file = _inside(root, output_path)
    translation_value = _read_json_value(translation_file, "正式翻译")
    source_rows, _translation_document = _extract_rows(
        translation_value, label="正式翻译"
    )

    normalized_assignments = {
        str(role).strip(): str(voice).strip()
        for role, voice in (assignments or {}).items()
        if str(role).strip() and str(voice).strip()
    }
    if assignments is not None and len(normalized_assignments) != len(assignments):
        raise AudioTimelineError("角色到音色映射包含空值或规范化后的重复角色")
    voice_lock_binding: dict[str, Any] | None = None
    if voice_lock_path is not None:
        voice_file = _inside(root, voice_lock_path, must_exist=True, require_file=True)
        voice_lock = _read_object(voice_file, "音色锁")
        if voice_lock.get("status") != "voice_selection_locked_audition_ready":
            raise AudioTimelineError("音色锁状态无效")
        lock_rows = voice_lock.get("roles")
        if not isinstance(lock_rows, list) or not lock_rows:
            raise AudioTimelineError("音色锁没有角色映射")
        locked: dict[str, str] = {}
        for row in lock_rows:
            if not isinstance(row, Mapping):
                raise AudioTimelineError("音色锁角色结构无效")
            role = str(row.get("role_id") or "").strip()
            voice = str(row.get("voice_id") or "").strip()
            if not role or not voice or role in locked:
                raise AudioTimelineError("音色锁角色缺失、重复或未选择音色")
            locked[role] = voice
        if normalized_assignments and normalized_assignments != locked:
            raise AudioTimelineError("传入音色与冻结音色锁不一致")
        normalized_assignments = locked
        voice_lock_binding = _artifact(root, voice_file)
    if not normalized_assignments:
        raise AudioTimelineError("必须提供完整的角色到音色映射")
    if time_basis not in {"working_master", "source_master"}:
        raise AudioTimelineError("time_basis 必须是 working_master 或 source_master")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_end = 0.0
    used_roles: set[str] = set()
    for index, source in enumerate(source_rows, start=1):
        segment_id = str(
            source.get("segment_id") or source.get("id") or source.get("stable_id") or ""
        ).strip()
        if not SAFE_SEGMENT_ID.fullmatch(segment_id) or segment_id in seen:
            raise AudioTimelineError(f"第 {index} 条稳定 ID 缺失、重复或不安全")
        if time_basis == "working_master":
            start_value = source.get(
                "working_start", source.get("source_start", source.get("start"))
            )
            end_value = source.get(
                "working_end", source.get("source_end", source.get("end"))
            )
        else:
            start_value = source.get("source_start", source.get("start"))
            end_value = source.get("source_end", source.get("end"))
        start = _finite(start_value, f"{segment_id} 开始时间")
        end = _finite(end_value, f"{segment_id} 结束时间")
        if start < -EPSILON or end <= start + EPSILON:
            raise AudioTimelineError(f"{segment_id} 的时间区间无效")
        if start < previous_end - EPSILON:
            raise AudioTimelineError("正式翻译的时间区间重叠或顺序改变")
        subtitle = str(source.get("subtitle_zh") or "").strip()
        if not subtitle:
            raise AudioTimelineError(f"{segment_id} 缺少批准的 subtitle_zh")
        recording_value = source.get("recording_zh")
        recording_source = "recording_zh"
        if recording_value is None or not str(recording_value).strip():
            recording_value = source.get("tts_text")
            recording_source = "tts_text"
        if recording_value is None or not str(recording_value).strip():
            recording_value = subtitle
            recording_source = "subtitle_zh"
        text = str(recording_value).strip()
        role = str(source.get("role_id") or source.get("speaker") or "").strip()
        if not role or role not in normalized_assignments:
            raise AudioTimelineError(f"{segment_id} 的角色没有锁定音色")
        voice = normalized_assignments[role]
        row = {
            "segment_id": segment_id,
            "stable_id": segment_id,
            "source_start": round(start, 6),
            "source_end": round(end, 6),
            "role_id": role,
            "subtitle_zh": subtitle,
            "text": text,
            "recording_text_source": recording_source,
            "voice_id": voice,
            "speed": 1.0,
        }
        if time_basis == "working_master":
            row["working_start"] = round(start, 6)
            row["working_end"] = round(end, 6)
        row["input_sha256"] = _canonical_sha256(row)
        rows.append(row)
        seen.add(segment_id)
        used_roles.add(role)
        previous_end = end

    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass",
        "version": int(version),
        "time_basis": time_basis,
        "paths_are_relative_to_project": True,
        "translation": _artifact(root, translation_file),
        "assignments_sha256": _canonical_sha256(normalized_assignments),
        "used_roles": sorted(used_roles),
        "segment_count": len(rows),
        "segments_sha256": _canonical_sha256(rows),
        "speed": 1.0,
        "offline_rate": 1.0,
        "segments": rows,
    }
    if voice_lock_binding is not None:
        payload["voice_lock"] = voice_lock_binding
    _write_json_once_or_same(output_file, payload)
    return {
        **payload,
        "path": _relative(root, output_file),
        "sha256": _sha256_file(output_file),
    }


def plan_audio_timeline(
    segments: Sequence[Mapping[str, Any]],
    audio_frames: Mapping[str, int],
    *,
    working_duration: float,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> dict[str, Any]:
    """Pure frame-accurate plan preserving every dialogue-free source gap."""

    duration = _finite(working_duration, "工作母版时长")
    if duration <= 0:
        raise AudioTimelineError("工作母版时长必须大于 0")
    if sample_rate <= 0:
        raise AudioTimelineError("PCM 采样率必须大于 0")
    if not segments:
        raise AudioTimelineError("没有可组装的中文语音片段")

    planned: list[dict[str, Any]] = []
    retime: list[dict[str, Any]] = []
    source_cursor = 0.0
    target_cursor_frames = 0
    seen: set[str] = set()

    def append_retime(kind: str, source_start: float, source_end: float, frames: int) -> None:
        nonlocal target_cursor_frames
        if source_end <= source_start + EPSILON:
            return
        if frames <= 0:
            raise AudioTimelineError("正长度画面区间不能映射到零长度音频")
        target_start_frames = target_cursor_frames
        target_cursor_frames += frames
        retime.append(
            {
                "kind": kind,
                "source_start": round(source_start, 6),
                "source_end": round(source_end, 6),
                "target_start": target_start_frames / sample_rate,
                "target_end": target_cursor_frames / sample_rate,
            }
        )

    for index, value in enumerate(segments, start=1):
        segment_id = str(value.get("segment_id") or value.get("id") or "").strip()
        if not SAFE_SEGMENT_ID.fullmatch(segment_id) or segment_id in seen:
            raise AudioTimelineError(f"第 {index} 个音频片段 ID 无效或重复")
        start = _finite(
            value.get("working_start", value.get("source_start", value.get("start"))),
            f"{segment_id} 工作母版开始时间",
        )
        end = _finite(
            value.get("working_end", value.get("source_end", value.get("end"))),
            f"{segment_id} 工作母版结束时间",
        )
        if start < source_cursor - EPSILON or end <= start + EPSILON:
            raise AudioTimelineError("中文片段时间重叠、倒序或长度无效")
        if end > duration + max(EPSILON, 1.0 / sample_rate):
            raise AudioTimelineError(f"{segment_id} 越出正式工作母版")
        start = max(source_cursor, start)
        end = min(duration, end)
        frames = int(audio_frames.get(segment_id, 0))
        if frames <= 0:
            raise AudioTimelineError(f"{segment_id} 没有已验证的 PCM 帧")
        gap_seconds = max(0.0, start - source_cursor)
        gap_frames = max(1, round(gap_seconds * sample_rate)) if gap_seconds > EPSILON else 0
        append_retime("dialogue_free_gap", source_cursor, start, gap_frames)
        target_start_frames = target_cursor_frames
        append_retime("dialogue", start, end, frames)
        target_end_frames = target_cursor_frames
        planned.append(
            {
                **dict(value),
                "segment_id": segment_id,
                "working_start": round(start, 6),
                "working_end": round(end, 6),
                "target_start": target_start_frames / sample_rate,
                "target_end": target_end_frames / sample_rate,
                "target_start_frame": target_start_frames,
                "target_end_frame": target_end_frames,
                "audio_frames": frames,
                "audio_duration": frames / sample_rate,
                "preceding_gap_frames": gap_frames,
            }
        )
        source_cursor = end
        seen.add(segment_id)

    trailing_seconds = max(0.0, duration - source_cursor)
    trailing_frames = (
        max(1, round(trailing_seconds * sample_rate))
        if trailing_seconds > EPSILON
        else 0
    )
    append_retime("dialogue_free_gap", source_cursor, duration, trailing_frames)
    if not retime:
        raise AudioTimelineError("无法生成视频重定时计划")
    if abs(float(retime[0]["source_start"])) > EPSILON:
        raise AudioTimelineError("重定时计划没有从工作母版 0 秒开始")
    if abs(float(retime[-1]["source_end"]) - duration) > EPSILON:
        raise AudioTimelineError("重定时计划没有覆盖工作母版结尾")
    for previous, current in pairwise(retime):
        if abs(float(previous["source_end"]) - float(current["source_start"])) > EPSILON:
            raise AudioTimelineError("重定时计划的工作母版覆盖不连续")
        if abs(float(previous["target_end"]) - float(current["target_start"])) > EPSILON:
            raise AudioTimelineError("重定时计划的中文时轴不连续")
    return {
        "sample_rate": sample_rate,
        "source_duration": duration,
        "target_frames": target_cursor_frames,
        "target_duration": target_cursor_frames / sample_rate,
        "trailing_gap_frames": trailing_frames,
        "segments": planned,
        "retime_segments": retime,
        "source_coverage_complete": True,
        "source_gap_count": 0,
        "source_overlap_count": 0,
        "target_gap_count": 0,
        "target_overlap_count": 0,
        "overlap_count": 0,
    }


def _run_command(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    label: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioTimelineError(f"{label}无法执行：{type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise AudioTimelineError(f"{label}失败（exit={result.returncode}）：{detail}")
    return result


def _probe_json(
    path: Path,
    *,
    ffprobe: str,
    entries: str,
    runner: CommandRunner,
    label: str,
) -> dict[str, Any]:
    result = _run_command(
        runner,
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            entries,
            "-of",
            "json",
            str(path),
        ],
        label=label,
        timeout=120,
    )
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AudioTimelineError(f"{label}返回无效 JSON") from exc
    if not isinstance(value, dict):
        raise AudioTimelineError(f"{label}返回结构无效")
    return value


def _probe_working_master(
    path: Path, *, ffprobe: str, runner: CommandRunner
) -> dict[str, Any]:
    probe = _probe_json(
        path,
        ffprobe=ffprobe,
        entries=(
            "format=duration:stream=index,codec_type,width,height,"
            "avg_frame_rate,r_frame_rate,duration"
        ),
        runner=runner,
        label="正式工作母版 ffprobe",
    )
    streams = [
        row
        for row in probe.get("streams") or []
        if isinstance(row, Mapping) and row.get("codec_type") == "video"
    ]
    if not streams:
        raise AudioTimelineError("正式工作母版没有视频流")
    video = streams[0]
    duration_value = (probe.get("format") or {}).get("duration")
    if duration_value in (None, "", "N/A"):
        duration_value = video.get("duration")
    duration = _finite(duration_value, "正式工作母版时长")
    width = int(_finite(video.get("width"), "正式工作母版宽度"))
    height = int(_finite(video.get("height"), "正式工作母版高度"))
    frame_rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    if duration <= 0 or width <= 0 or height <= 0 or frame_rate in {"", "0/0"}:
        raise AudioTimelineError("正式工作母版元数据不完整")
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "frame_rate": frame_rate,
    }


def _wav_details(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, wave.Error) as exc:
        raise AudioTimelineError(f"PCM WAV 无法读取：{path.name}") from exc
    if (
        channels != DEFAULT_CHANNELS
        or sample_width != DEFAULT_SAMPLE_WIDTH
        or sample_rate != DEFAULT_SAMPLE_RATE
        or compression != "NONE"
        or frames <= 0
    ):
        raise AudioTimelineError(f"PCM WAV 规格不一致：{path.name}")
    if frames / sample_rate < 0.001:
        raise AudioTimelineError(f"TTS 片段短于 1 毫秒：{path.name}")
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration": frames / sample_rate,
    }


def _probe_pcm(
    path: Path, *, ffprobe: str, runner: CommandRunner
) -> dict[str, Any]:
    probe = _probe_json(
        path,
        ffprobe=ffprobe,
        entries="format=duration:stream=codec_type,codec_name,sample_rate,channels",
        runner=runner,
        label=f"PCM ffprobe {path.name}",
    )
    audios = [
        row
        for row in probe.get("streams") or []
        if isinstance(row, Mapping) and row.get("codec_type") == "audio"
    ]
    if len(audios) != 1:
        raise AudioTimelineError(f"PCM WAV 必须且只能有一个音频流：{path.name}")
    audio = audios[0]
    if (
        str(audio.get("codec_name") or "") != "pcm_s16le"
        or int(audio.get("sample_rate") or 0) != DEFAULT_SAMPLE_RATE
        or int(audio.get("channels") or 0) != DEFAULT_CHANNELS
    ):
        raise AudioTimelineError(f"ffprobe 验证的 PCM 规格不一致：{path.name}")
    return _wav_details(path)


def _normalise_audio(
    *,
    root: Path,
    raw_path: Path,
    raw_sha256: str,
    segment_id: str,
    normalized_dir: Path,
    ffmpeg: str,
    ffprobe: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    stem = f"{segment_id}.{raw_sha256[:16]}.pcm_s16le_48000_mono"
    output = normalized_dir / f"{stem}.wav"
    sidecar = normalized_dir / f"{stem}.json"

    def cache_metadata(details: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "ready",
            "segment_id": segment_id,
            "raw_path": _relative(root, raw_path),
            "raw_sha256": raw_sha256,
            "output_path": _relative(root, output),
            "output_sha256": _sha256_file(output),
            "byte_size": output.stat().st_size,
            "sample_rate": details["sample_rate"],
            "channels": details["channels"],
            "sample_width": details["sample_width"],
            "frames": details["frames"],
            "duration": details["duration"],
            "offline_rate": 1.0,
            "tempo_filters": [],
            "voiced_content_truncated": False,
        }

    if output.is_file() and sidecar.is_file():
        metadata = _read_object(sidecar, "PCM 缓存清单")
        if (
            metadata.get("status") == "ready"
            and metadata.get("raw_sha256") == raw_sha256
            and metadata.get("output_sha256") == _sha256_file(output)
            and metadata.get("sample_rate") == DEFAULT_SAMPLE_RATE
            and metadata.get("channels") == DEFAULT_CHANNELS
            and metadata.get("sample_width") == DEFAULT_SAMPLE_WIDTH
            and int(metadata.get("frames") or 0) > 0
        ):
            return {**metadata, "path": _relative(root, output), "cached": True}
        raise AudioTimelineError(f"PCM 缓存损坏，必须使用新的输出版本：{segment_id}")
    if output.is_file() and not sidecar.exists():
        # A crash can happen after the immutable PCM was published but before
        # its small sidecar was written.  Re-probe and finish that checkpoint
        # instead of spending another decode or asking for manual recovery.
        details = _probe_pcm(output, ffprobe=ffprobe, runner=runner)
        metadata = cache_metadata(details)
        _write_json_once_or_same(sidecar, metadata)
        return {**metadata, "path": _relative(root, output), "cached": True}
    if output.exists() or sidecar.exists():
        raise AudioTimelineError(f"PCM 缓存不完整，必须使用新的输出版本：{segment_id}")

    normalized_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{segment_id}.", suffix=".incoming.wav", dir=normalized_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(raw_path),
        "-map",
        "0:a:0",
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        str(DEFAULT_CHANNELS),
        "-ar",
        str(DEFAULT_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(temporary),
    ]
    forbidden = {"atempo", "rubberband", "asetrate", "atrim", "-t", "-to"}
    if any(token.lower() in forbidden for token in command):
        raise AudioTimelineError("PCM 标准化计划包含禁用的变速或截断参数")
    try:
        _run_command(
            runner,
            command,
            label=f"TTS PCM 标准化 {segment_id}",
            timeout=600,
        )
        details = _probe_pcm(temporary, ffprobe=ffprobe, runner=runner)
        _publish_temp_once_or_same(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = cache_metadata(details)
    _write_json_once_or_same(sidecar, metadata)
    return {**metadata, "path": _relative(root, output), "cached": False}


def _validate_binding(
    root: Path,
    binding: object,
    *,
    label: str,
    expected_path: Path | None = None,
) -> Path:
    if not isinstance(binding, Mapping):
        raise AudioTimelineError(f"{label}缺少路径与哈希绑定")
    raw_path = str(binding.get("path") or "")
    expected_sha = str(binding.get("sha256") or "")
    if not raw_path or Path(raw_path).is_absolute() or len(expected_sha) != 64:
        raise AudioTimelineError(f"{label}的路径或哈希无效")
    path = _inside(root, raw_path, must_exist=True, require_file=True)
    if expected_path is not None and path != expected_path.resolve():
        raise AudioTimelineError(f"{label}没有绑定当前文件")
    if _sha256_file(path) != expected_sha:
        raise AudioTimelineError(f"{label}哈希不匹配")
    return path


def _map_source_point(mapping: Mapping[str, Any], value: float) -> float:
    rows = mapping.get("segments")
    if not isinstance(rows, list):
        raise AudioTimelineError("原视频到工作母版映射缺少 segments")
    source_duration = _finite(mapping.get("source_duration_seconds"), "原视频时长")
    if abs(value - source_duration) <= EPSILON:
        return _finite(mapping.get("working_duration_seconds"), "工作母版时长")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_start = _finite(row.get("source_start"), "映射源开始")
        source_end = _finite(row.get("source_end"), "映射源结束")
        if source_start - EPSILON <= value < source_end - EPSILON or abs(
            value - source_start
        ) <= EPSILON:
            if row.get("kind") == "remove":
                return _finite(row.get("working_anchor"), "删除区间工作锚点")
            return _finite(row.get("working_start"), "保留区间工作开始") + (
                value - source_start
            )
    raise AudioTimelineError("原视频时间点没有映射到正式工作母版")


def _map_source_interval(
    mapping: Mapping[str, Any], start: float, end: float, segment_id: str
) -> tuple[float, float]:
    rows = mapping.get("segments")
    if not isinstance(rows, list):
        raise AudioTimelineError("原视频到工作母版映射缺少 segments")
    for row in rows:
        if not isinstance(row, Mapping) or row.get("kind") != "remove":
            continue
        removed_start = _finite(row.get("source_start"), "删除区间开始")
        removed_end = _finite(row.get("source_end"), "删除区间结束")
        if min(end, removed_end) - max(start, removed_start) > EPSILON:
            raise AudioTimelineError(
                f"{segment_id} 与已删除广告区间相交，必须从正式工作母版重新冻结"
            )
    mapped_start = _map_source_point(mapping, start)
    mapped_end = _map_source_point(mapping, end)
    if mapped_end <= mapped_start + EPSILON:
        raise AudioTimelineError(f"{segment_id} 映射到工作母版后没有正长度")
    return mapped_start, mapped_end


def _normalise_timeline_segments(
    rows: Sequence[Mapping[str, Any]],
    *,
    document: Mapping[str, Any],
    ad_gate: Mapping[str, Any],
    mapping: Mapping[str, Any] | None,
    working_duration: float,
) -> tuple[list[dict[str, Any]], str]:
    raw_basis = str(
        document.get("time_basis")
        or document.get("timeline_basis")
        or document.get("source_timeline")
        or ""
    ).strip()
    aliases = {
        "working": "working_master",
        "formal_working_master": "working_master",
        "edited_master": "working_master",
        "source": "source_master",
        "original_master": "source_master",
    }
    basis = aliases.get(raw_basis, raw_basis)
    removed = ad_gate.get("removed_segments") or []
    if not basis:
        if ad_gate.get("formal_slots_based_on_working_master") is True or not removed:
            basis = "working_master"
        else:
            raise AudioTimelineError(
                "存在广告删除时，语音输入必须明确 time_basis=working_master 或 source_master"
            )
    if basis not in {"working_master", "source_master"}:
        raise AudioTimelineError("语音输入的 time_basis 无效")
    if basis == "source_master" and mapping is None:
        raise AudioTimelineError("原视频时轴输入缺少 source_to_edit_timeline 映射")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_end = 0.0
    for index, row in enumerate(rows, start=1):
        segment_id = str(row.get("segment_id") or row.get("id") or "").strip()
        if not SAFE_SEGMENT_ID.fullmatch(segment_id) or segment_id in seen:
            raise AudioTimelineError(f"第 {index} 个语音片段 ID 无效或重复")
        if _finite(row.get("speed", 1.0), f"{segment_id} TTS speed") != 1.0:
            raise AudioTimelineError("正式中文 TTS 必须使用原生 speed=1.0")
        if basis == "working_master":
            start_value = row.get(
                "working_start", row.get("source_start", row.get("start"))
            )
            end_value = row.get(
                "working_end", row.get("source_end", row.get("end"))
            )
        else:
            start_value = row.get("source_start", row.get("start"))
            end_value = row.get("source_end", row.get("end"))
        input_start = _finite(start_value, f"{segment_id} 开始时间")
        input_end = _finite(end_value, f"{segment_id} 结束时间")
        if input_start < -EPSILON or input_end <= input_start + EPSILON:
            raise AudioTimelineError(f"{segment_id} 的输入时间区间无效")
        if basis == "source_master":
            assert mapping is not None
            working_start, working_end = _map_source_interval(
                mapping, input_start, input_end, segment_id
            )
        else:
            working_start, working_end = input_start, input_end
        if working_start < previous_end - EPSILON:
            raise AudioTimelineError("映射后的语音片段在工作母版上重叠或倒序")
        if working_end > working_duration + 0.05:
            raise AudioTimelineError(f"{segment_id} 越出正式工作母版")
        subtitle = str(row.get("subtitle_zh") or row.get("subtitle") or row.get("text") or "").strip()
        if not subtitle:
            raise AudioTimelineError(f"{segment_id} 缺少字幕文本")
        normalized.append(
            {
                **dict(row),
                "segment_id": segment_id,
                "input_time_basis": basis,
                "input_source_start": round(input_start, 6),
                "input_source_end": round(input_end, 6),
                "working_start": round(max(0.0, working_start), 6),
                "working_end": round(min(working_duration, working_end), 6),
                "subtitle_zh": subtitle,
            }
        )
        previous_end = working_end
        seen.add(segment_id)
    return normalized, basis


def _validate_tts_manifest(
    *,
    root: Path,
    manifest_path: Path,
    expected_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = _read_object(manifest_path, "TTS manifest")
    locks = manifest.get("locks")
    manifest_rows = manifest.get("segments")
    if manifest.get("status") != "ready" or not isinstance(locks, Mapping):
        raise AudioTimelineError("TTS manifest 尚未 ready 或缺少冻结参数")
    if locks.get("speed") != 1.0 or locks.get("offline_rate") != 1.0:
        raise AudioTimelineError("TTS manifest 没有锁定 speed/offline_rate=1.0")
    if not isinstance(manifest_rows, Mapping) or set(manifest_rows) != set(expected_ids):
        raise AudioTimelineError("TTS manifest 与冻结语音片段 ID 不一致")
    if int(manifest.get("ready_count") or -1) != len(expected_ids):
        raise AudioTimelineError("TTS manifest 完成数量不一致")

    verified: dict[str, dict[str, Any]] = {}
    manifest_root = manifest_path.parent.resolve()
    for segment_id in expected_ids:
        row = manifest_rows.get(segment_id)
        if not isinstance(row, Mapping) or row.get("status") != "ready":
            raise AudioTimelineError(f"TTS 片段尚未 ready：{segment_id}")
        raw_relative = str(row.get("audio_path") or "")
        if not raw_relative or Path(raw_relative).is_absolute():
            raise AudioTimelineError(f"TTS 原始音频路径无效：{segment_id}")
        raw_path = (manifest_root / raw_relative).resolve()
        if not raw_path.is_relative_to(manifest_root) or not raw_path.is_file():
            raise AudioTimelineError(f"TTS 原始音频缺失或越界：{segment_id}")
        expected_sha = str(row.get("sha256") or "")
        if len(expected_sha) != 64 or _sha256_file(raw_path) != expected_sha:
            raise AudioTimelineError(f"TTS 原始音频哈希不匹配：{segment_id}")
        if row.get("byte_size") is not None and int(row["byte_size"]) != raw_path.stat().st_size:
            raise AudioTimelineError(f"TTS 原始音频大小不匹配：{segment_id}")
        verified[segment_id] = {
            "row": dict(row),
            "path": raw_path,
            "sha256": expected_sha,
        }
    return manifest, verified


def _write_silence(stream: wave.Wave_write, frames: int) -> None:
    remaining = frames
    silence = b"\x00" * (65_536 * DEFAULT_SAMPLE_WIDTH * DEFAULT_CHANNELS)
    while remaining > 0:
        block_frames = min(remaining, 65_536)
        stream.writeframesraw(
            silence[: block_frames * DEFAULT_SAMPLE_WIDTH * DEFAULT_CHANNELS]
        )
        remaining -= block_frames


def _assemble_wav(
    *,
    destination: Path,
    plan: Mapping[str, Any],
    normalized: Mapping[str, Mapping[str, Any]],
    root: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".incoming.wav", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(DEFAULT_CHANNELS)
            output.setsampwidth(DEFAULT_SAMPLE_WIDTH)
            output.setframerate(DEFAULT_SAMPLE_RATE)
            written_frames = 0
            for segment in plan["segments"]:
                gap_frames = int(segment["preceding_gap_frames"])
                _write_silence(output, gap_frames)
                written_frames += gap_frames
                segment_id = str(segment["segment_id"])
                audio_path = _inside(
                    root,
                    str(normalized[segment_id]["path"]),
                    must_exist=True,
                    require_file=True,
                )
                with wave.open(str(audio_path), "rb") as source:
                    while True:
                        data = source.readframes(65_536)
                        if not data:
                            break
                        output.writeframesraw(data)
                written_frames += int(segment["audio_frames"])
            trailing = int(plan["trailing_gap_frames"])
            _write_silence(output, trailing)
            written_frames += trailing
            if written_frames != int(plan["target_frames"]):
                raise AudioTimelineError("中文 WAV 拼接帧数与计划不一致")
        _publish_temp_once_or_same(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _build_srt(segments: Sequence[Mapping[str, Any]]) -> bytes:
    blocks: list[str] = []
    previous_end_ms = 0
    for index, row in enumerate(segments, start=1):
        start_seconds = float(row["target_start"])
        end_seconds = float(row["target_end"])
        start_ms = round(start_seconds * 1000)
        end_ms = max(start_ms + 1, round(end_seconds * 1000))
        if start_ms < previous_end_ms:
            raise AudioTimelineError("字幕毫秒时轴因舍入产生重叠")
        previous_end_ms = end_ms
        text = str(row.get("subtitle_zh") or "").strip().replace("\r\n", "\n")
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{_srt_timestamp(start_ms / 1000)} --> {_srt_timestamp(end_ms / 1000)}",
                    text,
                ]
            )
        )
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def _cached_result(
    *, root: Path, timeline_path: Path, timeline: Mapping[str, Any], fingerprint: str
) -> dict[str, Any]:
    if timeline.get("status") != "pass" or timeline.get("input_fingerprint") != fingerprint:
        raise AudioTimelineError("现有中文时间轴与当前输入不匹配，请使用新的版本号")
    outputs = timeline.get("outputs")
    if not isinstance(outputs, Mapping):
        raise AudioTimelineError("现有中文时间轴缺少输出绑定")
    for key in ("chinese_audio", "subtitle_srt"):
        _validate_binding(root, outputs.get(key), label=f"中文时间轴 {key}")
    retime = timeline.get("retime_segments")
    source = timeline.get("working_master")
    if not isinstance(retime, list) or not isinstance(source, Mapping):
        raise AudioTimelineError("现有中文时间轴缺少重定时或媒体元数据")
    return {
        "status": "pass",
        "cached": True,
        "chinese_audio_path": str(outputs["chinese_audio"]["path"]),
        "subtitle_path": str(outputs["subtitle_srt"]["path"]),
        "chinese_timeline_path": _relative(root, timeline_path),
        "chinese_timeline_sha256": _sha256_file(timeline_path),
        "retime_segments": retime,
        "source_duration": float(source["duration"]),
        "chinese_audio_duration": float(timeline["target_duration"]),
        "frame_width": int(source["width"]),
        "frame_height": int(source["height"]),
        "frame_rate": source["frame_rate"],
    }


def build_audio_timeline(
    *,
    project_root: Path | str,
    segments_path: Path | str,
    tts_manifest_path: Path | str,
    working_master_path: Path | str,
    ad_edit_gate_path: Path | str,
    output_dir: Path | str,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    version: int = 1,
    command_runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Create the render-ready Chinese WAV, SRT, timeline, and retime map."""

    root = _root_path(project_root)
    if int(version) < 1:
        raise AudioTimelineError("中文时间轴版本号必须大于 0")
    segments_file = _inside(root, segments_path, must_exist=True, require_file=True)
    manifest_file = _inside(root, tts_manifest_path, must_exist=True, require_file=True)
    working_master = _inside(
        root, working_master_path, must_exist=True, require_file=True
    )
    ad_gate_file = _inside(root, ad_edit_gate_path, must_exist=True, require_file=True)
    output = _inside(root, output_dir)
    ad_gate = _read_object(ad_gate_file, "广告门禁")
    if ad_gate.get("status") != "pass":
        raise AudioTimelineError("ad_edit_gate 尚未通过")
    _validate_binding(
        root,
        ad_gate.get("working_master"),
        label="广告门禁中的正式工作母版",
        expected_path=working_master,
    )
    mapping: dict[str, Any] | None = None
    mapping_path: Path | None = None
    if ad_gate.get("timeline_mapping") is not None:
        mapping_path = _validate_binding(
            root, ad_gate.get("timeline_mapping"), label="原视频到工作母版映射"
        )
        mapping = _read_object(mapping_path, "原视频到工作母版映射")
        if mapping.get("status") != "pass" or mapping.get("mapping") != "source_to_working_master":
            raise AudioTimelineError("原视频到工作母版映射状态无效")

    segment_value = _read_json_value(segments_file, "冻结 TTS segments")
    segment_rows, segment_document = _extract_rows(
        segment_value, label="冻结 TTS segments"
    )
    expected_ids = [
        str(row.get("segment_id") or row.get("id") or "").strip()
        for row in segment_rows
    ]
    manifest, verified_audio = _validate_tts_manifest(
        root=root, manifest_path=manifest_file, expected_ids=expected_ids
    )

    input_bindings: dict[str, Any] = {
        "segments": _artifact(root, segments_file),
        "tts_manifest": _artifact(root, manifest_file),
        "working_master": _artifact(root, working_master),
        "ad_edit_gate": _artifact(root, ad_gate_file),
    }
    if mapping_path is not None:
        input_bindings["source_to_working_timeline"] = _artifact(root, mapping_path)
    fingerprint = _canonical_sha256(
        {
            "inputs": input_bindings,
            "version": int(version),
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "channels": DEFAULT_CHANNELS,
            "sample_width": DEFAULT_SAMPLE_WIDTH,
            "policy": "native_speed_video_retime_only_v1",
        }
    )
    timeline_path = output / f"chinese_timeline_v{int(version)}.json"
    if timeline_path.exists():
        return _cached_result(
            root=root,
            timeline_path=timeline_path,
            timeline=_read_object(timeline_path, "中文时间轴"),
            fingerprint=fingerprint,
        )

    media = _probe_working_master(
        working_master, ffprobe=ffprobe, runner=command_runner
    )
    if mapping is not None:
        mapped_duration = _finite(
            mapping.get("working_duration_seconds"), "映射中的工作母版时长"
        )
        if abs(mapped_duration - float(media["duration"])) > 0.25:
            raise AudioTimelineError("广告映射时长与正式工作母版 ffprobe 时长不一致")
    normalized_segments, input_time_basis = _normalise_timeline_segments(
        segment_rows,
        document=segment_document,
        ad_gate=ad_gate,
        mapping=mapping,
        working_duration=float(media["duration"]),
    )

    normalized_dir = output / "normalized"
    normalized_audio: dict[str, dict[str, Any]] = {}
    frames: dict[str, int] = {}
    for row in normalized_segments:
        segment_id = str(row["segment_id"])
        source = verified_audio[segment_id]
        metadata = _normalise_audio(
            root=root,
            raw_path=source["path"],
            raw_sha256=str(source["sha256"]),
            segment_id=segment_id,
            normalized_dir=normalized_dir,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            runner=command_runner,
        )
        normalized_audio[segment_id] = metadata
        frames[segment_id] = int(metadata["frames"])

    plan = plan_audio_timeline(
        normalized_segments,
        frames,
        working_duration=float(media["duration"]),
        sample_rate=DEFAULT_SAMPLE_RATE,
    )
    audio_path = output / f"chinese_voice_v{int(version)}.wav"
    subtitle_path = output / f"subtitles_zh_v{int(version)}.srt"
    _assemble_wav(
        destination=audio_path, plan=plan, normalized=normalized_audio, root=root
    )
    final_audio = _probe_pcm(audio_path, ffprobe=ffprobe, runner=command_runner)
    if int(final_audio["frames"]) != int(plan["target_frames"]):
        raise AudioTimelineError("最终中文 WAV 帧数与时间轴计划不一致")
    _write_bytes_once_or_same(subtitle_path, _build_srt(plan["segments"]))

    timeline = {
        "schema_version": 1,
        "status": "pass",
        "version": int(version),
        "input_fingerprint": fingerprint,
        "paths_are_relative_to_project": True,
        "inputs": input_bindings,
        "tts_manifest_locks": dict(manifest.get("locks") or {}),
        "input_time_basis": input_time_basis,
        "timeline_basis": "formal_working_master",
        "working_master": media,
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "channels": DEFAULT_CHANNELS,
        "sample_width": DEFAULT_SAMPLE_WIDTH,
        "tts_native_speed": 1.0,
        "offline_rate": 1.0,
        "audio_time_stretch": False,
        "tempo_filters": [],
        "voiced_content_truncated": False,
        "dialogue_free_gaps_preserved_at_original_duration": True,
        "subtitle_timeline_rebuilt": True,
        "subtitle_granularity": "frozen_tts_segment",
        "segment_count": len(plan["segments"]),
        "subtitle_count": len(plan["segments"]),
        "source_duration": plan["source_duration"],
        "target_frames": plan["target_frames"],
        "target_duration": plan["target_duration"],
        "overlap_count": 0,
        "source_coverage_complete": True,
        "source_gap_count": 0,
        "source_overlap_count": 0,
        "target_gap_count": 0,
        "target_overlap_count": 0,
        "segments": [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"target_start_frame", "target_end_frame"}
                },
                "raw_audio": {
                    "path": _relative(root, verified_audio[str(row["segment_id"])]["path"]),
                    "sha256": verified_audio[str(row["segment_id"])]["sha256"],
                },
                "normalized_audio": {
                    "path": normalized_audio[str(row["segment_id"])]["path"],
                    "sha256": normalized_audio[str(row["segment_id"])]["output_sha256"],
                    "frames": normalized_audio[str(row["segment_id"])]["frames"],
                },
            }
            for row in plan["segments"]
        ],
        "retime_segments": plan["retime_segments"],
        "outputs": {
            "chinese_audio": {
                **_artifact(root, audio_path),
                "duration": final_audio["duration"],
                "frames": final_audio["frames"],
            },
            "subtitle_srt": {
                **_artifact(root, subtitle_path),
                "cue_count": len(plan["segments"]),
                "encoding": "utf-8",
            },
        },
    }
    _write_json_once_or_same(timeline_path, timeline)
    return {
        "status": "pass",
        "cached": False,
        "chinese_audio_path": _relative(root, audio_path),
        "subtitle_path": _relative(root, subtitle_path),
        "chinese_timeline_path": _relative(root, timeline_path),
        "chinese_timeline_sha256": _sha256_file(timeline_path),
        "retime_segments": plan["retime_segments"],
        "source_duration": float(media["duration"]),
        "chinese_audio_duration": float(plan["target_duration"]),
        "frame_width": int(media["width"]),
        "frame_height": int(media["height"]),
        "frame_rate": media["frame_rate"],
    }


__all__ = [
    "AudioTimelineError",
    "build_audio_timeline",
    "build_tts_segments",
    "plan_audio_timeline",
]
