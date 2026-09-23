"""Portable, deterministic render planning and final machine QA.

This module deliberately separates planning from execution.  Building a render
plan is a pure operation: it validates the complete source-frame coverage,
maps evidence-backed visual masks to the retimed output timeline and produces
an argv list that can be inspected before FFmpeg is started.  The Chinese
speech input is always mapped directly; audio tempo, stretching and trimming
filters are never part of a valid plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

EPSILON = 1e-6
FORBIDDEN_AUDIO_PROCESSORS = (
    "atempo",
    "rubberband",
    "asetrate",
    "atrim",
    "asetpts",
)
FORBIDDEN_OUTPUT_OPTIONS = ("-shortest", "-t", "-to")
PRODUCTION_ARTIFACT_KEYS = (
    "working_master",
    "ad_overlay_plan",
    "translation",
    "role_map",
    "voice_mapping",
    "tts_manifest",
    "chinese_timeline",
    "video_retime_plan",
)
DEFAULT_SUBTITLE_STYLE = (
    "FontName=Microsoft YaHei,FontSize=22,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00101010,BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=22"
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COLOR = re.compile(r"^(?:#[0-9a-fA-F]{6}|[A-Za-z]+)$")
_FORBIDDEN_FILTER = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(FORBIDDEN_AUDIO_PROCESSORS) + r")\s*=",
    re.IGNORECASE,
)


class RenderPlanError(ValueError):
    """Raised when a render plan would violate a production invariant."""


class MachineQAError(RuntimeError):
    """Raised when the machine-QA tools cannot produce trustworthy evidence."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _number(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RenderPlanError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise RenderPlanError(f"{name} must be a finite number")
    return result


def _positive(value: object, name: str) -> float:
    result = _number(value, name)
    if result <= 0:
        raise RenderPlanError(f"{name} must be greater than zero")
    return result


def _format_number(value: float) -> str:
    rendered = f"{value:.9f}".rstrip("0").rstrip(".")
    return rendered if rendered not in {"", "-0"} else "0"


def _close(left: float, right: float, tolerance: float = EPSILON) -> bool:
    return abs(left - right) <= tolerance


def _parse_rate(value: object) -> float:
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        denominator_value = _positive(denominator, "frame-rate denominator")
        return _positive(numerator, "frame-rate numerator") / denominator_value
    return _positive(value, "frame rate")


def _normalise_removed_intervals(
    values: Iterable[Mapping[str, Any] | Sequence[float]],
    source_duration: float,
) -> list[dict[str, float]]:
    intervals: list[dict[str, float]] = []
    for index, value in enumerate(values):
        if isinstance(value, Mapping):
            start = _number(
                value.get("source_start", value.get("start")),
                f"removed interval {index} start",
            )
            end = _number(
                value.get("source_end", value.get("end")),
                f"removed interval {index} end",
            )
        else:
            if len(value) != 2:
                raise RenderPlanError(f"removed interval {index} must contain two values")
            start = _number(value[0], f"removed interval {index} start")
            end = _number(value[1], f"removed interval {index} end")
        if start < -EPSILON or end > source_duration + EPSILON or end <= start:
            raise RenderPlanError(f"removed interval {index} is outside the working master")
        intervals.append({"source_start": max(0.0, start), "source_end": min(source_duration, end)})
    intervals.sort(key=lambda row: row["source_start"])
    previous_end = -math.inf
    for index, interval in enumerate(intervals):
        if interval["source_start"] < previous_end - EPSILON:
            raise RenderPlanError(f"removed intervals overlap at index {index}")
        previous_end = interval["source_end"]
    return intervals


def _expected_source_intervals(
    source_duration: float,
    removed: Sequence[Mapping[str, float]],
) -> list[tuple[float, float]]:
    expected: list[tuple[float, float]] = []
    cursor = 0.0
    for interval in removed:
        start = float(interval["source_start"])
        end = float(interval["source_end"])
        if start > cursor + EPSILON:
            expected.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < source_duration - EPSILON:
        expected.append((cursor, source_duration))
    return expected


def _merge_adjacent(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1] + EPSILON:
            merged.append((start, end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
    return merged


def validate_retime_segments(
    segments: Iterable[Mapping[str, Any]],
    *,
    source_duration: float,
    removed_intervals: Iterable[Mapping[str, Any] | Sequence[float]] = (),
) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """Validate a monotonic video-only mapping with complete source coverage.

    The returned segments preserve their input order.  Their target intervals
    must start at zero and be contiguous.  Source intervals must cover every
    frame of the formal working master except explicitly removed ad intervals.
    """

    duration = _positive(source_duration, "source duration")
    removed = _normalise_removed_intervals(removed_intervals, duration)
    normalised: list[dict[str, float]] = []
    previous_source_end: float | None = None
    previous_target_end = 0.0
    for index, segment in enumerate(segments):
        source_start = _number(segment.get("source_start"), f"segment {index} source_start")
        source_end = _number(segment.get("source_end"), f"segment {index} source_end")
        target_start = _number(segment.get("target_start"), f"segment {index} target_start")
        target_end = _number(segment.get("target_end"), f"segment {index} target_end")
        if source_start < -EPSILON or source_end > duration + EPSILON or source_end <= source_start:
            raise RenderPlanError(f"segment {index} has an invalid source interval")
        if target_start < -EPSILON or target_end <= target_start:
            raise RenderPlanError(f"segment {index} has an invalid target interval")
        if previous_source_end is not None and source_start < previous_source_end - EPSILON:
            raise RenderPlanError(f"segment {index} overlaps or reorders source frames")
        if not _close(target_start, previous_target_end):
            raise RenderPlanError(f"segment {index} leaves a target gap or overlap")
        for interval in removed:
            overlap = min(source_end, interval["source_end"]) - max(source_start, interval["source_start"])
            if overlap > EPSILON:
                raise RenderPlanError(f"segment {index} includes an explicitly removed ad interval")
        source_length = source_end - source_start
        target_length = target_end - target_start
        normalised.append(
            {
                "source_start": max(0.0, source_start),
                "source_end": min(duration, source_end),
                "target_start": max(0.0, target_start),
                "target_end": target_end,
                "setpts_factor": target_length / source_length,
                "video_speed": source_length / target_length,
            }
        )
        previous_source_end = source_end
        previous_target_end = target_end

    if not normalised:
        raise RenderPlanError("at least one video retime segment is required")
    actual = _merge_adjacent(
        [(row["source_start"], row["source_end"]) for row in normalised]
    )
    expected = _expected_source_intervals(duration, removed)
    if len(actual) != len(expected) or any(
        not (_close(left[0], right[0]) and _close(left[1], right[1]))
        for left, right in zip(actual, expected, strict=True)
    ):
        raise RenderPlanError("video retime plan does not cover every retained working-master frame")
    return normalised, removed


def _integer(value: object, name: str) -> int:
    number = _number(value, name)
    integer = int(number)
    if not _close(number, integer):
        raise RenderPlanError(f"{name} must be an integer pixel coordinate")
    return integer


def _overlay_is_verified(value: Mapping[str, Any]) -> bool:
    if value.get("verified") is True or value.get("evidence_verified") is True:
        return True
    frame_verified = any(
        value.get(key) is True
        for key in ("sample_verified", "snapshot_verified", "frame_verified")
    )
    return (
        value.get("time_verified") is True
        and value.get("coordinates_verified") is True
        and frame_verified
    )


def _map_source_interval(
    start: float,
    end: float,
    segments: Sequence[Mapping[str, float]],
) -> list[tuple[float, float, float, float]]:
    mapped: list[tuple[float, float, float, float]] = []
    for segment in segments:
        source_start = float(segment["source_start"])
        source_end = float(segment["source_end"])
        intersection_start = max(start, source_start)
        intersection_end = min(end, source_end)
        if intersection_end <= intersection_start + EPSILON:
            continue
        ratio = (float(segment["target_end"]) - float(segment["target_start"])) / (
            source_end - source_start
        )
        target_start = float(segment["target_start"]) + (intersection_start - source_start) * ratio
        target_end = float(segment["target_start"]) + (intersection_end - source_start) * ratio
        mapped.append((intersection_start, intersection_end, target_start, target_end))
    return mapped


def validate_and_map_overlays(
    overlays: Iterable[Mapping[str, Any]],
    *,
    segments: Sequence[Mapping[str, float]],
    source_duration: float,
    frame_width: int,
    frame_height: int,
) -> list[dict[str, Any]]:
    """Validate evidence-backed ad masks and map them to output time."""

    mapped: list[dict[str, Any]] = []
    for index, overlay in enumerate(overlays):
        if not _overlay_is_verified(overlay):
            raise RenderPlanError(f"overlay {index} lacks time/coordinate/frame verification")
        action = str(overlay.get("action") or overlay.get("kind") or "overlay").lower()
        if action not in {"overlay", "mask", "drawbox", "solid"}:
            raise RenderPlanError(f"overlay {index} has unsupported action {action!r}")
        start = _number(
            overlay.get("source_start", overlay.get("start")),
            f"overlay {index} source_start",
        )
        end = _number(
            overlay.get("source_end", overlay.get("end")),
            f"overlay {index} source_end",
        )
        if start < -EPSILON or end > source_duration + EPSILON or end <= start:
            raise RenderPlanError(f"overlay {index} has an invalid source interval")
        x = _integer(overlay.get("x"), f"overlay {index} x")
        y = _integer(overlay.get("y"), f"overlay {index} y")
        width = _integer(overlay.get("width", overlay.get("w")), f"overlay {index} width")
        height = _integer(overlay.get("height", overlay.get("h")), f"overlay {index} height")
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise RenderPlanError(f"overlay {index} has an invalid rectangle")
        if x + width > frame_width or y + height > frame_height:
            raise RenderPlanError(f"overlay {index} extends outside the video frame")
        color = str(overlay.get("color") or "black")
        if not _SAFE_COLOR.fullmatch(color):
            raise RenderPlanError(f"overlay {index} has an unsafe color value")
        opacity = _number(overlay.get("opacity", 1.0), f"overlay {index} opacity")
        if opacity < 0 or opacity > 1:
            raise RenderPlanError(f"overlay {index} opacity must be between zero and one")
        pieces = _map_source_interval(start, end, segments)
        if not pieces:
            raise RenderPlanError(f"overlay {index} only targets removed frames")
        for part_index, (source_start, source_end, target_start, target_end) in enumerate(pieces):
            mapped.append(
                {
                    "id": str(overlay.get("id") or f"overlay-{index + 1}"),
                    "part": part_index + 1,
                    "source_start": source_start,
                    "source_end": source_end,
                    "target_start": target_start,
                    "target_end": target_end,
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "color": color,
                    "opacity": opacity,
                    "evidence_verified": True,
                }
            )
    return mapped


def escape_subtitle_filter_path(path: str | Path) -> str:
    """Escape a path for FFmpeg's libass ``subtitles=filename=`` option."""

    value = str(path).replace("\\", "/")
    return (
        value.replace("'", "\\'")
        .replace(":", "\\:")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def build_video_filter_graph(
    segments: Sequence[Mapping[str, float]],
    overlays: Sequence[Mapping[str, Any]],
    *,
    subtitle_file: str | Path,
) -> str:
    """Build a video-only filter graph in the mandated layer order."""

    chains: list[str] = []
    labels: list[str] = []
    for index, segment in enumerate(segments):
        label = f"vseg{index}"
        labels.append(f"[{label}]")
        chains.append(
            "[0:v:0]"
            f"trim=start={_format_number(float(segment['source_start']))}:"
            f"end={_format_number(float(segment['source_end']))},"
            "setpts=(PTS-STARTPTS)*"
            f"{_format_number(float(segment['setpts_factor']))}[{label}]"
        )
    if len(labels) == 1:
        chains.append(f"{labels[0]}null[vretime]")
    else:
        chains.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vretime]")

    current = "vretime"
    for index, overlay in enumerate(overlays):
        next_label = f"vmask{index}"
        color = str(overlay["color"])
        if color.startswith("#"):
            color = "0x" + color[1:]
        chains.append(
            f"[{current}]drawbox="
            f"x={overlay['x']}:y={overlay['y']}:"
            f"w={overlay['width']}:h={overlay['height']}:"
            f"color={color}@{_format_number(float(overlay['opacity']))}:t=fill:"
            "enable='between(t,"
            f"{_format_number(float(overlay['target_start']))},"
            f"{_format_number(float(overlay['target_end']))})'[{next_label}]"
        )
        current = next_label

    escaped_subtitles = escape_subtitle_filter_path(subtitle_file)
    chains.append(
        f"[{current}]subtitles=filename='{escaped_subtitles}':"
        f"force_style='{DEFAULT_SUBTITLE_STYLE}'[vout]"
    )
    return ";".join(chains)


def build_render_plan(
    *,
    working_master: str | Path,
    chinese_audio: str | Path,
    subtitle_file: str | Path,
    output_file: str | Path,
    source_duration: float,
    frame_width: int,
    frame_height: int,
    frame_rate: float | str,
    retime_segments: Iterable[Mapping[str, Any]],
    overlay_regions: Iterable[Mapping[str, Any]] = (),
    removed_intervals: Iterable[Mapping[str, Any] | Sequence[float]] = (),
    chinese_audio_duration: float | None = None,
    tts_native_speed: float = 1.0,
    offline_rate: float = 1.0,
    ffmpeg: str = "ffmpeg",
    video_codec: str = "libx264",
    audio_bitrate: str = "192k",
) -> dict[str, Any]:
    """Create a complete, JSON-serialisable render plan without running tools."""

    if not _close(_number(tts_native_speed, "TTS native speed"), 1.0):
        raise RenderPlanError("Chinese TTS speed must remain exactly 1.0")
    if not _close(_number(offline_rate, "offline audio rate"), 1.0):
        raise RenderPlanError("Chinese offline audio rate must remain exactly 1.0")
    duration = _positive(source_duration, "source duration")
    width = _integer(frame_width, "frame width")
    height = _integer(frame_height, "frame height")
    if width <= 0 or height <= 0:
        raise RenderPlanError("frame dimensions must be positive")
    fps = _parse_rate(frame_rate)
    output = Path(output_file)
    if output.suffix.lower() != ".mp4":
        raise RenderPlanError("the soft-subtitle production output must be an MP4 file")
    codec = str(video_codec)
    if codec not in {"libx264", "libx265", "h264_nvenc", "hevc_nvenc"}:
        raise RenderPlanError("unsupported video codec")

    segments, removed = validate_retime_segments(
        retime_segments,
        source_duration=duration,
        removed_intervals=removed_intervals,
    )
    target_duration = float(segments[-1]["target_end"])
    if chinese_audio_duration is not None:
        audio_duration = _positive(chinese_audio_duration, "Chinese audio duration")
        duration_tolerance = max(0.05, 2.0 / fps)
        if abs(audio_duration - target_duration) > duration_tolerance:
            raise RenderPlanError(
                "Chinese audio duration does not match the video retime target; "
                "the audio must not be stretched or truncated"
            )

    overlays = validate_and_map_overlays(
        overlay_regions,
        segments=segments,
        source_duration=duration,
        frame_width=width,
        frame_height=height,
    )
    filter_graph = build_video_filter_graph(
        segments,
        overlays,
        subtitle_file=subtitle_file,
    )
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-n",
        "-i",
        str(working_master),
        "-i",
        str(chinese_audio),
        "-i",
        str(subtitle_file),
        "-filter_complex",
        filter_graph,
        "-map",
        "[vout]",
        "-map",
        "1:a:0",
        "-map",
        "2:0",
        "-c:v",
        codec,
        "-fps_mode:v:0",
        "passthrough",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bitrate),
        "-c:s",
        "mov_text",
        "-metadata:s:a:0",
        "language=zho",
        "-metadata:s:s:0",
        "language=zho",
        "-metadata:s:s:0",
        "title=Simplified Chinese",
        "-disposition:s:0",
        "default",
        "-movflags",
        "+faststart",
        str(output_file),
    ]
    plan: dict[str, Any] = {
        "schema_version": 1,
        "status": "ready",
        "inputs": {
            "working_master": str(working_master),
            "chinese_audio": str(chinese_audio),
            "subtitle_file": str(subtitle_file),
        },
        "output_file": str(output_file),
        "source": {
            "duration": duration,
            "width": width,
            "height": height,
            "frame_rate": fps,
        },
        "expected_output": {
            "duration": target_duration,
            "width": width,
            "height": height,
            "frame_rate": fps,
            "require_video": True,
            "require_audio": True,
            "require_subtitle": True,
            "require_strict_video_dts": True,
        },
        "sync_strategy": "video_retime_only",
        "tts_native_speed": 1.0,
        "offline_rate": 1.0,
        "audio_time_stretch": False,
        "audio_input_mode": "direct_map",
        "audio_filters": [],
        "forbidden_audio_processors": [],
        "video_only_retime": True,
        "working_master_frames_preserved": True,
        "coverage_complete": True,
        "removed_ad_intervals": removed,
        "video_retime_segments": segments,
        "overlay_regions": overlays,
        "overlay_intervals_valid": True,
        "render_order": ["retimed_video", "ad_overlay", "burned_subtitles"],
        "subtitle_layer_last": True,
        "soft_subtitle_track": True,
        "filter_graph": filter_graph,
        "ffmpeg_command": command,
    }
    issues = validate_render_plan(plan)
    if issues:
        raise RenderPlanError("invalid render plan: " + ", ".join(issues))
    return plan


def validate_render_plan(plan: Mapping[str, Any]) -> list[str]:
    """Return stable issue codes for any unsafe render-plan property."""

    issues: list[str] = []
    if plan.get("sync_strategy") != "video_retime_only":
        issues.append("sync_strategy_not_video_only")
    if plan.get("tts_native_speed") != 1.0:
        issues.append("tts_speed_not_one")
    if plan.get("offline_rate") != 1.0:
        issues.append("offline_rate_not_one")
    if plan.get("audio_time_stretch") is not False:
        issues.append("audio_time_stretch_enabled")
    if plan.get("audio_filters") not in ([], ()):
        issues.append("audio_filter_chain_not_empty")
    declared_processors = [
        str(value).lower() for value in plan.get("forbidden_audio_processors") or []
    ]
    if declared_processors:
        issues.append("forbidden_audio_processor:" + ",".join(sorted(declared_processors)))
    if plan.get("audio_input_mode") != "direct_map":
        issues.append("chinese_audio_not_direct_mapped")
    if plan.get("coverage_complete") is not True:
        issues.append("source_coverage_incomplete")
    if plan.get("working_master_frames_preserved") is not True:
        issues.append("working_master_frames_not_preserved")
    if plan.get("video_only_retime") is not True:
        issues.append("non_video_retime_present")
    if plan.get("overlay_intervals_valid") is not True:
        issues.append("overlay_intervals_invalid")
    if list(plan.get("render_order") or []) != [
        "retimed_video",
        "ad_overlay",
        "burned_subtitles",
    ]:
        issues.append("render_order_invalid")
    if plan.get("subtitle_layer_last") is not True:
        issues.append("subtitle_layer_not_last")
    if plan.get("soft_subtitle_track") is not True:
        issues.append("soft_subtitle_track_missing")

    graph = str(plan.get("filter_graph") or "")
    processors = sorted({match.group(1).lower() for match in _FORBIDDEN_FILTER.finditer(graph)})
    if processors:
        issues.append("forbidden_audio_processor:" + ",".join(processors))
    final_chain = graph.rsplit(";", 1)[-1]
    if "subtitles=" not in final_chain or not final_chain.endswith("[vout]"):
        issues.append("subtitle_filter_not_last")

    command = [str(value) for value in plan.get("ffmpeg_command") or []]
    if any(option in command for option in FORBIDDEN_OUTPUT_OPTIONS):
        issues.append("audio_truncating_output_option")
    if "-af" in command or "-filter:a" in command:
        issues.append("audio_filter_option_present")
    if "-fps_mode:v:0" not in command or "passthrough" not in command:
        issues.append("video_frame_passthrough_missing")
    maps = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-map"]
    if "1:a:0" not in maps:
        issues.append("chinese_audio_map_missing")
    if "2:0" not in maps or "-c:s" not in command or "mov_text" not in command:
        issues.append("soft_subtitle_map_missing")
    if "[vout]" not in maps:
        issues.append("filtered_video_map_missing")
    return issues


def artifact_ref(path: str | Path, *, relative_to: str | Path | None = None) -> dict[str, str]:
    """Return a content-addressed artifact reference suitable for a gate."""

    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(file_path)
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    display_path: Path = file_path
    if relative_to is not None:
        try:
            display_path = file_path.resolve().relative_to(Path(relative_to).resolve())
        except ValueError as exc:
            raise RenderPlanError("artifact is outside the requested portable root") from exc
    return {"path": display_path.as_posix(), "sha256": digest.hexdigest()}


def _valid_artifact(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("path"), str)
        and bool(value.get("path"))
        and isinstance(value.get("sha256"), str)
        and bool(_HEX_SHA256.fullmatch(str(value.get("sha256"))))
    )


def build_production_gate_report(
    plan: Mapping[str, Any],
    *,
    artifacts: Mapping[str, Mapping[str, str]],
    ad_edit_gate_status: str,
    translation_gate_status: str,
    working_master_and_overlay_hashes_match: bool,
    translation_hash_matches: bool,
    voice_mapping_locked: bool,
    authorization_bound: bool,
    automatic_dry_run_recorded: bool,
    tts_speeds: Sequence[float],
    offline_rates: Sequence[float],
    chinese_overlap_count: int,
    subtitle_timeline_rebuilt: bool,
) -> dict[str, Any]:
    """Build the deterministic pre-render production-gate payload.

    A failed report is intentionally returned with ``status=fail`` so callers
    can persist the evidence.  Only a report with no failure codes uses the
    schema-compatible ``status=pass`` value.
    """

    plan_issues = validate_render_plan(plan)
    detected_processors = sorted(
        {
            str(value).lower()
            for value in plan.get("forbidden_audio_processors") or []
        }
        | {
            match.group(1).lower()
            for match in _FORBIDDEN_FILTER.finditer(str(plan.get("filter_graph") or ""))
        }
    )
    tts_values = [float(value) for value in tts_speeds]
    offline_values = [float(value) for value in offline_rates]
    artifact_checks = {
        key: _valid_artifact(artifacts.get(key)) for key in PRODUCTION_ARTIFACT_KEYS
    }
    checks: dict[str, bool] = {
        "ad_edit_gate_pass": ad_edit_gate_status == "pass",
        "translation_gate_pass": translation_gate_status == "pass",
        "working_master_and_overlay_hashes_match": bool(
            working_master_and_overlay_hashes_match
        ),
        "translation_hash_matches": bool(translation_hash_matches),
        "voice_mapping_locked": bool(voice_mapping_locked),
        "user_generate_full_command_matches_frozen_inputs": bool(authorization_bound),
        "automatic_dry_run_recorded": bool(automatic_dry_run_recorded),
        "all_tts_speed_1_0": bool(tts_values)
        and all(_close(value, 1.0) for value in tts_values),
        "all_offline_rate_1_0": bool(offline_values)
        and all(_close(value, 1.0) for value in offline_values),
        "chinese_tempo_filters_zero": not any(
            issue.startswith("forbidden_audio_processor") for issue in plan_issues
        ),
        "chinese_overlap_count_zero": chinese_overlap_count == 0,
        "retime_source_coverage_complete": plan.get("coverage_complete") is True,
        "retime_gaps_zero": plan.get("coverage_complete") is True,
        "retime_overlaps_zero": plan.get("coverage_complete") is True,
        "overlay_intervals_valid": plan.get("overlay_intervals_valid") is True,
        "render_order_video_overlay_subtitles": list(plan.get("render_order") or [])
        == ["retimed_video", "ad_overlay", "burned_subtitles"],
        "subtitle_timeline_rebuilt": bool(subtitle_timeline_rebuilt),
        "render_plan_safe": not plan_issues,
        "required_artifacts_complete": all(artifact_checks.values()),
    }
    failure_codes = [name for name, passed in checks.items() if not passed]
    failure_codes.extend(f"render_plan:{issue}" for issue in plan_issues)
    failure_codes.extend(
        f"artifact_invalid:{key}" for key, passed in artifact_checks.items() if not passed
    )
    # Preserve order while removing duplicate codes.
    failure_codes = list(dict.fromkeys(failure_codes))
    video_retime_plan = artifacts.get("video_retime_plan") or {"path": "", "sha256": ""}
    return {
        "schema_version": 1,
        "status": "pass" if not failure_codes else "fail",
        "tts_native_speed": plan.get("tts_native_speed"),
        "offline_rate": plan.get("offline_rate"),
        "audio_time_stretch": plan.get("audio_time_stretch") is not False,
        "forbidden_audio_processors": detected_processors,
        "sync_strategy": plan.get("sync_strategy"),
        "working_master_frames_preserved": plan.get("working_master_frames_preserved") is True,
        "video_retime_plan": dict(video_retime_plan),
        "coverage_complete": plan.get("coverage_complete") is True,
        "soft_subtitle_track": plan.get("soft_subtitle_track") is True,
        "subtitle_layer_last": plan.get("subtitle_layer_last") is True,
        "artifacts": {key: dict(value) for key, value in artifacts.items()},
        "artifact_checks": artifact_checks,
        "checks": checks,
        "failure_codes": failure_codes,
    }


def build_machine_qa_commands(
    final_file: str | Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> dict[str, list[str]]:
    """Build metadata, packet-DTS and full A/V decode commands."""

    path = str(final_file)
    return {
        "metadata": [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            (
                "format=filename,duration,size,bit_rate:"
                "stream=index,codec_name,codec_type,width,height,avg_frame_rate,"
                "r_frame_rate,sample_rate,channels,duration,nb_frames:stream_tags=language"
            ),
            "-of",
            "json",
            path,
        ],
        "video_dts": [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=stream_index,dts,dts_time",
            "-of",
            "json",
            path,
        ],
        "decode": [
            str(ffmpeg),
            "-v",
            "error",
            "-xerror",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
    }


def _probe_duration(payload: Mapping[str, Any]) -> float | None:
    try:
        value = float((payload.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _observed_rate(stream: Mapping[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if value in (None, "", "0/0"):
            continue
        try:
            return _parse_rate(value)
        except RenderPlanError:
            continue
    return None


def _video_dts_values(packets: Iterable[Mapping[str, Any]]) -> list[float] | None:
    values: list[float] = []
    for packet in packets:
        raw = packet.get("dts_time", packet.get("dts"))
        if raw in (None, "", "N/A"):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
    return values


def evaluate_machine_qa(
    probe: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    decode_returncode: int = 0,
    decode_stderr: str = "",
) -> dict[str, Any]:
    """Evaluate ffprobe/decode evidence without touching the filesystem."""

    streams = [row for row in probe.get("streams") or [] if isinstance(row, Mapping)]
    videos = [row for row in streams if row.get("codec_type") == "video"]
    audios = [row for row in streams if row.get("codec_type") == "audio"]
    subtitles = [row for row in streams if row.get("codec_type") == "subtitle"]
    video = videos[0] if videos else {}
    observed_duration = _probe_duration(probe)
    observed_rate = _observed_rate(video) if video else None
    dts_values = _video_dts_values(
        row for row in probe.get("packets") or [] if isinstance(row, Mapping)
    )
    require_dts = expected.get("require_strict_video_dts", True) is True
    dts_strict = bool(dts_values) and all(
        current > previous for previous, current in pairwise(dts_values)
    )

    checks: dict[str, bool] = {
        "video_stream_present": bool(videos),
        "audio_stream_present": bool(audios),
        "subtitle_stream_present": bool(subtitles),
        "full_video_audio_decode": decode_returncode == 0,
        "video_dts_available": (dts_values is not None and bool(dts_values)) or not require_dts,
        "video_dts_strictly_increasing": dts_strict or not require_dts,
    }
    if expected.get("width") is not None:
        checks["width_matches"] = bool(video) and int(video.get("width") or 0) == int(expected["width"])
    if expected.get("height") is not None:
        checks["height_matches"] = bool(video) and int(video.get("height") or 0) == int(expected["height"])
    if expected.get("frame_rate") is not None:
        target_rate = _parse_rate(expected["frame_rate"])
        rate_tolerance = float(expected.get("frame_rate_tolerance", 0.01))
        checks["frame_rate_matches"] = (
            observed_rate is not None and abs(observed_rate - target_rate) <= rate_tolerance
        )
    if expected.get("duration") is not None:
        target_duration = float(expected["duration"])
        duration_tolerance = float(expected.get("duration_tolerance", 0.25))
        checks["duration_matches"] = (
            observed_duration is not None
            and abs(observed_duration - target_duration) <= duration_tolerance
        )
    if expected.get("subtitle_count") is not None:
        subtitle_frames: int | None = None
        if subtitles and subtitles[0].get("nb_frames") not in (None, "", "N/A"):
            try:
                subtitle_frames = int(subtitles[0]["nb_frames"])
            except (TypeError, ValueError):
                subtitle_frames = None
        checks["subtitle_count_matches"] = subtitle_frames == int(expected["subtitle_count"])
    expected_language = expected.get("subtitle_language")
    if expected_language:
        languages = {str((row.get("tags") or {}).get("language") or "") for row in subtitles}
        checks["subtitle_language_matches"] = str(expected_language) in languages

    failures = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "status": "pass" if not failures else "fail",
        "checks": checks,
        "failure_codes": failures,
        "observed": {
            "duration": observed_duration,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "frame_rate": observed_rate,
            "video_stream_count": len(videos),
            "audio_stream_count": len(audios),
            "subtitle_stream_count": len(subtitles),
            "video_packet_count": len(dts_values or []),
        },
        "decode_error": decode_stderr[-2000:] if decode_returncode != 0 else "",
    }


def _run_command(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MachineQAError(f"machine QA command failed: {type(exc).__name__}") from exc


def _json_result(result: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-1000:]
        raise MachineQAError(f"{label} failed with exit code {result.returncode}: {detail}")
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MachineQAError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise MachineQAError(f"{label} returned an invalid payload")
    return value


def run_machine_qa(
    final_file: str | Path,
    expected: Mapping[str, Any],
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    command_runner: CommandRunner = subprocess.run,
    probe_timeout: float = 300.0,
    decode_timeout: float = 7200.0,
) -> dict[str, Any]:
    """Run final ffprobe, packet scan and ``ffmpeg -v error -xerror`` QA."""

    path = Path(final_file)
    if not path.is_file():
        raise MachineQAError("final media file does not exist")
    if path.stat().st_size <= 0:
        raise MachineQAError("final media file is empty")
    commands = build_machine_qa_commands(path, ffprobe=ffprobe, ffmpeg=ffmpeg)
    metadata_result = _run_command(command_runner, commands["metadata"], timeout=probe_timeout)
    packet_result = _run_command(command_runner, commands["video_dts"], timeout=probe_timeout)
    metadata = _json_result(metadata_result, "ffprobe metadata")
    packets = _json_result(packet_result, "ffprobe video packet scan")
    metadata["packets"] = packets.get("packets") or []
    decode = _run_command(command_runner, commands["decode"], timeout=decode_timeout)
    report = evaluate_machine_qa(
        metadata,
        expected,
        decode_returncode=decode.returncode,
        decode_stderr=decode.stderr or "",
    )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    report.update(
        {
            "final_file": str(path),
            "byte_size": path.stat().st_size,
            "sha256": digest.hexdigest(),
            "qa_commands": {
                "ffprobe_metadata": True,
                "ffprobe_video_packets": True,
                "ffmpeg_error_xerror_decode": True,
            },
        }
    )
    return report
