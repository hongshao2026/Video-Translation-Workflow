"""Persistent, deterministic FFmpeg jobs for run-scoped media artifacts.

The semantic decisions are deliberately made elsewhere.  This module only
executes already-frozen mechanical work: embedded-subtitle extraction,
working-master construction from validated advertisement evidence, and PNG
frame extraction from the formal working master.

Every job is restart-safe.  A small immutable plan is written before FFmpeg is
started; a retry may reuse bytes only when that plan and every input hash still
match.  Generated paths are always project-relative in persisted artifacts.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .artifact_services import WorkflowArtifactService
from .database import WorkbenchDatabase
from .gates import validate_gate_file
from .ingest import (
    AdEvidenceError,
    PathSafetyError,
    build_ad_edit_artifacts,
    build_embedded_subtitle_plan,
    build_source_to_edit_timeline,
    canonical_json_bytes,
    canonical_sha256,
    import_source_transcript,
    map_source_time,
    materialize_ad_edit_artifacts,
    parse_ffprobe_payload,
    safe_project_path,
    sha256_file,
    validate_ad_analysis,
)
from .library import ProjectLibrary
from .runner import ProgressReporter, TaskToken
from .settings import WorkbenchSettings


class MediaArtifactError(RuntimeError):
    """Raised when a deterministic media artifact cannot be produced safely."""


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _artifact(root: Path, path: Path) -> dict[str, Any]:
    safe = safe_project_path(root, path, must_exist=True, require_file=True)
    return {
        "path": _relative(root, safe),
        "sha256": sha256_file(safe),
        "byte_size": safe.stat().st_size,
    }


def _write_bytes_once_or_same(path: Path, data: bytes) -> bool:
    """Write immutable bytes and return ``True`` when an existing match is reused."""

    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise MediaArtifactError(f"版本化制品已存在且内容不同: {path.name}")
        return True
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
    return False


def _write_json_once_or_same(path: Path, payload: Mapping[str, Any]) -> bool:
    return _write_bytes_once_or_same(path, canonical_json_bytes(payload))


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MediaArtifactError(f"{label}不是可读 JSON") from exc
    if not isinstance(payload, dict):
        raise MediaArtifactError(f"{label}必须是 JSON 对象")
    return payload


def _format_seconds(value: object) -> str:
    number = float(value)
    if not (number >= 0 and number < float("inf")):
        raise MediaArtifactError("媒体时间必须是非负有限数")
    return f"{number:.6f}"


def build_working_master_command(
    source_path: str,
    output_path: str,
    timeline: Mapping[str, Any],
    *,
    ffmpeg_binary: str = "ffmpeg",
) -> list[str]:
    """Build a shell-free FFmpeg command from a validated source timeline."""

    rows = timeline.get("segments")
    if not isinstance(rows, list) or not rows:
        raise MediaArtifactError("原→工作母版映射缺少区间")
    kept = [row for row in rows if isinstance(row, Mapping) and row.get("kind") == "keep"]
    if not kept:
        raise MediaArtifactError("广告剪辑不能删除全部画面")
    common = [
        str(ffmpeg_binary),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-n",
        "-i",
        source_path,
    ]
    if timeline.get("identity") is True:
        # A remux creates independent bytes/inode while preserving the source.
        # Hard links and source-path reuse are explicitly forbidden.
        return [
            *common,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map_metadata",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ]

    chains: list[str] = []
    concat_inputs: list[str] = []
    for index, row in enumerate(kept):
        start = _format_seconds(row.get("source_start"))
        end = _format_seconds(row.get("source_end"))
        chains.extend(
            [
                f"[0:v:0]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]",
                f"[0:a:0]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]",
            ]
        )
        concat_inputs.extend((f"[v{index}]", f"[a{index}]"))
    chains.append(
        "".join(concat_inputs)
        + f"concat=n={len(kept)}:v=1:a=1[vout][aout]"
    )
    return [
        *common,
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-map_metadata",
        "0",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        output_path,
    ]


def build_cover_source_command(
    working_master_path: str,
    output_path: str,
    *,
    at_seconds: float,
    ffmpeg_binary: str = "ffmpeg",
) -> list[str]:
    """Build a shell-free, exact-seek PNG extraction command."""

    return [
        str(ffmpeg_binary),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-n",
        "-i",
        working_master_path,
        "-ss",
        _format_seconds(at_seconds),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-f",
        "image2",
        output_path,
    ]


def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaArtifactError(f"{label}执行失败: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        # stderr can contain absolute paths or signed inputs; keep it out of
        # task state and expose only the stable exit code.
        raise MediaArtifactError(f"{label}返回非零退出码 {completed.returncode}")
    return completed


def _probe_media(
    root: Path,
    path: Path,
    *,
    ffprobe_binary: str,
) -> dict[str, Any]:
    relative = _relative(root, path)
    completed = _run_process(
        [
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
        ],
        cwd=root,
        timeout_seconds=120,
        label="ffprobe",
    )
    return parse_ffprobe_payload(completed.stdout, require_audio=True)


def _decode_media(
    root: Path,
    path: Path,
    *,
    ffmpeg_binary: str,
) -> None:
    _run_process(
        [
            str(ffmpeg_binary),
            "-hide_banner",
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            _relative(root, path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            os.devnull,
        ],
        cwd=root,
        timeout_seconds=6 * 60 * 60,
        label="FFmpeg 全片解码",
    )


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise MediaArtifactError("封面源帧不是可解码的 PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise MediaArtifactError("封面源帧尺寸无效")
    return width, height


def _text_projection(document: Mapping[str, Any]) -> bytes:
    slots = document.get("slots")
    if not isinstance(slots, list):
        raise MediaArtifactError("冻结源文缺少 slots")
    lines = []
    for row in slots:
        if not isinstance(row, Mapping):
            raise MediaArtifactError("冻结源文包含无效时间槽")
        lines.append(
            "\t".join(
                (
                    str(row.get("id") or ""),
                    _format_seconds(row.get("start")),
                    _format_seconds(row.get("end")),
                    str(row.get("source_text") or "").replace("\t", " ").replace("\n", " "),
                )
            )
        )
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _map_frozen_source_to_working(
    root: Path,
    source_path: Path,
    output_path: Path,
    timeline: Mapping[str, Any],
    working_master: Path,
) -> dict[str, Any]:
    document = _load_json_object(source_path, "冻结源文")
    slots = document.get("slots")
    if not isinstance(slots, list) or not slots:
        raise MediaArtifactError("冻结源文没有可映射的时间槽")
    removed = [
        row
        for row in (timeline.get("segments") or [])
        if isinstance(row, Mapping) and row.get("kind") == "remove"
    ]
    mapped: list[dict[str, Any]] = []
    for raw in slots:
        if not isinstance(raw, Mapping):
            raise MediaArtifactError("冻结源文包含无效时间槽")
        start = float(raw.get("start"))
        end = float(raw.get("end"))
        overlaps = [
            row
            for row in removed
            if start < float(row["source_end"]) and end > float(row["source_start"])
        ]
        if overlaps:
            fully_removed = any(
                start >= float(row["source_start"]) - 0.000001
                and end <= float(row["source_end"]) + 0.000001
                for row in overlaps
            )
            if fully_removed:
                continue
            raise MediaArtifactError(
                f"字幕槽 {raw.get('id')} 跨越广告删除边界；请先修正边界或重新转写工作母版"
            )
        mapped_row = dict(raw)
        mapped_row["start"] = map_source_time(timeline, start)
        mapped_row["end"] = map_source_time(timeline, end)
        mapped_row["position"] = len(mapped)
        mapped.append(mapped_row)
    if not mapped:
        raise MediaArtifactError("广告删除后没有剩余源文时间槽")
    mapped_document = {
        **document,
        "schema_version": max(1, int(document.get("schema_version") or 1)),
        "kind": "frozen_working_master_transcript",
        "status": "pass",
        "slot_count": len(mapped),
        "stable_ids": [str(row.get("id") or "") for row in mapped],
        "slots": mapped,
        "based_on_working_master": True,
        "working_master": _artifact(root, working_master),
        "source_to_working_timeline_sha256": canonical_sha256(timeline),
    }
    _write_json_once_or_same(output_path, mapped_document)
    return mapped_document


class MediaArtifactJobs:
    """Handlers registered with :class:`LocalTaskRunner`."""

    def __init__(
        self,
        database: WorkbenchDatabase,
        library: ProjectLibrary,
        settings: WorkbenchSettings,
        artifact_service: WorkflowArtifactService,
    ) -> None:
        self.database = database
        self.library = library
        self.settings = settings
        self.artifact_service = artifact_service

    def _scope(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any], Path]:
        run_id = str(payload.get("run_id") or "")
        run = self.database.get_run(run_id)
        if run is None:
            raise MediaArtifactError("运行不存在")
        project = self.database.get_project(run["project_id"])
        if project is None:
            raise MediaArtifactError("项目不存在")
        root = self.library.path_for(project).resolve()
        lock_path = root / "qa" / "workflow_lock.json"
        validation = validate_gate_file(lock_path, kind="workflow_lock", artifact_root=root)
        if not validation.valid:
            raise MediaArtifactError("workflow_lock 未通过：" + "; ".join(validation.errors))
        lock = _load_json_object(lock_path, "工作流锁")
        if lock.get("run_id") != run_id or lock.get("project_id") != project["id"]:
            raise MediaArtifactError("工作流锁不属于当前运行")
        return run_id, project, root

    @staticmethod
    def _completed_report(
        root: Path,
        path: Path,
        *,
        input_signature: str,
        artifact_keys: Sequence[str],
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        report = _load_json_object(path, "媒体任务报告")
        if report.get("status") != "pass" or report.get("input_signature") != input_signature:
            raise MediaArtifactError("已有媒体任务报告与当前冻结输入不一致")
        for key in artifact_keys:
            ref = report.get(key)
            if not isinstance(ref, Mapping):
                raise MediaArtifactError(f"媒体任务报告缺少 {key}")
            artifact_path = safe_project_path(
                root, str(ref.get("path") or ""), must_exist=True, require_file=True
            )
            if sha256_file(artifact_path) != ref.get("sha256"):
                raise MediaArtifactError(f"媒体任务报告中的 {key} 哈希不匹配")
        return report

    def extract_embedded_subtitle_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        run_id, project, root = self._scope(payload)
        if (root / "qa" / "ad_edit_gate.json").exists():
            raise MediaArtifactError("广告门禁已冻结；不能再替换内嵌源字幕")
        version = int(payload.get("version") or 1)
        if version < 1:
            raise MediaArtifactError("字幕版本必须大于 0")
        source = safe_project_path(
            root,
            str(payload.get("source_path") or ""),
            must_exist=True,
            require_file=True,
        )
        output_value = str(payload.get("output_path") or f"work/embedded_source_v{version}.srt")
        output = safe_project_path(root, output_value)
        if output.suffix.lower() not in {".srt", ".vtt"}:
            raise MediaArtifactError("内嵌字幕输出必须是 .srt 或 .vtt")
        frozen_path = safe_project_path(root, f"work/frozen_source_v{version}.json")
        text_path = safe_project_path(root, f"work/frozen_source_v{version}.txt")
        plan_path = safe_project_path(root, f"qa/embedded_subtitle_plan_v{version}.json")
        report_path = safe_project_path(root, f"qa/embedded_subtitle_extraction_v{version}.json")
        signature_payload = {
            "operation": "extract_embedded_subtitle",
            "source": {"path": _relative(root, source), "sha256": sha256_file(source)},
            "output_path": _relative(root, output),
            "stream_index": int(payload.get("stream_index") or 0),
            "language": str(payload.get("language") or "") or None,
            "version": version,
        }
        input_signature = canonical_sha256(signature_payload)
        reused = self._completed_report(
            root,
            report_path,
            input_signature=input_signature,
            artifact_keys=("subtitle", "frozen_source", "text_projection"),
        )
        if reused is not None:
            progress.exact(3, 3, "steps", "已复用通过哈希校验的内嵌字幕制品")
            return {"detail": "内嵌字幕制品已存在并通过哈希校验", "reused": True, **reused}

        plan = build_embedded_subtitle_plan(
            source,
            output,
            stream_index=signature_payload["stream_index"],
            project_root=root,
            ffmpeg_binary=self.settings.ffmpeg,
            allow_existing=True,
        )
        plan_document = {
            "schema_version": 1,
            "status": "ready",
            "input_signature": input_signature,
            "source": signature_payload["source"],
            "stream_index": signature_payload["stream_index"],
            "output_path": _relative(root, output),
            "command": {
                "kind": plan.kind,
                "shell": False,
                "overwrite": False,
                "ffmpeg_binary": Path(self.settings.ffmpeg).name,
            },
        }
        _write_json_once_or_same(plan_path, plan_document)
        token.checkpoint()
        progress.exact(1, 3, "steps", "正在提取内嵌字幕")
        if not output.is_file():
            try:
                _run_process(
                    plan.argv,
                    cwd=root,
                    timeout_seconds=30 * 60,
                    label="FFmpeg 字幕提取",
                )
            except Exception:
                if output.exists():
                    output.unlink()
                raise
        if not output.is_file() or output.stat().st_size <= 0:
            raise MediaArtifactError("FFmpeg 未生成字幕文件")

        token.checkpoint()
        document = import_source_transcript(
            output,
            project_root=root,
            language=str(payload.get("language") or "") or None,
        )
        _write_json_once_or_same(frozen_path, document)
        _write_bytes_once_or_same(text_path, _text_projection(document))
        progress.exact(2, 3, "steps", "字幕已解析并冻结稳定 ID")
        report = {
            "schema_version": 1,
            "status": "pass",
            "kind": "embedded_subtitle_extraction",
            "input_signature": input_signature,
            "stream_index": signature_payload["stream_index"],
            "source_language": signature_payload["language"],
            "slot_count": int(document["slot_count"]),
            "source_master": _artifact(root, source),
            "subtitle": _artifact(root, output),
            "frozen_source": _artifact(root, frozen_path),
            "text_projection": _artifact(root, text_path),
            "plan": _artifact(root, plan_path),
        }
        _write_json_once_or_same(report_path, report)
        progress.exact(3, 3, "steps", "内嵌字幕、文本投影与冻结源文已完成")
        self.database.emit_event(
            "ingest.embedded_subtitle_frozen",
            {
                "report_path": _relative(root, report_path),
                "frozen_source_path": _relative(root, frozen_path),
                "slot_count": report["slot_count"],
            },
            project_id=project["id"],
            run_id=run_id,
        )
        return {
            "detail": "内嵌字幕已提取并冻结为可审计文本制品",
            "reused": False,
            "report": _artifact(root, report_path),
            **report,
        }

    def ad_edit_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        run_id, _project, root = self._scope(payload)
        if (root / "qa" / "translation_gate.json").exists():
            raise MediaArtifactError("翻译门禁已冻结；不能再重建广告工作母版")
        version = int(payload.get("version") or 1)
        source = safe_project_path(
            root,
            str(payload.get("source_master_path") or ""),
            must_exist=True,
            require_file=True,
        )
        working = safe_project_path(root, str(payload.get("working_master_path") or ""))
        if source.resolve(strict=False) == working.resolve(strict=False):
            raise PathSafetyError("工作母版不能覆盖原始母版")
        if working.suffix.lower() != ".mp4":
            raise MediaArtifactError("正式工作母版必须输出为 .mp4")
        analysis = payload.get("analysis")
        if not isinstance(analysis, Mapping):
            raise AdEvidenceError("广告证据必须是 JSON 对象")
        duration = float(payload.get("source_duration") or 0)
        validation = validate_ad_analysis(
            analysis,
            source_duration=duration,
            frame_width=(int(payload["frame_width"]) if payload.get("frame_width") else None),
            frame_height=(int(payload["frame_height"]) if payload.get("frame_height") else None),
        )
        if validation.status == "analysis_required":
            raise MediaArtifactError(
                "广告证据尚未完成：" + ", ".join(validation.blockers)
            )
        if not validation.ok or validation.normalized is None:
            raise AdEvidenceError("; ".join(validation.errors) or "广告证据校验失败")
        normalized = dict(validation.normalized)
        removed = [
            row for row in normalized["candidates"] if row.get("action") == "remove"
        ]
        timeline = build_source_to_edit_timeline(duration, removed)
        frozen_input_value = str(payload.get("frozen_source_path") or "").strip()
        if not frozen_input_value:
            raise MediaArtifactError("广告工作母版任务需要已冻结源文，以生成工作母版时间轴版本")
        frozen_input = safe_project_path(
            root, frozen_input_value, must_exist=True, require_file=True
        )
        frozen_working = safe_project_path(root, f"work/frozen_source_working_v{version}.json")
        plan_path = safe_project_path(root, "qa/ad_media_plan.json")
        report_path = safe_project_path(root, "qa/ad_media_execution.json")
        signature_payload = {
            "operation": "build_ad_working_master",
            "source": {"path": _relative(root, source), "sha256": sha256_file(source)},
            "working_path": _relative(root, working),
            "analysis_sha256": canonical_sha256(normalized),
            "timeline_sha256": canonical_sha256(timeline),
            "frozen_source": {
                "path": _relative(root, frozen_input),
                "sha256": sha256_file(frozen_input),
            },
            "version": version,
        }
        input_signature = canonical_sha256(signature_payload)
        reused = self._completed_report(
            root,
            report_path,
            input_signature=input_signature,
            artifact_keys=("working_master", "ad_edit_gate", "frozen_working_source"),
        )
        if reused is not None:
            gate_validation = validate_gate_file(
                root / "qa" / "ad_edit_gate.json",
                kind="ad_edit_gate",
                artifact_root=root,
            )
            if not gate_validation.valid:
                raise MediaArtifactError("已有广告门禁失效：" + "; ".join(gate_validation.errors))
            progress.exact(4, 4, "steps", "已复用通过哈希校验的广告工作母版")
            return {"detail": "广告工作母版与门禁已存在并通过哈希校验", "reused": True, **reused}

        command = build_working_master_command(
            _relative(root, source),
            _relative(root, working),
            timeline,
            ffmpeg_binary=self.settings.ffmpeg,
        )
        plan_document = {
            "schema_version": 1,
            "status": "ready",
            "input_signature": input_signature,
            **signature_payload,
            "decision": normalized["decision"],
            "removed_segments": [
                {"id": row["id"], "start": row["start"], "end": row["end"]}
                for row in removed
            ],
            "command": {
                "kind": "remux" if timeline["identity"] else "trim_concat",
                "shell": False,
                "overwrite": False,
                "ffmpeg_binary": Path(self.settings.ffmpeg).name,
            },
        }
        _write_json_once_or_same(plan_path, plan_document)
        token.checkpoint()
        progress.exact(1, 4, "steps", "正在生成独立工作母版")
        source_before = sha256_file(source)
        if not working.is_file():
            working.parent.mkdir(parents=True, exist_ok=True)
            try:
                _run_process(
                    command,
                    cwd=root,
                    timeout_seconds=12 * 60 * 60,
                    label="FFmpeg 工作母版生成",
                )
            except Exception:
                if working.exists():
                    working.unlink()
                raise
        if not working.is_file() or working.stat().st_size <= 0:
            raise MediaArtifactError("FFmpeg 未生成正式工作母版")
        if sha256_file(source) != source_before:
            raise MediaArtifactError("原始母版在工作母版生成期间发生变化")
        try:
            if os.path.samefile(source, working):
                raise MediaArtifactError("正式工作母版不能是原母版的硬链接")
        except OSError as exc:
            raise MediaArtifactError("无法验证工作母版是否独立") from exc

        token.checkpoint()
        observed = _probe_media(root, working, ffprobe_binary=self.settings.ffprobe)
        expected_duration = float(timeline["working_duration_seconds"])
        tolerance = max(0.5, expected_duration * 0.001)
        if abs(float(observed["duration_seconds"]) - expected_duration) > tolerance:
            raise MediaArtifactError("工作母版时长与广告删除映射不一致")
        _decode_media(root, working, ffmpeg_binary=self.settings.ffmpeg)
        progress.exact(2, 4, "steps", "工作母版已通过探测与全片解码")

        mapped_document = _map_frozen_source_to_working(
            root,
            frozen_input,
            frozen_working,
            timeline,
            working,
        )
        progress.exact(3, 4, "steps", "冻结源文已映射到正式工作母版")
        existing_gate = root / "qa" / "ad_edit_gate.json"
        if existing_gate.exists():
            expected_bundle = build_ad_edit_artifacts(
                project_root=root,
                source_master=source,
                working_master=working,
                analysis=analysis,
                source_duration=duration,
                frame_width=(int(payload["frame_width"]) if payload.get("frame_width") else None),
                frame_height=(int(payload["frame_height"]) if payload.get("frame_height") else None),
                frozen_source_path=frozen_working,
            )
            materialize_ad_edit_artifacts(expected_bundle, project_root=root)
            gate_validation = validate_gate_file(
                existing_gate, kind="ad_edit_gate", artifact_root=root
            )
            if not gate_validation.valid:
                raise MediaArtifactError(
                    "已有广告门禁与本次媒体输入不一致："
                    + "; ".join(gate_validation.errors)
                )
            gate_result = {
                "status": "pass",
                "decision": expected_bundle["ad_edit_gate"]["decision"],
                "gate": _artifact(root, existing_gate),
            }
        else:
            gate_result = self.artifact_service.apply_ad_evidence(
                run_id,
                source_master_path=_relative(root, source),
                working_master_path=_relative(root, working),
                analysis=analysis,
                source_duration=duration,
                frame_width=(int(payload["frame_width"]) if payload.get("frame_width") else None),
                frame_height=(int(payload["frame_height"]) if payload.get("frame_height") else None),
                frozen_source_path=_relative(root, frozen_working),
            )
        gate_path = root / "qa" / "ad_edit_gate.json"
        report = {
            "schema_version": 1,
            "status": "pass",
            "kind": "ad_working_master",
            "input_signature": input_signature,
            "decision": normalized["decision"],
            "execution": plan_document["command"]["kind"],
            "source_master": _artifact(root, source),
            "working_master": _artifact(root, working),
            "frozen_working_source": _artifact(root, frozen_working),
            "mapped_slot_count": int(mapped_document["slot_count"]),
            "ad_edit_gate": _artifact(root, gate_path),
            "source_to_working": _artifact(root, root / "qa" / "source_to_edit_timeline.json"),
            "plan": _artifact(root, plan_path),
            "observed": {
                "duration_seconds": observed["duration_seconds"],
                "width": observed["video"]["width"],
                "height": observed["video"]["height"],
            },
        }
        _write_json_once_or_same(report_path, report)
        progress.exact(4, 4, "steps", "广告工作母版、时间映射与门禁已完成")
        return {
            "detail": "已按冻结广告证据生成独立工作母版并通过 ad_edit_gate",
            "reused": False,
            "report": _artifact(root, report_path),
            "gate": gate_result,
            **report,
        }

    def cover_source_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        run_id, project, root = self._scope(payload)
        gate_path = root / "qa" / "ad_edit_gate.json"
        gate_validation = validate_gate_file(
            gate_path, kind="ad_edit_gate", artifact_root=root
        )
        if not gate_validation.valid:
            raise MediaArtifactError("ad_edit_gate 未通过：" + "; ".join(gate_validation.errors))
        gate = _load_json_object(gate_path, "广告门禁")
        working = safe_project_path(
            root,
            str(payload.get("working_master_path") or ""),
            must_exist=True,
            require_file=True,
        )
        bound = gate.get("working_master")
        if not isinstance(bound, Mapping):
            raise MediaArtifactError("广告门禁缺少正式工作母版绑定")
        bound_path = safe_project_path(
            root, str(bound.get("path") or ""), must_exist=True, require_file=True
        )
        if bound_path.resolve() != working.resolve() or sha256_file(working) != bound.get("sha256"):
            raise MediaArtifactError("封面源帧必须从广告门禁绑定的正式工作母版抽取")
        version = int(payload.get("version") or 1)
        output_value = str(payload.get("output_path") or f"work/cover_source_v{version}.png")
        output = safe_project_path(root, output_value)
        if output.suffix.lower() != ".png":
            raise MediaArtifactError("封面源帧输出必须是 PNG")
        at_seconds = float(payload.get("at_seconds") or 0)
        if at_seconds < 0:
            raise MediaArtifactError("封面抽帧时间不能为负数")
        plan_path = safe_project_path(root, f"qa/cover_source_plan_v{version}.json")
        report_path = safe_project_path(root, f"qa/cover_source_v{version}.json")
        signature_payload = {
            "operation": "extract_cover_source",
            "working_master": {"path": _relative(root, working), "sha256": sha256_file(working)},
            "ad_edit_gate_sha256": sha256_file(gate_path),
            "at_seconds": round(at_seconds, 6),
            "output_path": _relative(root, output),
            "version": version,
        }
        input_signature = canonical_sha256(signature_payload)
        reused = self._completed_report(
            root,
            report_path,
            input_signature=input_signature,
            artifact_keys=("cover_source", "working_master", "ad_edit_gate"),
        )
        if reused is not None:
            progress.exact(2, 2, "steps", "已复用通过哈希校验的封面源帧")
            return {"detail": "封面源帧已存在并通过哈希校验", "reused": True, **reused}

        observed = _probe_media(root, working, ffprobe_binary=self.settings.ffprobe)
        if at_seconds >= float(observed["duration_seconds"]):
            raise MediaArtifactError("封面抽帧时间必须小于工作母版时长")
        command = build_cover_source_command(
            _relative(root, working),
            _relative(root, output),
            at_seconds=at_seconds,
            ffmpeg_binary=self.settings.ffmpeg,
        )
        plan_document = {
            "schema_version": 1,
            "status": "ready",
            "input_signature": input_signature,
            **signature_payload,
            "command": {
                "kind": "media.extract_cover_source",
                "shell": False,
                "overwrite": False,
                "ffmpeg_binary": Path(self.settings.ffmpeg).name,
            },
        }
        _write_json_once_or_same(plan_path, plan_document)
        token.checkpoint()
        progress.exact(1, 2, "steps", "正在从正式工作母版抽取封面源帧")
        if not output.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                _run_process(
                    command,
                    cwd=root,
                    timeout_seconds=10 * 60,
                    label="FFmpeg 封面抽帧",
                )
            except Exception:
                if output.exists():
                    output.unlink()
                raise
        if not output.is_file() or output.stat().st_size <= 0:
            raise MediaArtifactError("FFmpeg 未生成封面源帧")
        width, height = _png_dimensions(output)
        report = {
            "schema_version": 1,
            "status": "pass",
            "kind": "cover_source_frame",
            "input_signature": input_signature,
            "at_seconds": round(at_seconds, 6),
            "working_master": _artifact(root, working),
            "ad_edit_gate": _artifact(root, gate_path),
            "cover_source": {**_artifact(root, output), "width": width, "height": height},
            "plan": _artifact(root, plan_path),
            "authorization_note": "源帧抽取不代表发布授权；发布包仍需显式核对授权、人物、文字与广告残留。",
        }
        _write_json_once_or_same(report_path, report)
        progress.exact(2, 2, "steps", "封面源帧已抽取并完成哈希冻结")
        self.database.emit_event(
            "publication.cover_source_ready",
            {
                "report_path": _relative(root, report_path),
                "cover_source_path": _relative(root, output),
            },
            project_id=project["id"],
            run_id=run_id,
        )
        return {
            "detail": "已从正式工作母版抽取 PNG 封面源帧",
            "reused": False,
            "report": _artifact(root, report_path),
            **report,
        }


__all__ = [
    "MediaArtifactError",
    "MediaArtifactJobs",
    "build_cover_source_command",
    "build_working_master_command",
]
