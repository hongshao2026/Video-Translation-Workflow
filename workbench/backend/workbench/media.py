"""Video probing, automatic source-format selection and resumable downloading."""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .database import WorkbenchDatabase
from .ingest import canonical_json_bytes, import_source_transcript
from .library import ProjectLibrary, atomic_json, sha256_file
from .runner import ProgressReporter, TaskToken
from .settings import WorkbenchSettings


class MediaToolError(RuntimeError):
    pass


def _number(value: object, default: float = 0) -> float:
    try:
        result = float(value or default)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _language_score(value: Mapping[str, Any], expected_language: str | None) -> int:
    language = str(value.get("language") or "").lower()
    expected = str(expected_language or "").lower()
    note = " ".join(
        str(value.get(key) or "")
        for key in ("format_note", "language_preference", "name")
    ).lower()
    score = 0
    if expected and (language == expected or language.startswith(expected + "-") or expected.startswith(language + "-")):
        score += 100
    if value.get("is_original") or "original" in note or "default" in note:
        score += 20
    return score


def select_formats(info: Mapping[str, Any], expected_language: str | None = None) -> dict[str, Any]:
    formats = [row for row in info.get("formats", []) if isinstance(row, Mapping)]
    videos = [
        row
        for row in formats
        if str(row.get("vcodec") or "none") != "none"
        and str(row.get("acodec") or "none") == "none"
    ]
    audios = [
        row
        for row in formats
        if str(row.get("acodec") or "none") != "none"
        and str(row.get("vcodec") or "none") == "none"
    ]
    if not videos:
        videos = [row for row in formats if str(row.get("vcodec") or "none") != "none"]
    if not audios:
        audios = [row for row in formats if str(row.get("acodec") or "none") != "none"]
    if not videos or not audios:
        raise MediaToolError("格式探测结果缺少可用的视频流或音频流")

    def video_rank(row: Mapping[str, Any]) -> tuple[float, int, float, float]:
        codec = str(row.get("vcodec") or "").lower()
        extension = str(row.get("ext") or "").lower()
        compatibility = int(extension == "mp4" and codec.startswith(("avc", "h264")))
        return (
            _number(row.get("height")),
            compatibility,
            _number(row.get("fps")),
            _number(row.get("tbr")),
        )

    def audio_rank(row: Mapping[str, Any]) -> tuple[int, int, float, float]:
        codec = str(row.get("acodec") or "").lower()
        extension = str(row.get("ext") or "").lower()
        compatibility = int(extension in {"m4a", "mp4"} or codec.startswith("mp4a") or codec == "aac")
        return (
            _language_score(row, expected_language),
            compatibility,
            _number(row.get("abr")),
            _number(row.get("asr")),
        )

    video = max(videos, key=video_rank)
    audio = max(audios, key=audio_rank)
    return {
        "policy": "source_language_resolution_compatibility_fps_bitrate",
        "manual_approval_required": False,
        "video": {
            "format_id": str(video.get("format_id")),
            "ext": video.get("ext"),
            "vcodec": video.get("vcodec"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": video.get("fps"),
            "tbr": video.get("tbr"),
        },
        "audio": {
            "format_id": str(audio.get("format_id")),
            "ext": audio.get("ext"),
            "acodec": audio.get("acodec"),
            "language": audio.get("language"),
            "abr": audio.get("abr"),
            "is_original": bool(audio.get("is_original")),
        },
        "expected_language": expected_language,
        "source_language_verified": bool(
            not expected_language or _language_score(audio, expected_language) >= 100
        ),
    }


def _language_match_score(candidate: str, expected: str) -> int:
    candidate = candidate.lower().replace("_", "-")
    expected = expected.lower().replace("_", "-")
    if not candidate or not expected:
        return 0
    if candidate == expected:
        return 3
    if candidate.startswith(expected + "-") or expected.startswith(candidate + "-"):
        return 2
    if candidate.split("-", 1)[0] == expected.split("-", 1)[0]:
        return 1
    return 0


def select_subtitle_track(
    info: Mapping[str, Any],
    expected_language: str | None,
) -> dict[str, Any]:
    """Freeze one source-language subtitle choice without persisting URLs.

    Manual subtitles win over automatic captions.  When no source language is
    known, a track is selected only if one source has exactly one language;
    ambiguous language sets stay explicit instead of silently choosing one.
    """

    sources: list[tuple[str, Mapping[str, Any]]] = []
    for kind, key in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        value = info.get(key)
        sources.append((kind, value if isinstance(value, Mapping) else {}))

    target = str(expected_language or info.get("language") or "").strip()
    selected: tuple[str, str, object] | None = None
    if target:
        for kind, tracks in sources:
            ranked = sorted(
                (
                    (_language_match_score(str(language), target), str(language), rows)
                    for language, rows in tracks.items()
                ),
                reverse=True,
            )
            if ranked and ranked[0][0] > 0:
                _, language, rows = ranked[0]
                selected = (kind, language, rows)
                break
    else:
        for kind, tracks in sources:
            if len(tracks) == 1:
                language, rows = next(iter(tracks.items()))
                selected = (kind, str(language), rows)
                break

    available = {
        kind: sorted(str(language) for language in tracks)
        for kind, tracks in sources
    }
    if selected is None:
        return {
            "status": "needs_import_or_device_asr",
            "reason": "source_language_track_unavailable_or_ambiguous",
            "expected_language": target or None,
            "available_languages": available,
        }

    kind, language, rows = selected
    formats: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            extension = str(row.get("ext") or "").lower()
            if extension and extension not in formats:
                formats.append(extension)
    return {
        "status": "selected",
        "kind": kind,
        "language": language,
        "expected_language": target or language,
        "available_formats": formats,
        "requested_format": "srt/vtt/best",
        "available_languages": available,
    }


def _cookie_path(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise MediaToolError("Cookie 文件不存在")
    # Validate structure without exposing any cookie values.
    first = path.open("r", encoding="utf-8", errors="ignore").readline().strip()
    if "Netscape HTTP Cookie File" not in first:
        raise MediaToolError("Cookie 文件不是 Netscape 格式")
    return str(path)


def probe_remote(
    url: str,
    *,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    try:
        import yt_dlp  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MediaToolError("尚未安装 yt-dlp") from exc
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    cookie_path = _cookie_path(cookie_file)
    if cookie_path:
        options["cookiefile"] = cookie_path
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise MediaToolError(f"视频探测失败：{type(exc).__name__}") from exc
    if not isinstance(result, dict):
        raise MediaToolError("视频探测没有返回元数据")
    # Never persist temporary media URLs, cookies or opaque extractor secrets.
    safe = {
        key: result.get(key)
        for key in (
            "id",
            "title",
            "description",
            "duration",
            "width",
            "height",
            "fps",
            "uploader",
            "channel",
            "webpage_url",
            "original_url",
            "language",
            "chapters",
            "subtitles",
            "automatic_captions",
            "formats",
        )
    }
    return safe


def probe_local(path: Path, ffprobe: str = "ffprobe") -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaToolError(f"本地视频探测失败：{type(exc).__name__}") from exc
    if result.returncode != 0:
        raise MediaToolError("ffprobe 无法读取本地视频")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaToolError("ffprobe 返回了无效 JSON") from exc
    streams = payload.get("streams") or []
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio = next((row for row in streams if row.get("codec_type") == "audio"), {})
    return {
        "id": path.stem,
        "title": path.stem,
        "duration": _number((payload.get("format") or {}).get("duration")),
        "width": video.get("width"),
        "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "audio_language": (audio.get("tags") or {}).get("language"),
        "byte_size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def download_remote(
    url: str,
    *,
    destination: Path,
    selection: Mapping[str, Any],
    progress: ProgressReporter,
    token: TaskToken,
    cookie_file: str | None = None,
    subtitle_selection: Mapping[str, Any] | None = None,
) -> tuple[Path, Path | None]:
    try:
        import yt_dlp  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MediaToolError("尚未安装 yt-dlp") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    video_id = str((selection.get("video") or {}).get("format_id") or "")
    audio_id = str((selection.get("audio") or {}).get("format_id") or "")
    if not video_id or not audio_id:
        raise MediaToolError("下载计划缺少画面或音频格式 ID")

    def hook(value: Mapping[str, Any]) -> None:
        token.checkpoint()
        if value.get("status") == "downloading":
            downloaded = _number(value.get("downloaded_bytes"))
            total = _number(value.get("total_bytes") or value.get("total_bytes_estimate"))
            if total > 0:
                progress.exact(downloaded, total, "bytes", "正在下载源媒体")
            else:
                progress.indeterminate("正在下载源媒体；服务端未提供总大小")
        elif value.get("status") == "finished":
            progress.indeterminate("下载完成，正在合并并校验")

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"{video_id}+{audio_id}",
        "outtmpl": str(destination.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "continuedl": True,
        "overwrites": False,
        "progress_hooks": [hook],
    }
    if subtitle_selection and subtitle_selection.get("status") == "selected":
        language = str(subtitle_selection.get("language") or "")
        if not language:
            raise MediaToolError("字幕选择缺少冻结语言")
        options.update(
            {
                "writesubtitles": subtitle_selection.get("kind") == "manual",
                "writeautomaticsub": subtitle_selection.get("kind") == "automatic",
                "subtitleslangs": [language],
                "subtitlesformat": "srt/vtt/best",
                "convertsubtitles": "srt",
            }
        )
    cookie_path = _cookie_path(cookie_file)
    if cookie_path:
        options["cookiefile"] = cookie_path
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise MediaToolError(f"视频下载失败：{type(exc).__name__}") from exc
    candidates = [destination.with_suffix(".mp4"), prepared, prepared.with_suffix(".mp4")]
    output = next((path for path in candidates if path.is_file()), None)
    if output is None:
        raise MediaToolError("下载完成但没有找到合并后的文件")
    subtitle: Path | None = None
    if subtitle_selection and subtitle_selection.get("status") == "selected":
        candidates = sorted(
            (
                path
                for path in destination.parent.glob(f"{destination.name}.*")
                if path.is_file() and path.suffix.lower() in {".srt", ".vtt"}
            ),
            key=lambda path: (path.suffix.lower() != ".srt", path.name),
        )
        subtitle = candidates[0] if candidates else None
    return output, subtitle


def _write_bytes_once_or_same(path: Path, data: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise MediaToolError(f"版本化字幕制品已存在且内容不同: {path.name}")
        return
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


def _freeze_downloaded_subtitle(
    subtitle: Path,
    *,
    run_dir: Path,
    language: str | None,
    version: int = 1,
) -> dict[str, Any]:
    extension = subtitle.suffix.lower()
    target = run_dir / "work" / f"downloaded_source_v{version}{extension}"
    if subtitle.resolve() != target.resolve():
        data = subtitle.read_bytes()
        _write_bytes_once_or_same(target, data)
        try:
            subtitle.unlink()
        except OSError:
            pass
    document = import_source_transcript(target, project_root=run_dir, language=language)
    frozen = run_dir / "work" / f"frozen_source_v{version}.json"
    text = run_dir / "work" / f"frozen_source_v{version}.txt"
    _write_bytes_once_or_same(frozen, canonical_json_bytes(document))
    lines = [
        "\t".join(
            (
                str(row["id"]),
                f"{float(row['start']):.6f}",
                f"{float(row['end']):.6f}",
                str(row["source_text"]).replace("\t", " ").replace("\n", " "),
            )
        )
        for row in document["slots"]
    ]
    _write_bytes_once_or_same(text, ("\n".join(lines) + "\n").encode("utf-8"))
    return {
        "status": "frozen",
        "language": language,
        "subtitle": {
            "path": target.relative_to(run_dir).as_posix(),
            "sha256": sha256_file(target),
        },
        "frozen_source": {
            "path": frozen.relative_to(run_dir).as_posix(),
            "sha256": sha256_file(frozen),
        },
        "text_projection": {
            "path": text.relative_to(run_dir).as_posix(),
            "sha256": sha256_file(text),
        },
        "slot_count": int(document["slot_count"]),
    }


class MediaService:
    def __init__(
        self,
        database: WorkbenchDatabase,
        library: ProjectLibrary,
        settings: WorkbenchSettings,
    ) -> None:
        self.database = database
        self.library = library
        self.settings = settings

    def probe_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        project_id = str(payload["project_id"])
        project = self.database.get_project(project_id)
        if project is None:
            raise MediaToolError("项目不存在")
        token.checkpoint()
        progress.indeterminate("正在读取视频元数据")
        expected_language = str(payload.get("expected_language") or "") or None
        if project["source_kind"] == "video_url":
            info = probe_remote(str(project["source"]), cookie_file=payload.get("cookie_file"))
            selection = select_formats(info, expected_language)
            source_language = (
                expected_language
                or str((selection.get("audio") or {}).get("language") or "")
                or str(info.get("language") or "")
                or None
            )
            subtitle_selection = select_subtitle_track(info, source_language)
            report = {
                "schema_version": 1,
                "status": "pass" if selection["source_language_verified"] else "repair_required",
                "format_probe_pass": True,
                "video_id": info.get("id"),
                "title": info.get("title"),
                "duration": info.get("duration"),
                "selection": selection,
                "subtitle_selection": subtitle_selection,
            }
        else:
            source = Path(str(project["source"]))
            info = probe_local(source, self.settings.ffprobe)
            report = {
                "schema_version": 1,
                "status": "pass",
                "format_probe_pass": True,
                "local_source": info,
                "selection": {"manual_approval_required": False, "policy": "local_source_as_provided"},
            }
        token.checkpoint()
        run_dir = self.library.path_for(project)
        qa_path = run_dir / "qa" / "media_format_selection_v1.json"
        atomic_json(qa_path, report)
        metadata = dict(project.get("metadata") or {})
        metadata["media_probe"] = {
            "status": report["status"],
            "qa_path": str(qa_path.relative_to(run_dir)),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
        }
        self.database.update_project(
            project_id,
            title=str(info.get("title") or project["title"]),
            status="queued" if report["status"] == "pass" else "repair_required",
            current_stage="02",
            progress_mode="gate",
            needs_attention=report["status"] != "pass",
            metadata=metadata,
        )
        return {
            "detail": "视频探测与自动格式选择完成",
            "qa_path": str(qa_path.relative_to(run_dir)),
            "status": report["status"],
        }

    def download_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        project_id = str(payload["project_id"])
        project = self.database.get_project(project_id)
        if project is None or project["source_kind"] != "video_url":
            raise MediaToolError("只有链接项目需要下载")
        run_dir = self.library.path_for(project)
        report_path = run_dir / "qa" / "media_format_selection_v1.json"
        if not report_path.is_file():
            raise MediaToolError("请先完成视频探测和自动格式选择")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") != "pass":
            raise MediaToolError("媒体格式门禁尚未通过")
        subtitle_selection = report.get("subtitle_selection")
        output, downloaded_subtitle = download_remote(
            str(project["source"]),
            destination=run_dir / "source" / "original_master",
            selection=report["selection"],
            progress=progress,
            token=token,
            cookie_file=payload.get("cookie_file"),
            subtitle_selection=(
                subtitle_selection if isinstance(subtitle_selection, Mapping) else None
            ),
        )
        token.checkpoint()
        digest = sha256_file(output)
        result = {
            "path": str(output.relative_to(run_dir)),
            "sha256": digest,
            "byte_size": output.stat().st_size,
        }
        if downloaded_subtitle is not None:
            transcript = _freeze_downloaded_subtitle(
                downloaded_subtitle,
                run_dir=run_dir,
                language=str((subtitle_selection or {}).get("language") or "") or None,
            )
        else:
            transcript = {
                "status": "needs_import_or_device_asr",
                "reason": (
                    str((subtitle_selection or {}).get("reason") or "")
                    or "selected_subtitle_not_returned_by_downloader"
                ),
                "next_action": "导入 SRT/VTT/JSON，或在当前设备运行已绑定的 ASR 工具",
            }
        result["source_transcript"] = transcript
        metadata = dict(project.get("metadata") or {})
        metadata["source_master"] = result
        metadata["source_transcript"] = transcript
        self.database.update_project(
            project_id,
            status="queued",
            current_stage="03",
            progress_mode="gate",
            progress_current=None,
            progress_total=None,
            progress_unit=None,
            metadata=metadata,
        )
        detail = (
            "源视频与冻结源语言字幕已下载并登记"
            if transcript.get("status") == "frozen"
            else "源视频已下载；未取得可冻结字幕，需导入字幕或使用当前设备 ASR"
        )
        return {"detail": detail, **result}
