"""Deterministic video ingest, source-text freezing and advertisement gates.

This module deliberately separates *planning* from long-running media work.  It
can inspect already captured ``ffprobe`` JSON, parse subtitle files, validate
advertisement decisions and produce auditable command/artifact plans.  Callers
may execute the command plans with the event-driven worker, but no model or
paid API is invoked here.

All filesystem-facing helpers can be bound to a project root.  Bound paths are
resolved before use so ``..`` components and symlinks cannot escape the run
directory.  Source video files are inputs only; extraction commands use
FFmpeg's ``-n`` flag and the advertisement bundle refuses a working master
that aliases the original master.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any


class IngestError(RuntimeError):
    """Base class for deterministic ingest failures."""


class PathSafetyError(IngestError):
    """Raised when a path can escape a project or overwrite a source."""


class MediaProbeError(IngestError):
    """Raised when media metadata is missing or structurally invalid."""


class TranscriptError(IngestError):
    """Raised when a source transcript cannot be normalized safely."""


class AdEvidenceError(IngestError):
    """Raised when an advertisement decision is not supported by evidence."""


TIMESTAMP_PATTERN = re.compile(
    r"^(?:(?P<hours>\d{1,3}):)?(?P<minutes>\d{1,2}):"
    r"(?P<seconds>\d{1,2})(?P<fraction>[.,]\d{1,3})?$"
)
TIME_RANGE_PATTERN = re.compile(r"(?P<start>\S+)\s+-->\s+(?P<end>\S+)")
STABLE_ID_PATTERN = re.compile(r"^S\d{6}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SEMANTIC_EVIDENCE = {"asr_semantic", "chapter", "description"}
BOUNDARY_EVIDENCE = {"pause", "transition", "music_change", "context_before_after"}
VISUAL_EVIDENCE = {"frame", "screenshot", "ocr", "qr_detector", "logo_detector"}
EVIDENCE_KINDS = SEMANTIC_EVIDENCE | BOUNDARY_EVIDENCE | VISUAL_EVIDENCE | {
    "program_identity",
    "manual_observation",
}
AD_KINDS = {"content", "visual"}
AD_ACTIONS = {"remove", "mask", "keep"}
MASK_MODES = {"blur", "solid", "background_patch", "graphic"}


def _finite_number(value: object, *, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"{label} 必须是数字") from exc
    if not math.isfinite(number):
        raise IngestError(f"{label} 必须是有限数字")
    return number


def _round_time(value: float) -> float:
    # Six decimals is finer than common video time bases while producing
    # stable JSON on all supported platforms.
    return round(float(value), 6)


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    """Return the exact UTF-8 representation used for artifact hash binding."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _root_path(project_root: Path | str) -> Path:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise PathSafetyError(f"项目目录不存在: {root}")
    return root


def safe_project_path(
    project_root: Path | str,
    value: Path | str,
    *,
    must_exist: bool = False,
    require_file: bool = False,
) -> Path:
    """Resolve ``value`` below ``project_root`` and reject traversal/symlinks."""

    root = _root_path(project_root)
    candidate = Path(value).expanduser()
    resolved = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (root / candidate).resolve(strict=False)
    )
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"路径越出项目目录: {value}") from exc
    if must_exist and not resolved.exists():
        raise PathSafetyError(f"文件不存在: {resolved}")
    if require_file and resolved.exists() and not resolved.is_file():
        raise PathSafetyError(f"路径不是文件: {resolved}")
    return resolved


def _portable_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _same_file_or_path(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


@dataclass(frozen=True)
class CommandPlan:
    """Serializable, shell-free command plan for the local event runner."""

    kind: str
    argv: tuple[str, ...]
    cwd: str
    input_path: str
    output_path: str | None = None
    overwrite: bool = False
    long_running: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["argv"] = list(self.argv)
        payload["shell"] = False
        return payload


def build_ffprobe_plan(
    source_path: Path | str,
    *,
    project_root: Path | str,
    ffprobe_binary: str = "ffprobe",
) -> CommandPlan:
    """Build a portable ffprobe plan without executing a process."""

    root = _root_path(project_root)
    source = safe_project_path(root, source_path, must_exist=True, require_file=True)
    relative = _portable_path(root, source)
    argv = (
        str(ffprobe_binary),
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,size,format_name,bit_rate:"
            "stream=index,codec_type,codec_name,width,height,r_frame_rate,"
            "duration,sample_rate,channels:stream_tags=language,title"
        ),
        "-of",
        "json",
        "-i",
        relative,
    )
    return CommandPlan(
        kind="media.ffprobe",
        argv=argv,
        cwd=str(root),
        input_path=relative,
    )


def _frame_rate(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw or raw in {"0/0", "N/A"}:
        return None
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        den = _finite_number(denominator, label="帧率分母")
        if den == 0:
            return None
        return _round_time(_finite_number(numerator, label="帧率分子") / den)
    return _round_time(_finite_number(raw, label="帧率"))


def parse_ffprobe_payload(
    payload: Mapping[str, Any] | str | bytes,
    *,
    require_audio: bool = True,
) -> dict[str, Any]:
    """Validate ffprobe JSON and return a compact, stable media report."""

    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MediaProbeError("ffprobe 输出不是有效 JSON") from exc
    if not isinstance(payload, Mapping):
        raise MediaProbeError("ffprobe 输出必须是 JSON 对象")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaProbeError("ffprobe 输出缺少 streams")
    video_streams = [row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "video"]
    audio_streams = [row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "audio"]
    subtitle_streams = [row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "subtitle"]
    if not video_streams:
        raise MediaProbeError("媒体没有视频流")
    if require_audio and not audio_streams:
        raise MediaProbeError("媒体没有音频流")

    video = video_streams[0]
    width = int(_finite_number(video.get("width"), label="视频宽度"))
    height = int(_finite_number(video.get("height"), label="视频高度"))
    if width <= 0 or height <= 0:
        raise MediaProbeError("视频尺寸必须大于 0")
    format_payload = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
    duration_value = format_payload.get("duration")
    if duration_value in (None, "", "N/A"):
        duration_value = max(
            (
                _finite_number(row.get("duration"), label="流时长")
                for row in video_streams + audio_streams
                if row.get("duration") not in (None, "", "N/A")
            ),
            default=0.0,
        )
    duration = _finite_number(duration_value, label="媒体时长")
    if duration <= 0:
        raise MediaProbeError("媒体时长必须大于 0")

    def tags(row: Mapping[str, Any]) -> Mapping[str, Any]:
        value = row.get("tags")
        return value if isinstance(value, Mapping) else {}

    def optional_int(value: object, *, label: str) -> int:
        if value in (None, "", "N/A"):
            return 0
        return int(_finite_number(value, label=label))

    return {
        "schema_version": 1,
        "status": "pass",
        "duration_seconds": _round_time(duration),
        "byte_size": optional_int(format_payload.get("size"), label="文件大小"),
        "format_name": str(format_payload.get("format_name") or ""),
        "video": {
            "stream_index": int(video.get("index", 0)),
            "codec": str(video.get("codec_name") or ""),
            "width": width,
            "height": height,
            "frame_rate": _frame_rate(video.get("r_frame_rate")),
        },
        "audio_streams": [
            {
                "stream_index": int(row.get("index", 0)),
                "codec": str(row.get("codec_name") or ""),
                "sample_rate": optional_int(row.get("sample_rate"), label="采样率"),
                "channels": optional_int(row.get("channels"), label="声道数"),
                "language": str(tags(row).get("language") or ""),
                "title": str(tags(row).get("title") or ""),
            }
            for row in audio_streams
        ],
        "subtitle_streams": [
            {
                "stream_index": int(row.get("index", 0)),
                "codec": str(row.get("codec_name") or ""),
                "language": str(tags(row).get("language") or ""),
                "title": str(tags(row).get("title") or ""),
            }
            for row in subtitle_streams
        ],
    }


def probe_media(
    source_path: Path | str,
    *,
    project_root: Path | str,
    ffprobe_binary: str = "ffprobe",
    timeout_seconds: float = 60.0,
    require_audio: bool = True,
) -> dict[str, Any]:
    """Execute the bounded ffprobe plan and validate its JSON output."""

    plan = build_ffprobe_plan(
        source_path, project_root=project_root, ffprobe_binary=ffprobe_binary
    )
    try:
        completed = subprocess.run(
            plan.argv,
            cwd=plan.cwd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaProbeError(f"ffprobe 执行失败: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        # Do not echo complete process output: it can include machine paths.
        raise MediaProbeError(f"ffprobe 返回非零退出码 {completed.returncode}")
    report = parse_ffprobe_payload(completed.stdout, require_audio=require_audio)
    root = _root_path(project_root)
    source = safe_project_path(root, source_path, must_exist=True, require_file=True)
    report["source"] = {
        "path": _portable_path(root, source),
        "sha256": sha256_file(source),
    }
    return report


def build_embedded_subtitle_plan(
    source_path: Path | str,
    output_path: Path | str,
    *,
    stream_index: int,
    project_root: Path | str,
    ffmpeg_binary: str = "ffmpeg",
    allow_existing: bool = False,
) -> CommandPlan:
    """Plan extraction of one embedded subtitle stream without overwriting."""

    root = _root_path(project_root)
    source = safe_project_path(root, source_path, must_exist=True, require_file=True)
    output = safe_project_path(root, output_path)
    if int(stream_index) < 0:
        raise MediaProbeError("字幕流索引必须大于或等于 0")
    if output.exists() and not allow_existing:
        raise PathSafetyError(f"字幕输出已存在，必须创建新版本: {output}")
    if _same_file_or_path(source, output):
        raise PathSafetyError("字幕输出不能覆盖源母版")
    extension = output.suffix.lower()
    codec_by_extension = {".srt": "srt", ".vtt": "webvtt"}
    if extension not in codec_by_extension:
        raise MediaProbeError("内嵌字幕输出只支持 .srt 或 .vtt")
    source_rel = _portable_path(root, source)
    output_rel = _portable_path(root, output)
    argv = (
        str(ffmpeg_binary),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-n",
        "-i",
        source_rel,
        "-map",
        f"0:{int(stream_index)}",
        "-c:s",
        codec_by_extension[extension],
        output_rel,
    )
    return CommandPlan(
        kind="media.extract_embedded_subtitle",
        argv=argv,
        cwd=str(root),
        input_path=source_rel,
        output_path=output_rel,
        overwrite=False,
        long_running=False,
    )


def parse_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite_number(value, label="时间")
        if number < 0:
            raise TranscriptError("时间不能为负数")
        return _round_time(number)
    raw = str(value or "").strip()
    match = TIMESTAMP_PATTERN.fullmatch(raw)
    if not match:
        raise TranscriptError(f"无法解析时间: {raw!r}")
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if minutes >= 60 or seconds >= 60:
        raise TranscriptError(f"时间字段越界: {raw!r}")
    fraction = match.group("fraction")
    milliseconds = int((fraction[1:] if fraction else "0").ljust(3, "0"))
    return _round_time(hours * 3600 + minutes * 60 + seconds + milliseconds / 1000)


def _visible_text(lines: Iterable[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    return re.sub(r"\s+", " ", text).strip()


def _parse_timed_text(value: str, *, webvtt: bool) -> list[dict[str, Any]]:
    value = value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", value.strip())
    cues: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n")]
        if not lines:
            continue
        first = lines[0].strip()
        if webvtt and (first == "WEBVTT" or first.startswith(("NOTE", "STYLE", "REGION"))):
            continue
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            # WEBVTT header metadata can be a block of its own.
            continue
        match = TIME_RANGE_PATTERN.search(lines[timing_index])
        if not match:
            raise TranscriptError(f"字幕时间行无效: {lines[timing_index]!r}")
        text = _visible_text(lines[timing_index + 1 :])
        cues.append(
            {
                "source_id": first if timing_index > 0 else None,
                "start": parse_timestamp(match.group("start")),
                "end": parse_timestamp(match.group("end")),
                "text": text,
            }
        )
    if not cues:
        raise TranscriptError("字幕文件没有可用时间槽")
    return cues


def _json_cues(payload: object) -> list[dict[str, Any]]:
    rows: object = payload
    if isinstance(payload, Mapping):
        for key in ("slots", "segments", "cues", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list) or not rows:
        raise TranscriptError("JSON 源文必须包含非空 slots/segments/cues/items 数组")
    cues: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise TranscriptError(f"JSON 第 {index} 项不是对象")
        if row.get("start_ms") is not None:
            start = _finite_number(row["start_ms"], label=f"第 {index} 项 start_ms") / 1000
        else:
            start = row.get("start", row.get("start_time", row.get("begin")))
        if row.get("end_ms") is not None:
            end = _finite_number(row["end_ms"], label=f"第 {index} 项 end_ms") / 1000
        else:
            end = row.get("end", row.get("end_time", row.get("finish")))
        if end is None and row.get("duration") is not None:
            end = parse_timestamp(start) + _finite_number(
                row["duration"], label=f"第 {index} 项 duration"
            )
        text = row.get("source_text", row.get("source", row.get("text", row.get("content"))))
        cues.append(
            {
                "source_id": row.get("id"),
                "start": parse_timestamp(start),
                "end": parse_timestamp(end),
                "text": str(text or ""),
                "speaker": row.get("speaker", row.get("role_id")),
            }
        )
    return cues


def normalize_source_cues(
    cues: Sequence[Mapping[str, Any]],
    *,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Assign deterministic continuous IDs and validate source time order."""

    if not cues:
        raise TranscriptError("冻结源文不能为空")
    normalized: list[dict[str, Any]] = []
    previous_start = -1.0
    for ordinal, cue in enumerate(cues, start=1):
        if not isinstance(cue, Mapping):
            raise TranscriptError(f"第 {ordinal} 个时间槽结构无效")
        start = parse_timestamp(cue.get("start"))
        end = parse_timestamp(cue.get("end"))
        if end <= start:
            raise TranscriptError(f"第 {ordinal} 个时间槽结束时间必须晚于开始时间")
        if start < previous_start:
            raise TranscriptError(f"第 {ordinal} 个时间槽开始时间不单调")
        text = _visible_text(str(cue.get("text", cue.get("source_text", ""))).splitlines())
        if not text:
            raise TranscriptError(f"第 {ordinal} 个时间槽源文为空")
        row: dict[str, Any] = {
            "id": f"S{ordinal:06d}",
            "source_text": text,
            "start": _round_time(start),
            "end": _round_time(end),
            "position": ordinal - 1,
        }
        source_id = str(cue.get("source_id") or cue.get("id") or "").strip()
        if source_id:
            row["imported_id"] = source_id
        speaker = cue.get("speaker") or cue.get("role_id")
        if speaker:
            row["speaker"] = str(speaker)
        if language:
            row["source_language"] = str(language)
        normalized.append(row)
        previous_start = start
    if not all(STABLE_ID_PATTERN.fullmatch(row["id"]) for row in normalized):
        raise AssertionError("internal stable ID invariant failed")
    return normalized


def import_source_transcript(
    source_path: Path | str,
    *,
    project_root: Path | str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Import SRT, VTT or JSON and return a frozen-source document in memory."""

    if project_root is None:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise PathSafetyError(f"源文文件不存在: {source}")
        portable = source.name
    else:
        root = _root_path(project_root)
        source = safe_project_path(root, source_path, must_exist=True, require_file=True)
        portable = _portable_path(root, source)
    extension = source.suffix.lower()
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TranscriptError(f"无法读取源文文件: {type(exc).__name__}") from exc
    if extension == ".srt":
        cues = _parse_timed_text(text, webvtt=False)
        source_format = "srt"
    elif extension == ".vtt":
        cues = _parse_timed_text(text, webvtt=True)
        source_format = "vtt"
    elif extension == ".json":
        try:
            cues = _json_cues(json.loads(text))
        except json.JSONDecodeError as exc:
            raise TranscriptError("JSON 源文格式无效") from exc
        source_format = "json"
    else:
        raise TranscriptError("源文导入只支持 .srt、.vtt 或 .json")
    slots = normalize_source_cues(cues, language=language)
    return {
        "schema_version": 1,
        "status": "pass",
        "kind": "frozen_source_transcript",
        "source_format": source_format,
        "source_file": {"path": portable, "sha256": sha256_file(source)},
        "source_language": language,
        "stable_id_scheme": "S{ordinal:06d}",
        "slot_count": len(slots),
        "stable_ids": [row["id"] for row in slots],
        "slots": slots,
    }


def write_frozen_source(
    document: Mapping[str, Any],
    output_path: Path | str,
    *,
    project_root: Path | str,
) -> dict[str, str]:
    """Write a new frozen source JSON version; existing files are immutable."""

    root = _root_path(project_root)
    output = safe_project_path(root, output_path)
    if output.suffix.lower() != ".json":
        raise TranscriptError("冻结源文必须写为 .json")
    data = canonical_json_bytes(document)
    _write_new_file(output, data)
    return {"path": _portable_path(root, output), "sha256": hashlib.sha256(data).hexdigest()}


@dataclass(frozen=True)
class ValidationResult:
    status: str
    errors: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    normalized: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pass" and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "errors": list(self.errors),
            "blockers": list(self.blockers),
            "normalized": dict(self.normalized) if self.normalized is not None else None,
        }


def _evidence_rows(value: object, *, candidate_id: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors.append(f"{candidate_id}: evidence 必须是数组")
        return []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            errors.append(f"{candidate_id}: 第 {index} 条证据不是对象")
            continue
        kind = str(raw.get("kind") or "")
        summary = str(raw.get("summary") or "").strip()
        if kind not in EVIDENCE_KINDS:
            errors.append(f"{candidate_id}: 不支持的证据类型 {kind!r}")
        if not summary:
            errors.append(f"{candidate_id}: 第 {index} 条证据缺少摘要")
        row: dict[str, Any] = {"kind": kind, "summary": summary}
        if raw.get("reference"):
            row["reference"] = str(raw["reference"])
        if raw.get("at_seconds") is not None:
            try:
                at = _finite_number(raw["at_seconds"], label="证据时间")
                if at < 0:
                    raise IngestError("证据时间不能为负数")
                row["at_seconds"] = _round_time(at)
            except IngestError as exc:
                errors.append(f"{candidate_id}: {exc}")
        if raw.get("sha256") is not None:
            sha = str(raw["sha256"])
            if not SHA256_PATTERN.fullmatch(sha):
                errors.append(f"{candidate_id}: 证据 SHA-256 无效")
            row["sha256"] = sha
        rows.append(row)
    return rows


def _normalized_region(
    value: object,
    *,
    candidate_id: str,
    frame_width: int | None,
    frame_height: int | None,
    errors: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{candidate_id}: mask 必须包含 region")
        return None
    normalized = value.get("normalized")
    pixels = value.get("pixels")
    width = int(value.get("frame_width") or frame_width or 0)
    height = int(value.get("frame_height") or frame_height or 0)
    if width <= 0 or height <= 0:
        errors.append(f"{candidate_id}: region 缺少有效画面尺寸")
        return None
    if not isinstance(normalized, Mapping) or not isinstance(pixels, Mapping):
        errors.append(f"{candidate_id}: region 必须同时记录 normalized 和 pixels 坐标")
        return None
    try:
        norm = {key: _finite_number(normalized.get(key), label=key) for key in ("x", "y", "width", "height")}
        pix = {key: int(_finite_number(pixels.get(key), label=key)) for key in ("x", "y", "width", "height")}
        margin = int(_finite_number(value.get("safe_margin_pixels", 0), label="safe_margin_pixels"))
    except IngestError as exc:
        errors.append(f"{candidate_id}: {exc}")
        return None
    if norm["x"] < 0 or norm["y"] < 0 or norm["width"] <= 0 or norm["height"] <= 0:
        errors.append(f"{candidate_id}: 归一化坐标必须为正矩形")
    if norm["x"] + norm["width"] > 1.000001 or norm["y"] + norm["height"] > 1.000001:
        errors.append(f"{candidate_id}: 归一化坐标越出画面")
    if pix["x"] < 0 or pix["y"] < 0 or pix["width"] <= 0 or pix["height"] <= 0:
        errors.append(f"{candidate_id}: 像素坐标必须为正矩形")
    if pix["x"] + pix["width"] > width or pix["y"] + pix["height"] > height:
        errors.append(f"{candidate_id}: 像素坐标越出画面")
    if margin < 0:
        errors.append(f"{candidate_id}: 安全边距不能为负数")
    expected = {
        "x": norm["x"] * width,
        "y": norm["y"] * height,
        "width": norm["width"] * width,
        "height": norm["height"] * height,
    }
    if any(abs(pix[key] - expected[key]) > 2.0 for key in expected):
        errors.append(f"{candidate_id}: 像素坐标与归一化坐标不一致")
    mode = str(value.get("mode") or "blur")
    if mode not in MASK_MODES:
        errors.append(f"{candidate_id}: 不支持的遮盖方式 {mode!r}")
    return {
        "frame_width": width,
        "frame_height": height,
        "normalized": {key: round(number, 8) for key, number in norm.items()},
        "pixels": pix,
        "safe_margin_pixels": margin,
        "mode": mode,
    }


def validate_ad_analysis(
    report: Mapping[str, Any],
    *,
    source_duration: float,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> ValidationResult:
    """Validate evidence and normalize final remove/mask/keep decisions.

    ``analysis_required`` is an intentional non-error state.  It is returned
    whenever scans are incomplete or semantic analysis was declared necessary
    but not completed; callers must not manufacture ``no_ads_detected`` in that
    state.
    """

    duration = _finite_number(source_duration, label="源视频时长")
    if duration <= 0:
        raise AdEvidenceError("源视频时长必须大于 0")
    content_complete = report.get("content_scan_complete") is True
    visual_complete = report.get("visual_scan_complete") is True
    semantic_required = report.get("semantic_analysis_required", True) is not False
    semantic_complete = report.get("semantic_analysis_complete") is True
    declared_status = str(report.get("status") or "analysis_required")
    blockers: list[str] = []
    if not content_complete:
        blockers.append("content_ad_scan_incomplete")
    if not visual_complete:
        blockers.append("visual_ad_scan_incomplete")
    if semantic_required and not semantic_complete:
        blockers.append("semantic_analysis_required")
    if declared_status == "analysis_required" or blockers:
        normalized = {
            "schema_version": 1,
            "status": "analysis_required",
            "content_scan_complete": content_complete,
            "visual_scan_complete": visual_complete,
            "semantic_analysis_required": semantic_required,
            "semantic_analysis_complete": semantic_complete,
            "requirements": sorted(set(blockers or ["semantic_analysis_required"])),
            "candidates": list(report.get("candidates") or []),
        }
        return ValidationResult(
            "analysis_required",
            blockers=tuple(normalized["requirements"]),
            normalized=normalized,
        )
    if declared_status != "pass":
        return ValidationResult("fail", errors=("广告分析 status 必须是 pass 或 analysis_required",))

    errors: list[str] = []
    raw_candidates = report.get("candidates")
    if not isinstance(raw_candidates, list):
        return ValidationResult("fail", errors=("candidates 必须是数组",))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, Mapping):
            errors.append(f"第 {index} 个广告候选不是对象")
            continue
        candidate_id = str(raw.get("id") or f"AD{index:03d}").strip()
        if candidate_id in seen:
            errors.append(f"广告候选 ID 重复: {candidate_id}")
        seen.add(candidate_id)
        kind = str(raw.get("kind") or "")
        action = str(raw.get("action") or "")
        rationale = str(raw.get("rationale") or "").strip()
        try:
            start = _finite_number(raw.get("start"), label="开始时间")
            end = _finite_number(raw.get("end"), label="结束时间")
            confidence = _finite_number(raw.get("confidence"), label="置信度")
        except IngestError as exc:
            errors.append(f"{candidate_id}: {exc}")
            continue
        if kind not in AD_KINDS:
            errors.append(f"{candidate_id}: kind 必须是 content 或 visual")
        if action not in AD_ACTIONS:
            errors.append(f"{candidate_id}: action 必须是 remove、mask 或 keep")
        if start < 0 or end <= start or end > duration + 0.000001:
            errors.append(f"{candidate_id}: 时间区间无效或越出源视频")
        if not 0 <= confidence <= 1:
            errors.append(f"{candidate_id}: confidence 必须在 0 到 1 之间")
        if not rationale:
            errors.append(f"{candidate_id}: 缺少处置理由")
        evidence = _evidence_rows(raw.get("evidence"), candidate_id=candidate_id, errors=errors)
        evidence_kinds = {row["kind"] for row in evidence}
        region = None
        if not evidence:
            errors.append(f"{candidate_id}: 至少需要一条可追溯证据")
        if action == "remove":
            if kind != "content":
                errors.append(f"{candidate_id}: 只有内容广告可以整段删除")
            if not evidence_kinds.intersection(SEMANTIC_EVIDENCE):
                errors.append(f"{candidate_id}: remove 缺少语义证据")
            if not evidence_kinds.intersection(BOUNDARY_EVIDENCE):
                errors.append(f"{candidate_id}: remove 缺少自然边界证据")
            if raw.get("boundary_safe") is not True:
                errors.append(f"{candidate_id}: remove 必须确认 boundary_safe=true")
            try:
                before = _finite_number(
                    raw.get("context_before_seconds", 0), label="context_before_seconds"
                )
                after = _finite_number(
                    raw.get("context_after_seconds", 0), label="context_after_seconds"
                )
            except IngestError as exc:
                errors.append(f"{candidate_id}: {exc}")
                before = 0.0
                after = 0.0
            if before < 10 or after < 10:
                errors.append(f"{candidate_id}: remove 必须核对候选前后各至少 10 秒")
        elif action == "mask":
            if kind != "visual":
                errors.append(f"{candidate_id}: 只有画面广告可以局部遮盖")
            if not evidence_kinds.intersection(VISUAL_EVIDENCE):
                errors.append(f"{candidate_id}: mask 缺少画面/OCR 证据")
            region = _normalized_region(
                raw.get("region"),
                candidate_id=candidate_id,
                frame_width=frame_width,
                frame_height=frame_height,
                errors=errors,
            )
            if raw.get("program_content_clear") is not True:
                errors.append(f"{candidate_id}: mask 必须确认未遮挡人物或正文信息")
        row: dict[str, Any] = {
            "id": candidate_id,
            "kind": kind,
            "start": _round_time(start),
            "end": _round_time(end),
            "action": action,
            "confidence": round(confidence, 6),
            "rationale": rationale,
            "evidence": evidence,
        }
        if action == "remove":
            row.update(
                {
                    "boundary_safe": True,
                    "context_before_seconds": _round_time(before),
                    "context_after_seconds": _round_time(after),
                }
            )
        if region is not None:
            row["region"] = region
            row["program_content_clear"] = raw.get("program_content_clear") is True
        candidates.append(row)

    for row in candidates:
        for evidence in row["evidence"]:
            at_seconds = evidence.get("at_seconds")
            if at_seconds is not None and float(at_seconds) > duration + 0.000001:
                errors.append(f"{row['id']}: 证据时间越出源视频")

    removed = sorted((row for row in candidates if row["action"] == "remove"), key=lambda row: row["start"])
    for previous, current in pairwise(removed):
        if current["start"] < previous["end"] - 0.000001:
            errors.append(f"remove 区间重叠: {previous['id']} / {current['id']}")
    masks = [row for row in candidates if row["action"] == "mask"]
    for mask in masks:
        if any(mask["start"] < item["end"] and mask["end"] > item["start"] for item in removed):
            errors.append(f"{mask['id']}: 遮盖区间与已删除区间重叠")

    effective_actions = {row["action"] for row in candidates if row["action"] in {"remove", "mask"}}
    if not effective_actions:
        decision = "no_ads_detected"
    elif effective_actions == {"remove"}:
        decision = "ads_removed"
    elif effective_actions == {"mask"}:
        decision = "ads_overlaid"
    else:
        decision = "ads_removed_and_overlaid"
    expected_decision = report.get("decision")
    if expected_decision is not None and str(expected_decision) != decision:
        errors.append(f"decision 应为 {decision}")
    if decision == "no_ads_detected" and candidates and any(row["action"] != "keep" for row in candidates):
        errors.append("no_ads_detected 不能包含 remove 或 mask")

    normalized = {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "decision": decision,
        "detection_outcome": "no_candidates" if not candidates else ("all_candidates_kept" if not effective_actions else "actions_planned"),
        "content_scan_complete": True,
        "visual_scan_complete": True,
        "semantic_analysis_required": semantic_required,
        "semantic_analysis_complete": semantic_complete,
        "analysis_method": str(report.get("analysis_method") or "recorded_evidence"),
        "decision_rule_version": str(report.get("decision_rule_version") or "ad-evidence-v1"),
        "source_duration_seconds": _round_time(duration),
        "candidates": candidates,
        "unresolved_count": 0,
    }
    return ValidationResult("pass" if not errors else "fail", tuple(errors), normalized=normalized)


def build_source_to_edit_timeline(
    source_duration: float,
    removed_segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a complete monotonic source→working-master piecewise mapping."""

    duration = _finite_number(source_duration, label="源视频时长")
    if duration <= 0:
        raise AdEvidenceError("源视频时长必须大于 0")
    removed: list[dict[str, Any]] = []
    for index, raw in enumerate(removed_segments, start=1):
        if not isinstance(raw, Mapping):
            raise AdEvidenceError(f"第 {index} 个删除区间不是对象")
        start = _finite_number(raw.get("start"), label="删除开始时间")
        end = _finite_number(raw.get("end"), label="删除结束时间")
        if start < 0 or end <= start or end > duration + 0.000001:
            raise AdEvidenceError(f"第 {index} 个删除区间越界")
        removed.append({"id": str(raw.get("id") or f"REMOVE{index:03d}"), "start": _round_time(start), "end": _round_time(end)})
    removed.sort(key=lambda row: row["start"])
    for previous, current in pairwise(removed):
        if current["start"] < previous["end"] - 0.000001:
            raise AdEvidenceError("删除区间不能重叠")
    total_removed = sum(row["end"] - row["start"] for row in removed)
    if total_removed >= duration - 0.000001:
        raise AdEvidenceError("不能删除整个源视频")

    segments: list[dict[str, Any]] = []
    source_cursor = 0.0
    edit_cursor = 0.0
    for item in removed:
        if item["start"] > source_cursor:
            length = item["start"] - source_cursor
            segments.append(
                {
                    "kind": "keep",
                    "source_start": _round_time(source_cursor),
                    "source_end": item["start"],
                    "working_start": _round_time(edit_cursor),
                    "working_end": _round_time(edit_cursor + length),
                }
            )
            edit_cursor += length
        segments.append(
            {
                "kind": "remove",
                "candidate_id": item["id"],
                "source_start": item["start"],
                "source_end": item["end"],
                "working_anchor": _round_time(edit_cursor),
            }
        )
        source_cursor = item["end"]
    if source_cursor < duration:
        length = duration - source_cursor
        segments.append(
            {
                "kind": "keep",
                "source_start": _round_time(source_cursor),
                "source_end": _round_time(duration),
                "working_start": _round_time(edit_cursor),
                "working_end": _round_time(edit_cursor + length),
            }
        )
        edit_cursor += length
    return {
        "schema_version": 1,
        "status": "pass",
        "mapping": "source_to_working_master",
        "source_duration_seconds": _round_time(duration),
        "working_duration_seconds": _round_time(edit_cursor),
        "removed_duration_seconds": _round_time(total_removed),
        "identity": not removed,
        "continuous": True,
        "monotonic": True,
        "segments": segments,
    }


def map_source_time(timeline: Mapping[str, Any], source_seconds: float) -> float:
    """Map a source time; removed points collapse to their working anchor."""

    time_value = _finite_number(source_seconds, label="源时间")
    duration = _finite_number(timeline.get("source_duration_seconds"), label="源视频时长")
    if time_value < 0 or time_value > duration + 0.000001:
        raise AdEvidenceError("源时间越出映射范围")
    segments = timeline.get("segments")
    if not isinstance(segments, list):
        raise AdEvidenceError("时间映射缺少 segments")
    if abs(time_value - duration) <= 0.000001:
        return _round_time(_finite_number(timeline.get("working_duration_seconds"), label="工作母版时长"))
    for row in segments:
        if not isinstance(row, Mapping):
            continue
        start = float(row.get("source_start", -1))
        end = float(row.get("source_end", -1))
        if start <= time_value < end or abs(time_value - start) <= 0.000001:
            if row.get("kind") == "remove":
                return _round_time(float(row["working_anchor"]))
            return _round_time(float(row["working_start"]) + time_value - start)
    raise AdEvidenceError("时间映射没有覆盖指定源时间")


def _artifact(root: Path, path: Path) -> dict[str, str]:
    safe = safe_project_path(root, path, must_exist=True, require_file=True)
    return {"path": _portable_path(root, safe), "sha256": sha256_file(safe)}


def _planned_artifact(path: str, payload: object) -> dict[str, str]:
    return {"path": path, "sha256": canonical_sha256(payload)}


def build_ad_edit_artifacts(
    *,
    project_root: Path | str,
    source_master: Path | str,
    working_master: Path | str,
    analysis: Mapping[str, Any],
    source_duration: float,
    frame_width: int | None = None,
    frame_height: int | None = None,
    frozen_source_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build all deterministic advertisement QA documents and a pass gate.

    No files are written.  ``materialize_ad_edit_artifacts`` performs the
    version-preserving writes after the caller reviews this bundle.
    """

    root = _root_path(project_root)
    source = safe_project_path(root, source_master, must_exist=True, require_file=True)
    working = safe_project_path(root, working_master, must_exist=True, require_file=True)
    if _same_file_or_path(source, working):
        raise PathSafetyError("正式工作母版必须与原始母版是不同文件，禁止覆盖或硬链接别名")
    validation = validate_ad_analysis(
        analysis,
        source_duration=source_duration,
        frame_width=frame_width,
        frame_height=frame_height,
    )
    if validation.status == "analysis_required":
        return {
            "status": "analysis_required",
            "blockers": list(validation.blockers),
            "analysis": dict(validation.normalized or {}),
            "ad_edit_gate": None,
        }
    if not validation.ok or validation.normalized is None:
        raise AdEvidenceError("; ".join(validation.errors) or "广告证据校验失败")
    normalized = dict(validation.normalized)
    candidates = list(normalized["candidates"])
    removed = [row for row in candidates if row["action"] == "remove"]
    masks = [row for row in candidates if row["action"] == "mask"]
    timeline = build_source_to_edit_timeline(source_duration, removed)
    decisions = {
        "schema_version": 1,
        "status": "pass",
        "decision": normalized["decision"],
        "decision_rule_version": normalized["decision_rule_version"],
        "candidates": candidates,
        "unresolved_count": 0,
    }
    overlay_plan = {
        "schema_version": 1,
        "status": "pass",
        "render_order": ["video", "overlay", "subtitles"],
        "subtitle_layer_above_overlays": True,
        "regions": [
            {
                "candidate_id": row["id"],
                "start": row["start"],
                "end": row["end"],
                **dict(row["region"]),
            }
            for row in masks
        ],
    }
    detection = normalized
    paths = {
        "detection": "qa/ad_detection.json",
        "decisions": "qa/ad_decisions.json",
        "overlay": "qa/ad_overlay_plan.json",
        "timeline": "qa/source_to_edit_timeline.json",
        "gate": "qa/ad_edit_gate.json",
    }
    gate: dict[str, Any] = {
        "schema_version": 2,
        "status": "pass",
        "decision": normalized["decision"],
        "detection_outcome": normalized["detection_outcome"],
        "source_master": _artifact(root, source),
        "working_master": _artifact(root, working),
        "original_master_preserved": True,
        "content_ad_scan_complete": True,
        "visual_ad_scan_complete": True,
        "semantic_analysis_required": normalized["semantic_analysis_required"],
        "semantic_analysis_complete": normalized["semantic_analysis_complete"],
        "all_candidates_have_evidence_based_decision": True,
        "uncertain_candidates_default_keep": True,
        "decision_rule_version": normalized["decision_rule_version"],
        "timeline_mapping": _planned_artifact(paths["timeline"], timeline),
        "ad_detection": _planned_artifact(paths["detection"], detection),
        "ad_decisions": _planned_artifact(paths["decisions"], decisions),
        "overlay_plan": _planned_artifact(paths["overlay"], overlay_plan),
        "removed_segments": [
            {"id": row["id"], "start": row["start"], "end": row["end"]}
            for row in removed
        ],
        "overlay_regions": overlay_plan["regions"],
        "subtitle_layer_above_overlays": True,
        "render_order": "video_overlay_subtitles",
        "formal_slots_based_on_working_master": frozen_source_path is not None,
        "unresolved_count": 0,
    }
    if frozen_source_path is not None:
        gate["frozen_source_transcript"] = _artifact(
            root,
            safe_project_path(root, frozen_source_path, must_exist=True, require_file=True),
        )
    documents = {
        paths["detection"]: detection,
        paths["decisions"]: decisions,
        paths["overlay"]: overlay_plan,
        paths["timeline"]: timeline,
        paths["gate"]: gate,
    }
    return {
        "status": "pass",
        "paths": paths,
        "documents": documents,
        "ad_edit_gate": gate,
    }


def materialize_ad_edit_artifacts(
    bundle: Mapping[str, Any],
    *,
    project_root: Path | str,
) -> list[dict[str, str]]:
    """Write a pass bundle once, reusing only byte-identical artifacts.

    Exact reuse closes the narrow crash window between writing the five QA
    documents and persisting the parent task result.  Different bytes still
    fail closed, so a later run can never overwrite an earlier decision.
    """

    if bundle.get("status") != "pass" or not isinstance(bundle.get("documents"), Mapping):
        raise AdEvidenceError("只有 status=pass 的广告制品包可以落盘")
    root = _root_path(project_root)
    documents = bundle["documents"]
    planned: list[tuple[Path, bytes]] = []
    for raw_path, payload in documents.items():
        path = safe_project_path(root, str(raw_path))
        data = canonical_json_bytes(payload)
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise PathSafetyError(f"QA 制品已存在且内容不同，必须创建新版本: {path}")
            continue
        planned.append((path, data))
    # Preflight every path before any write so normal conflicts cannot leave a
    # partial gate bundle.  O_EXCL in _write_new_file closes the race window.
    for path, _ in planned:
        path.parent.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, str]] = []
    for raw_path, payload in documents.items():
        path = safe_project_path(root, str(raw_path))
        if path.exists() and all(path != pending for pending, _ in planned):
            data = canonical_json_bytes(payload)
            written.append(
                {
                    "path": _portable_path(root, path),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    for path, data in planned:
        _write_new_file(path, data)
        written.append(
            {
                "path": _portable_path(root, path),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return written


def _write_new_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "AdEvidenceError",
    "CommandPlan",
    "IngestError",
    "MediaProbeError",
    "PathSafetyError",
    "TranscriptError",
    "ValidationResult",
    "build_ad_edit_artifacts",
    "build_embedded_subtitle_plan",
    "build_ffprobe_plan",
    "build_source_to_edit_timeline",
    "canonical_json_bytes",
    "canonical_sha256",
    "import_source_transcript",
    "map_source_time",
    "materialize_ad_edit_artifacts",
    "normalize_source_cues",
    "parse_ffprobe_payload",
    "parse_timestamp",
    "probe_media",
    "safe_project_path",
    "sha256_file",
    "validate_ad_analysis",
    "write_frozen_source",
]
