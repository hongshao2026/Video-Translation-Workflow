"""FastAPI service that connects the web desk to the local dubbing pipeline."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from backend.audition_data import (
    AUDITION_DURATION,
    AUDITION_END,
    AUDITION_SEGMENT_COUNT,
    AUDITION_START,
    AUDITION_TEXT,
    PROJECT_ID,
    build_external_manifest,
    manifest_csv_bytes,
)
from backend.local_runtime import (
    COSYVOICE_MODEL_DIR,
    COSYVOICE_PYTHON,
    KOKORO_MODEL_DIR,
    KOKORO_PYTHON,
    QWEN_PYTHON,
)
from backend.media_utils import normalize_audio as normalize_media_audio
from backend.media_utils import run_media_command as run_shared_media_command
from backend.minimax_client import (
    STARTER_VOICES,
    MiniMaxClient,
    MiniMaxError,
    SpeechConfig,
    clear_runtime_api_key,
    credential_status,
    estimate_cost,
    model_price_per_10k,
    public_catalog,
    resolve_api_key,
    set_runtime_api_key,
)
from backend.minimax_pipeline import synthesize_minimax_pack
from backend.minimax_preview_cache import (
    cached_voice_rows,
    enrich_voices_with_previews,
)
from backend.project_config import CONFIG, project_path, verify_translation_gate
from backend.voice_catalog import (
    COSYVOICE_VOICES,
    KOKORO_VOICES,
    QWEN_VOICES,
    VOICE_IDS,
    VOICES,
)
from backend.workbench.api import create_workbench_router

BASE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = BASE_DIR.parent
RUN_DIR = WORKSPACE_DIR / f"{PROJECT_ID}_run"
DOCS_DIR = BASE_DIR / "docs"
TRANSLATION_JSON = RUN_DIR / "work" / "translation_adjudicated_v2_final.json"
SPEAKER_TRANSLATION_JSON = RUN_DIR / "work" / "role_map_v1.json"
VOICE_HANDOFF_JSON = RUN_DIR / "work" / "voice_selection_handoff_v1.json"
BACKGROUND_AUDIO = RUN_DIR / "work" / "demucs_formal_v2" / "htdemucs" / "minus_vocals.flac"
SOURCE_VIDEO = RUN_DIR / "work" / "USHR-lJ25Qo_working_master_adfree_v2.mp4"
TRANSLATION_JSON = project_path("translation", TRANSLATION_JSON)
BACKGROUND_AUDIO = project_path("background_audio", BACKGROUND_AUDIO)
SOURCE_VIDEO = project_path("source_video", SOURCE_VIDEO)
VOICE_SELECTION_WORK_DIR = RUN_DIR / "work"
VOICE_SELECTION_QA_DIR = RUN_DIR / "qa"
PREVIEW_DIR = BASE_DIR / "backend" / "data" / "voice_previews"
OUTPUT_DIR = BASE_DIR / "outputs"
EXTERNAL_PACK_DIR = BASE_DIR / "backend" / "data" / "external_packs"
MINIMAX_PREVIEW_DIR = BASE_DIR / "backend" / "data" / "minimax_previews"
MINIMAX_CATALOG_PREVIEW_DIR = MINIMAX_PREVIEW_DIR / "catalog"
MINIMAX_CATALOG_MANIFEST = MINIMAX_CATALOG_PREVIEW_DIR / "manifest.json"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
EXTERNAL_PACK_DIR.mkdir(parents=True, exist_ok=True)
MINIMAX_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
MINIMAX_CATALOG_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_AUDIO_BYTES = 128 * 1024 * 1024
MAX_AUDIO_FILES = 200

app = FastAPI(title="声轨工坊本地服务", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", *CONFIG.get("frontend_origins", [])],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
workbench_router, workbench_context = create_workbench_router()
app.include_router(workbench_router)


@app.on_event("shutdown")
def shutdown_workbench_runner() -> None:
    workbench_context.runner.shutdown()


app.mount("/media/voices", StaticFiles(directory=PREVIEW_DIR), name="voice-previews")
app.mount("/media/minimax", StaticFiles(directory=MINIMAX_PREVIEW_DIR), name="minimax-previews")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
app.mount("/docs", StaticFiles(directory=DOCS_DIR), name="workflow-docs")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
gpu_lock = threading.Lock()
minimax_voice_lock = threading.Lock()
voice_selection_lock = threading.Lock()
minimax_voice_cache: list[dict[str, Any]] = cached_voice_rows(
    MINIMAX_CATALOG_MANIFEST,
    MINIMAX_CATALOG_PREVIEW_DIR,
) or enrich_voices_with_previews(
    STARTER_VOICES,
    MINIMAX_CATALOG_MANIFEST,
    MINIMAX_CATALOG_PREVIEW_DIR,
)


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    expected_roles: int = Field(default=3, ge=1, le=8)


class AuditionRequest(BaseModel):
    url: str
    assignments: dict[str, str]
    subtitle_mode: Literal["hard", "soft"] = "hard"
    preserve_source_resolution: bool = True


class ExternalTemplateRequest(BaseModel):
    url: str
    role_names: dict[str, str] = Field(default_factory=dict)


class ExternalAuditionRequest(BaseModel):
    url: str
    pack_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    subtitle_mode: Literal["hard", "soft"] = "hard"
    preserve_source_resolution: bool = True


class MiniMaxCredentialRequest(BaseModel):
    api_key: SecretStr


class MiniMaxPreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    voice_id: str = Field(min_length=1, max_length=256)
    config: dict[str, Any] = Field(default_factory=dict)


class MiniMaxAuditionRequest(BaseModel):
    url: str
    assignments: dict[str, str]
    role_names: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    requests_per_minute: Literal[10, 20] = 10
    subtitle_mode: Literal["hard", "soft"] = "hard"
    preserve_source_resolution: bool = True


class VoiceSelectionRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    assignments: dict[str, str]
    role_names: dict[str, str] = Field(default_factory=dict)


def youtube_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/") or None
    if parsed.hostname and parsed.hostname.endswith("youtube.com"):
        return urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
    return None


def oembed_title(url: str) -> str | None:
    endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"url": url, "format": "json"}
    )
    try:
        with urllib.request.urlopen(endpoint, timeout=8) as response:
            return json.loads(response.read().decode("utf-8")).get("title")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def paid_audition_authorized() -> bool:
    """A locked, hash-valid voice mapping directly unlocks paid audition generation."""
    if not verify_translation_gate() or (CONFIG and AUDITION_SEGMENT_COUNT == 0):
        return False
    _version, _path, selection = latest_voice_selection()
    if not selection or selection.get("video_id") != PROJECT_ID:
        return False
    try:
        handoff = json.loads(VOICE_HANDOFF_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    expected_roles = {str(role.get("role_id")) for role in handoff.get("roles") or []}
    selected_rows = selection.get("roles") or []
    selected_roles = {str(role.get("role_id")) for role in selected_rows}
    if not expected_roles or selected_roles != expected_roles:
        return False
    if any(not str(role.get("voice_id") or "").strip() for role in selected_rows):
        return False
    for role in selected_rows:
        preview = role.get("preview") or {}
        preview_path = Path(str(preview.get("path") or ""))
        if not preview_path.is_absolute():
            preview_path = WORKSPACE_DIR / preview_path
        try:
            if (
                preview.get("status") != "ready_cached"
                or not preview_path.is_file()
                or preview.get("sha256") != file_sha256(preview_path)
            ):
                return False
        except OSError:
            return False
    for label in ("formal_translation", "provisional_role_map", "voice_selection_handoff"):
        source = (selection.get("source_artifacts") or {}).get(label) or {}
        source_path = Path(str(source.get("path") or ""))
        if not source_path.is_absolute():
            source_path = WORKSPACE_DIR / source_path
        try:
            if not source_path.is_file() or source.get("sha256") != file_sha256(source_path):
                return False
        except OSError:
            return False
    return True


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def workspace_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def latest_voice_selection() -> tuple[int, Path | None, dict[str, Any] | None]:
    latest_version = 0
    latest_path: Path | None = None
    pattern = re.compile(r"^voice_selection_locked_v(\d+)\.json$")
    if VOICE_SELECTION_WORK_DIR.exists():
        for path in VOICE_SELECTION_WORK_DIR.glob("voice_selection_locked_v*.json"):
            match = pattern.match(path.name)
            if match and int(match.group(1)) > latest_version:
                latest_version = int(match.group(1))
                latest_path = path
    if latest_path is None:
        return 0, None, None
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return latest_version, latest_path, None
    return latest_version, latest_path, payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def known_project(url: str) -> dict:
    video_id = youtube_id(url)
    required_files = (
        TRANSLATION_JSON,
        SPEAKER_TRANSLATION_JSON,
        VOICE_HANDOFF_JSON,
        BACKGROUND_AUDIO,
        SOURCE_VIDEO,
    )
    is_ready = video_id == PROJECT_ID and all(path.exists() for path in required_files) and verify_translation_gate()
    if is_ready:
        handoff = json.loads(VOICE_HANDOFF_JSON.read_text(encoding="utf-8"))
        role_map = json.loads(SPEAKER_TRANSLATION_JSON.read_text(encoding="utf-8"))
        slot_counts = {
            str(role["role_id"]): int(role.get("provisional_slot_count") or 0)
            for role in role_map.get("roles") or []
        }
        saved_version, saved_path, saved_selection = latest_voice_selection()
        saved_voices = {
            str(role.get("role_id")): str(role.get("voice_id") or "")
            for role in (saved_selection or {}).get("roles") or []
        }
        roles = []
        for speaker_index, speaker in enumerate(handoff["roles"]):
            candidates = sorted(
                speaker.get("candidates") or [],
                key=lambda row: int(row.get("recommendation_rank") or 99),
            )
            representative = speaker.get("source_reference_text") or []
            role_id = str(speaker["role_id"])
            roles.append(
                {
                    "id": role_id,
                    "name": str(speaker["display_name"]),
                    "line_count": slot_counts.get(role_id, 0),
                    "sample": str(representative[0]["subtitle_zh"]) if representative else "暂无代表台词",
                    "default_voice": VOICES[speaker_index % len(VOICES)]["id"],
                    "default_minimax_voice": saved_voices.get(role_id)
                    or str(candidates[0].get("voice_id") if candidates else ""),
                    "minimax_candidates": [str(row["voice_id"]) for row in candidates],
                }
            )
        return {
            "id": video_id,
            "url": url,
            "title": CONFIG.get("title", "The Return to High-Stakes Poker | Fedor Holz"),
            "duration": CONFIG.get("duration", 8947.8095),
            "duration_label": CONFIG.get("duration_label", "2:29:08"),
            "source_resolution": "1920×1080（去广告工作母版）",
            "available_resolution": None,
            "ready": True,
            "notice": CONFIG.get("notice", "Fedor Holz 访谈裁决译稿 v2、去广告工作母版与三角色候选音色已载入。保存并锁定角色音色后即可生成一分钟试听，不再需要单独付费批准。"),
            "paid_audition_authorized": paid_audition_authorized(),
            "voice_selection_saved": saved_selection is not None,
            "voice_selection_version": saved_version if saved_selection is not None else None,
            "voice_selection_sha256": file_sha256(saved_path) if saved_path and saved_selection else None,
            "roles": roles,
            "audition": {
                "start": AUDITION_START,
                "end": AUDITION_END,
                "duration": AUDITION_DURATION,
                "segments": AUDITION_SEGMENT_COUNT,
            },
        }
    count = 3
    role_names = ["角色 A", "角色 B", "角色 C", "角色 D", "角色 E", "角色 F", "角色 G", "角色 H"]
    return {
        "id": video_id or uuid.uuid4().hex[:11],
        "url": url,
        "title": oembed_title(url) or "待预处理的视频",
        "duration": None,
        "duration_label": "下载后读取",
        "source_resolution": "下载后读取",
        "available_resolution": None,
        "ready": False,
        "notice": f"链接已导入，但尚未完成下载、转写、翻译和角色切分。当前工作台已接通 {PROJECT_ID} 项目。",
        "paid_audition_authorized": False,
        "roles": [
            {"id": f"role_{index + 1}", "name": role_names[index], "line_count": None, "sample": "预处理后显示台词", "default_voice": VOICES[index % len(VOICES)]["id"]}
            for index in range(count)
        ],
        "audition": {"start": 0, "end": 60, "duration": 60, "segments": 0},
    }


def update_job(job_id: str, **values) -> None:
    with jobs_lock:
        jobs[job_id].update(values)


def run_command(
    args: list[str],
    cwd: Path,
    job_id: str,
    stage: str,
    *,
    external_progress_start: int = 8,
    external_progress_span: int = 72,
) -> None:
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    tail = []
    for line in process.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-30:]
        if line.startswith("{"):
            try:
                event = json.loads(line)
                if event.get("event") == "progress":
                    progress = 8 + round(event["current"] / event["total"] * 72)
                    update_job(job_id, progress=progress, detail=f"正在生成第 {event['current']}/{event['total']} 句")
                elif event.get("event") == "cosy_progress":
                    progress = 5 + round(event["current"] / event["total"] * 3)
                    update_job(
                        job_id,
                        progress=progress,
                        detail=f"CosyVoice 正在预生成第 {event['current']}/{event['total']} 句",
                    )
                elif event.get("event") == "external_progress":
                    progress = external_progress_start + round(
                        event["current"] / event["total"] * external_progress_span
                    )
                    update_job(
                        job_id,
                        progress=progress,
                        detail=f"正在对齐第 {event['current']}/{event['total']} 句外部配音",
                    )
            except json.JSONDecodeError:
                pass
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"{stage}失败：" + "\n".join(tail[-8:]))


def finish_audition(job_id: str, started: float, warning: str | None = None) -> None:
    job_dir = OUTPUT_DIR / job_id
    update_job(job_id, stage="背景混音", progress=84, detail="正在合并环境声与中文对白")
    mix_path = job_dir / "mix.m4a"
    run_command(
        [
            "ffmpeg", "-y", "-hide_banner", "-ss", str(AUDITION_START),
            "-t", str(AUDITION_DURATION), "-i", str(BACKGROUND_AUDIO),
            "-i", str(job_dir / "voice_track.wav"), "-filter_complex",
            f"[0:a]volume=3.0,aresample=48000[bg];[1:a]aresample=48000,volume=1.10[voice];[bg][voice]amix=inputs=2:duration=longest:normalize=0:dropout_transition=0,alimiter=limit=0.891:level=false,atrim=duration={AUDITION_DURATION}[out]",
            "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(mix_path),
        ],
        BASE_DIR,
        job_id,
        "背景混音",
    )

    update_job(job_id, stage="视频导出", progress=91, detail="正在烧录字幕并输出 H.264 视频")
    output = job_dir / "audition.mp4"
    subtitle_filter = "subtitles=filename='subtitles.srt':force_style='FontName=Microsoft YaHei,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=22'"
    run_command(
        [
            "ffmpeg", "-y", "-hide_banner", "-ss", str(AUDITION_START),
            "-t", str(AUDITION_DURATION), "-i", str(SOURCE_VIDEO), "-i", str(mix_path),
            "-map", "0:v:0", "-map", "1:a:0", "-vf", subtitle_filter,
            "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", "-shortest", str(output),
        ],
        job_dir,
        job_id,
        "视频导出",
    )
    run_command(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "NUL"], job_dir, job_id, "成片验收")
    metrics = json.loads((job_dir / "metrics.json").read_text(encoding="utf-8"))
    update_job(
        job_id,
        status="complete",
        stage="完成",
        progress=100,
        detail="试听片已生成并通过完整解码检查",
        elapsed_seconds=round(time.perf_counter() - started, 1),
        output_url=f"/outputs/{job_id}/audition.mp4",
        metrics=metrics,
        warning=warning,
    )


def render_audition(job_id: str, request: AuditionRequest) -> None:
    started = time.perf_counter()
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    assignments = dict(request.assignments)
    (job_dir / "assignments.json").write_text(
        json.dumps(assignments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        with gpu_lock:
            update_job(job_id, status="running", stage="语音合成", progress=5, detail="正在加载所选本地语音模型")
            run_command(
                [
                    str(KOKORO_PYTHON),
                    "-m",
                    "backend.generate_multi_engine_audition",
                    str(TRANSLATION_JSON),
                    str(job_dir / "assignments.json"),
                    "--output-dir",
                    str(job_dir),
                ],
                BASE_DIR,
                job_id,
                "语音合成",
            )
        finish_audition(job_id, started)
    except Exception as exc:  # noqa: BLE001 - worker boundary must persist every failure.
        update_job(
            job_id,
            status="error",
            stage="失败",
            detail=str(exc),
            elapsed_seconds=round(time.perf_counter() - started, 1),
        )


def run_media_command(args: list[str], timeout: int = 180) -> str:
    return run_shared_media_command(args, BASE_DIR, timeout)


def normalize_audio(source: Path, destination: Path) -> float:
    return normalize_media_audio(source, destination, BASE_DIR)


def pack_path(pack_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", pack_id):
        raise HTTPException(status_code=400, detail="配音包编号格式不正确")
    path = EXTERNAL_PACK_DIR / pack_id
    if not path.exists():
        raise HTTPException(status_code=404, detail="这个配音包不存在，请重新上传")
    return path


def build_pack_report(pack_id: str, manifest: dict, errors: dict[str, str], extras: list[str]) -> dict:
    normalized_dir = EXTERNAL_PACK_DIR / pack_id / "normalized"
    rows = []
    missing = []
    invalid = []
    warning_count = 0
    for segment in manifest["segments"]:
        segment_id = segment["segment_id"]
        audio_path = normalized_dir / f"{segment_id}.wav"
        error = errors.get(segment_id)
        if not audio_path.exists():
            status = "invalid" if error else "missing"
            audio_seconds = None
            stretch_rate = None
            duration_ratio = None
            overflow_seconds = None
            message = error or "尚未上传"
            (invalid if error else missing).append(segment_id)
        else:
            duration_text = run_media_command(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
                ],
                timeout=30,
            )
            audio_seconds = round(float(duration_text), 3)
            slot_seconds = float(segment["slot_seconds"])
            duration_ratio = round(audio_seconds / slot_seconds, 3)
            overflow_seconds = round(max(0.0, audio_seconds - slot_seconds), 3)
            stretch_rate = 1.0
            if overflow_seconds > 1.0 or duration_ratio > 1.35:
                status = "long"
                message = f"超出时间槽 {overflow_seconds:.2f} 秒；不会自动变速，建议缩短台词或重新配音"
                warning_count += 1
            elif overflow_seconds > 0.15:
                status = "tight"
                message = f"超出时间槽 {overflow_seconds:.2f} 秒；会保持原速，可能接近下一句"
                warning_count += 1
            else:
                status = "ready"
                message = "时长合适；生成时保持原速"
            if error:
                message = f"新文件无效，仍使用上次版本：{error}"
                warning_count += 1
        rows.append(
            {
                **segment,
                "status": status,
                "audio_seconds": audio_seconds,
                "stretch_rate": stretch_rate,
                "duration_ratio": duration_ratio,
                "overflow_seconds": overflow_seconds,
                "message": message,
            }
        )
    return {
        "pack_id": pack_id,
        "ready": not missing and not invalid,
        "required_count": len(rows),
        "ready_count": sum(1 for row in rows if row["audio_seconds"] is not None),
        "missing": missing,
        "invalid": invalid,
        "extras": extras,
        "warning_count": warning_count,
        "segments": rows,
    }


def render_external_audition(job_id: str, request: ExternalAuditionRequest) -> None:
    started = time.perf_counter()
    source_pack = pack_path(request.pack_id)
    pack_json = source_pack / "pack.json"
    try:
        payload = json.loads(pack_json.read_text(encoding="utf-8"))
        report = payload["report"]
        if not report["ready"]:
            raise ValueError("配音包仍有缺失或无效音频，请先完成检查")
        job_dir = OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        update_job(job_id, status="running", stage="音频对轴", progress=5, detail="正在读取外部配音包")
        run_command(
            [
                str(QWEN_PYTHON), "-m", "backend.generate_external_audition",
                str(pack_json), "--output-dir", str(job_dir),
            ],
            BASE_DIR,
            job_id,
            "音频对轴",
        )
        warning = None
        if report["warning_count"]:
            warning = f"有 {report['warning_count']} 句可能超出时间槽；成片保持原速，可以在检查表中找到并重新配音。"
        finish_audition(job_id, started, warning)
    except Exception as exc:  # noqa: BLE001 - worker boundary must persist every failure.
        update_job(
            job_id,
            status="error",
            stage="失败",
            detail=str(exc),
            elapsed_seconds=round(time.perf_counter() - started, 1),
        )


def minimax_error_text(exc: Exception) -> str:
    detail = str(exc)
    if isinstance(exc, MiniMaxError) and exc.trace_id:
        detail = f"{detail}（Trace ID：{exc.trace_id}）"
    return detail


def render_minimax_audition(
    job_id: str,
    request: MiniMaxAuditionRequest,
    client: MiniMaxClient,
    config: SpeechConfig,
) -> None:
    started = time.perf_counter()
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest = build_external_manifest(TRANSLATION_JSON, request.role_names)
        update_job(
            job_id,
            status="running",
            stage="MiniMax 合成",
            progress=3,
            detail="正在生成逐句中文语音；付费请求不会自动重试",
        )

        def report_progress(current: int, total: int, row: dict) -> None:
            progress_value = 3 + round(current / total * 52)
            update_job(
                job_id,
                progress=progress_value,
                detail=f"MiniMax 已生成第 {current}/{total} 句",
                last_segment=row["segment_id"],
            )

        synthesis_report = synthesize_minimax_pack(
            client=client,
            manifest=manifest,
            assignments=request.assignments,
            config=config,
            pack_dir=job_dir / "minimax_pack",
            project_root=BASE_DIR,
            requests_per_minute=request.requests_per_minute,
            progress=report_progress,
        )
        update_job(
            job_id,
            stage="音频对轴",
            progress=57,
            detail="逐句语音已返回，正在放回原始绝对时间轴",
        )
        run_command(
            [
                str(QWEN_PYTHON),
                "-m",
                "backend.generate_external_audition",
                synthesis_report["pack_json"],
                "--output-dir",
                str(job_dir),
            ],
            BASE_DIR,
            job_id,
            "MiniMax 音频对轴",
            external_progress_start=57,
            external_progress_span=23,
        )

        metrics_path = job_dir / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        synthesis_rows = {
            row["segment_id"]: row for row in synthesis_report["segments"]
        }
        for row in metrics.get("segments", []):
            generated = synthesis_rows.get(row["segment_id"], {})
            row.update(
                {
                    "engine": "minimax",
                    "voice_id": generated.get("voice_id"),
                    "trace_id": generated.get("trace_id"),
                    "synthesis_seconds": generated.get("synthesis_seconds", 0),
                    "usage_characters": generated.get("usage_characters", 0),
                }
            )
        actual_characters = int(synthesis_report["usage_characters"])
        metrics.update(
            {
                "source": "minimax_api",
                "synthesis_seconds": synthesis_report["synthesis_seconds"],
                "minimax": {
                    "config": config.public_dict(),
                    "requests_per_minute": request.requests_per_minute,
                    "usage_characters": actual_characters,
                    "estimated_cny": round(
                        actual_characters / 10_000 * model_price_per_10k(config.model), 4
                    ),
                },
            }
        )
        metrics_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        warnings = []
        if metrics.get("timing_overlap_count"):
            warnings.append(
                f"有 {metrics['timing_overlap_count']} 句保持原速后会与下一句重叠，请优先缩短中文或重配。"
            )
        elif metrics.get("timing_overflow_count"):
            warnings.append(
                f"有 {metrics['timing_overflow_count']} 句略超出原时间槽，但不会变速。"
            )
        warning = " ".join(warnings) or None
        finish_audition(job_id, started, warning)
    except Exception as exc:  # noqa: BLE001 - paid-request boundary records uncertain failures.
        update_job(
            job_id,
            status="error",
            stage="失败",
            detail=minimax_error_text(exc),
            elapsed_seconds=round(time.perf_counter() - started, 1),
            uncertain_completion=isinstance(exc, MiniMaxError)
            and exc.uncertain_completion,
        )


@app.get("/api/health")
def health() -> dict:
    kokoro_ready = (
        KOKORO_PYTHON.exists()
        and (KOKORO_MODEL_DIR / "config.json").exists()
        and (KOKORO_MODEL_DIR / "kokoro-v1_1-zh.pth").exists()
        and len(KOKORO_VOICES) == 100
    )
    cosyvoice_ready = (
        COSYVOICE_PYTHON.exists()
        and (COSYVOICE_MODEL_DIR / "cosyvoice.yaml").exists()
        and (COSYVOICE_MODEL_DIR / "spk2info.pt").exists()
        and len(COSYVOICE_VOICES) == 7
    )
    return {
        "ok": True,
        "project_id": PROJECT_ID,
        "model_ready": QWEN_PYTHON.exists() and kokoro_ready and cosyvoice_ready,
        "engines": {
            "qwen": {"ready": QWEN_PYTHON.exists(), "voices": len(QWEN_VOICES)},
            "kokoro": {"ready": kokoro_ready, "voices": len(KOKORO_VOICES)},
            "cosyvoice": {"ready": cosyvoice_ready, "voices": len(COSYVOICE_VOICES)},
            "minimax": credential_status(),
        },
        "sample_project_ready": verify_translation_gate() and all(
            path.exists()
            for path in (
                TRANSLATION_JSON,
                VOICE_HANDOFF_JSON,
                BACKGROUND_AUDIO,
                SOURCE_VIDEO,
            )
        ),
        "gpu_queue_busy": gpu_lock.locked(),
        "paid_audition_authorized": paid_audition_authorized(),
    }


@app.get("/api/voices")
def voices() -> dict:
    rows = []
    for voice in VOICES:
        filename = voice["preview_filename"]
        path = PREVIEW_DIR / filename
        public_voice = {key: value for key, value in voice.items() if key != "voice_path"}
        rows.append({**public_voice, "preview_ready": path.exists(), "preview_url": f"/media/voices/{filename}"})
    return {
        "voices": rows,
        "counts": {
            "total": len(rows),
            "qwen": len(QWEN_VOICES),
            "kokoro": len(KOKORO_VOICES),
            "cosyvoice": len(COSYVOICE_VOICES),
            "preview_ready": sum(1 for row in rows if row["preview_ready"]),
        },
    }


@app.get("/api/minimax/catalog")
def minimax_catalog() -> dict:
    with minimax_voice_lock:
        cached_voices = enrich_voices_with_previews(
            minimax_voice_cache,
            MINIMAX_CATALOG_MANIFEST,
            MINIMAX_CATALOG_PREVIEW_DIR,
        )
    preview_count = sum(1 for voice in cached_voices if voice.get("preview_ready"))
    is_account_catalog = len(cached_voices) > 58 or any(
        voice.get("category") != "system" for voice in cached_voices
    )
    return {
        **public_catalog(),
        "voices": cached_voices,
        "audition_estimates": {
            model["id"]: estimate_cost("".join(AUDITION_TEXT.values()), model["id"])
            for model in public_catalog()["models"]
        },
        "catalog_source": (
            "account" if is_account_catalog else "local_cache" if preview_count else "official_seed"
        ),
        "preview_cache": {
            "ready": preview_count,
            "mandarin_total": 58,
            "sample_model": "speech-2.8-hd",
            "local_playback_billable": False,
        },
        "paid_audition_authorized": paid_audition_authorized(),
    }


@app.post("/api/minimax/credential")
def save_minimax_credential(request: MiniMaxCredentialRequest) -> dict:
    try:
        set_runtime_api_key(request.api_key.get_secret_value())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        **credential_status(),
        "detail": "API Key 已保存在后端内存；服务重启后会自动清除。",
    }


@app.delete("/api/minimax/credential")
def delete_minimax_credential() -> dict:
    clear_runtime_api_key()
    local_voices = cached_voice_rows(
        MINIMAX_CATALOG_MANIFEST,
        MINIMAX_CATALOG_PREVIEW_DIR,
    ) or enrich_voices_with_previews(
        STARTER_VOICES,
        MINIMAX_CATALOG_MANIFEST,
        MINIMAX_CATALOG_PREVIEW_DIR,
    )
    with minimax_voice_lock:
        minimax_voice_cache[:] = local_voices
    return {
        **credential_status(),
        "voices": local_voices,
        "detail": "内存中的 MiniMax API Key 已清除；本地试听仍可继续播放。",
    }


@app.post("/api/minimax/connect")
def connect_minimax() -> dict:
    try:
        client = MiniMaxClient(resolve_api_key())
        account_voices = enrich_voices_with_previews(
            client.list_voices(),
            MINIMAX_CATALOG_MANIFEST,
            MINIMAX_CATALOG_PREVIEW_DIR,
        )
    except (MiniMaxError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=minimax_error_text(exc)) from None
    if not account_voices:
        raise HTTPException(status_code=502, detail="连接成功，但当前账号没有返回可用音色")
    with minimax_voice_lock:
        minimax_voice_cache[:] = account_voices
    counts = {
        category: sum(1 for voice in account_voices if voice["category"] == category)
        for category in ("system", "voice_cloning", "voice_generation")
    }
    return {
        **credential_status(),
        "connected": True,
        "voices": account_voices,
        "counts": {"total": len(account_voices), **counts},
        "detail": f"连接成功，已载入 {len(account_voices)} 个当前账号可用音色。",
    }


@app.post("/api/minimax/preview")
def create_minimax_preview(request: MiniMaxPreviewRequest) -> dict:
    if not paid_audition_authorized():
        raise HTTPException(
            status_code=423,
            detail="请先为全部角色保存并锁定音色。",
        )
    try:
        config = SpeechConfig.from_mapping(request.config)
        if config.speed != 1.0:
            raise ValueError("本项目中文 TTS 必须使用模型原生 speed=1.0")
        cost = estimate_cost(request.text, config.model)
        client = MiniMaxClient(resolve_api_key())
        result = client.synthesize(request.text, request.voice_id, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except MiniMaxError as exc:
        status = 504 if exc.uncertain_completion else 502
        raise HTTPException(status_code=status, detail=minimax_error_text(exc)) from None
    extension = result.audio_format if result.audio_format in {"mp3", "wav", "flac"} else config.format
    preview_id = uuid.uuid4().hex[:12]
    destination = MINIMAX_PREVIEW_DIR / f"{preview_id}.{extension}"
    destination.write_bytes(result.audio)
    actual_characters = int(result.extra_info.get("usage_characters") or cost["billable_characters"])
    return {
        "id": preview_id,
        "audio_url": f"/media/minimax/{destination.name}",
        "trace_id": result.trace_id,
        "audio_format": extension,
        "audio_length_ms": result.extra_info.get("audio_length"),
        "usage_characters": actual_characters,
        "estimated_cny": round(
            actual_characters / 10_000 * model_price_per_10k(config.model), 4
        ),
    }


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest) -> dict:
    if not re.match(r"^https?://", request.url.strip(), re.IGNORECASE):
        raise HTTPException(status_code=400, detail="请输入完整的 http 或 https 视频链接")
    return known_project(request.url.strip())


@app.post("/api/voice-selection")
def save_voice_selection(request: VoiceSelectionRequest) -> dict:
    """Persist a user's cached-voice choice without authorizing a paid call."""

    if youtube_id(request.url) != PROJECT_ID:
        raise HTTPException(status_code=409, detail="这个链接不是当前已放行的选音项目")
    if not verify_translation_gate():
        raise HTTPException(status_code=409, detail="当前译稿批准已失效，请重新核验翻译门禁")

    try:
        handoff = json.loads(VOICE_HANDOFF_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raise HTTPException(status_code=409, detail="当前项目缺少选音交接文件") from None

    handoff_roles = handoff.get("roles") or []
    expected_role_ids = [str(role.get("role_id")) for role in handoff_roles]
    if set(request.assignments) != set(expected_role_ids):
        raise HTTPException(status_code=400, detail="必须为当前每个角色各选择且只选择一个音色")

    with minimax_voice_lock:
        catalog = enrich_voices_with_previews(
            minimax_voice_cache,
            MINIMAX_CATALOG_MANIFEST,
            MINIMAX_CATALOG_PREVIEW_DIR,
        )
    voice_by_id = {str(voice.get("voice_id")): voice for voice in catalog}
    candidate_by_role = {
        str(role["role_id"]): {
            str(candidate["voice_id"]): candidate
            for candidate in role.get("candidates") or []
        }
        for role in handoff_roles
    }

    selected_rows: list[dict[str, Any]] = []
    canonical_names: dict[str, str] = {}
    for role in handoff_roles:
        role_id = str(role["role_id"])
        voice_id = str(request.assignments.get(role_id) or "").strip()
        voice = voice_by_id.get(voice_id)
        if not voice:
            raise HTTPException(status_code=400, detail=f"{role['display_name']} 的音色不在本地目录中")
        if voice.get("language") != "zh-CN" or not voice.get("preview_ready"):
            raise HTTPException(status_code=400, detail=f"{voice_id} 没有可验证的本地普通话试听")
        preview_url = str(voice.get("preview_url") or "")
        preview_filename = Path(preview_url).name
        preview_path = MINIMAX_CATALOG_PREVIEW_DIR / preview_filename
        if not preview_filename or not preview_path.is_file() or preview_path.stat().st_size <= 0:
            raise HTTPException(status_code=400, detail=f"{voice_id} 的本地试听文件不可用")

        display_name = str(request.role_names.get(role_id) or role["display_name"]).strip()
        if not display_name:
            raise HTTPException(status_code=400, detail=f"{role_id} 的角色名称不能为空")
        canonical_names[role_id] = display_name
        candidate = candidate_by_role[role_id].get(voice_id)
        selected_rows.append(
            {
                "role_id": role_id,
                "display_name": display_name,
                "voice_id": voice_id,
                "voice_name": str(voice.get("voice_name") or voice_id),
                "candidate_code": str(candidate.get("code")) if candidate else None,
                "candidate_rank": int(candidate.get("recommendation_rank")) if candidate else None,
                "preview": {
                    "path": workspace_path(preview_path),
                    "sha256": file_sha256(preview_path),
                    "bytes": preview_path.stat().st_size,
                    "status": "ready_cached",
                },
            }
        )

    normalized_assignments = {row["role_id"]: row["voice_id"] for row in selected_rows}
    with voice_selection_lock:
        latest_version, latest_path, latest_payload = latest_voice_selection()
        if latest_payload:
            prior_assignments = {
                str(row.get("role_id")): str(row.get("voice_id"))
                for row in latest_payload.get("roles") or []
            }
            prior_names = {
                str(row.get("role_id")): str(row.get("display_name"))
                for row in latest_payload.get("roles") or []
            }
            if prior_assignments == normalized_assignments and prior_names == canonical_names:
                return {
                    "detail": "这组角色音色已经锁定，现在可直接生成一分钟试听。",
                    "version": latest_version,
                    "selection_path": workspace_path(latest_path),
                    "selection_sha256": file_sha256(latest_path),
                    "assignments": normalized_assignments,
                    "paid_audition_authorized": paid_audition_authorized(),
                    "reused": True,
                }

        version = latest_version + 1
        generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        selection_path = VOICE_SELECTION_WORK_DIR / f"voice_selection_locked_v{version}.json"
        qa_path = VOICE_SELECTION_QA_DIR / f"voice_selection_lock_v{version}.json"
        source_files = {
            "formal_translation": TRANSLATION_JSON,
            "provisional_role_map": SPEAKER_TRANSLATION_JSON,
            "voice_selection_handoff": VOICE_HANDOFF_JSON,
            "cached_preview_manifest": MINIMAX_CATALOG_MANIFEST,
        }
        source_metadata = {
            label: {
                "path": workspace_path(path),
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
            for label, path in source_files.items()
        }
        selection_payload = {
            "schema_version": 1,
            "version": version,
            "status": "voice_selection_locked_audition_ready",
            "video_id": PROJECT_ID,
            "generated_at": generated_at,
            "selection_method": "workbench_user_click",
            "source_artifacts": source_metadata,
            "roles": selected_rows,
            "authorization": {
                "play_local_cached_previews": True,
                "local_voice_selection": True,
                "paid_preview": True,
                "paid_one_minute_sample": True,
                "paid_full_tts": False,
                "render": False,
            },
            "next_gate": "generate one-minute audition directly; after audition, a user generate-full command authorizes full TTS and render for the frozen inputs",
        }
        write_json_atomic(selection_path, selection_payload)
        selection_sha256 = file_sha256(selection_path)
        qa_payload = {
            "schema_version": 1,
            "version": version,
            "status": "pass",
            "video_id": PROJECT_ID,
            "generated_at": generated_at,
            "lock_scope": "voice_selection_and_one_minute_audition",
            "selection": {
                "path": workspace_path(selection_path),
                "sha256": selection_sha256,
                "bytes": selection_path.stat().st_size,
            },
            "checks": {
                "all_roles_selected_once": True,
                "all_voices_mandarin": True,
                "all_previews_cached_and_hashed": True,
                "paid_preview_authorized": True,
                "paid_tts_authorized": False,
                "render_authorized": False,
            },
            "role_assignments": normalized_assignments,
            "next_gate": selection_payload["next_gate"],
        }
        write_json_atomic(qa_path, qa_payload)

    return {
        "detail": "角色音色已保存并锁定；现在可直接生成一分钟试听，不需要另行批准。",
        "version": version,
        "selection_path": workspace_path(selection_path),
        "selection_sha256": selection_sha256,
        "qa_path": workspace_path(qa_path),
        "qa_sha256": file_sha256(qa_path),
        "assignments": normalized_assignments,
        "paid_audition_authorized": paid_audition_authorized(),
        "reused": False,
    }


@app.post("/api/external-packs/template")
def download_external_template(request: ExternalTemplateRequest) -> Response:
    if youtube_id(request.url) != PROJECT_ID:
        raise HTTPException(status_code=409, detail="这个链接还没有可导出的中文时间轴")
    manifest = build_external_manifest(TRANSLATION_JSON, request.role_names)
    readme = (
        "声轨工坊 · 外部配音任务包\r\n\r\n"
        "1. 打开 manifest.csv，把每行 text 交给剪映或其他语音工具生成。\r\n"
        "2. 每句话必须单独导出，文件名保持为 segment_0001、segment_0002……\r\n"
        "3. 支持 WAV、MP3、M4A、AAC、FLAC、OGG；不用自行改采样率。\r\n"
        "4. 把所有音频压成一个 ZIP，或直接多选音频回传工作台。\r\n"
        "5. 工作台会检查缺句和时长，并按 manifest 中的绝对时间自动对齐。\r\n\r\n"
        "不要把整段配音合成一个文件；逐句文件才能稳定对齐。\r\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.csv", manifest_csv_bytes(manifest))
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr("使用说明.txt", readme.encode("utf-8-sig"))
        archive.writestr(
            "audio/请把逐句音频放在这里.txt",
            "文件名示例：segment_0001.wav".encode("utf-8-sig"),
        )
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="voice_task_{PROJECT_ID}.zip"',
            "Cache-Control": "no-store",
        },
    )


async def save_limited_upload(upload: UploadFile, destination: Path, limit: int) -> int:
    written = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise HTTPException(status_code=413, detail=f"{upload.filename} 超过大小限制")
            handle.write(chunk)
    await upload.close()
    return written


def clean_upload_name(filename: str | None) -> str:
    name = PurePosixPath((filename or "").replace("\\", "/")).name
    if not name or len(name) > 180:
        raise HTTPException(status_code=400, detail="上传文件名无效或过长")
    return name


@app.post("/api/external-packs")
async def upload_external_pack(
    files: Annotated[list[UploadFile], File()],
    url: Annotated[str, Form()],
    role_names: Annotated[str, Form()] = "{}",
    pack_id: Annotated[str | None, Form()] = None,
) -> dict:
    if youtube_id(url) != PROJECT_ID:
        raise HTTPException(status_code=409, detail="这个链接还没有可导入的中文时间轴")
    if not files or len(files) > MAX_AUDIO_FILES:
        raise HTTPException(status_code=400, detail=f"一次请选择 1–{MAX_AUDIO_FILES} 个文件")
    try:
        parsed_names = json.loads(role_names)
        if not isinstance(parsed_names, dict):
            raise TypeError
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="角色名称数据格式不正确") from None

    names = [clean_upload_name(upload.filename) for upload in files]
    zip_files = [name for name in names if Path(name).suffix.lower() == ".zip"]
    if zip_files and (len(files) != 1 or len(zip_files) != 1):
        raise HTTPException(status_code=400, detail="ZIP 请单独上传，不要与音频文件混选")
    if not zip_files:
        unsupported = [name for name in names if Path(name).suffix.lower() not in AUDIO_EXTENSIONS]
        if unsupported:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式：{', '.join(unsupported[:5])}")

    current_pack_id = pack_id or uuid.uuid4().hex[:12]
    if pack_id:
        directory = pack_path(pack_id)
    else:
        directory = EXTERNAL_PACK_DIR / current_pack_id
        directory.mkdir(parents=True, exist_ok=False)
    incoming_dir = directory / "incoming"
    raw_dir = directory / "raw"
    normalized_dir = directory / "normalized"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_external_manifest(TRANSLATION_JSON, parsed_names)
    expected = {row["segment_id"]: row for row in manifest["segments"]}
    candidates: dict[str, Path] = {}
    extras: list[str] = []
    errors: dict[str, str] = {}

    try:
        if zip_files:
            archive_path = incoming_dir / f"upload_{uuid.uuid4().hex[:8]}.zip"
            await save_limited_upload(files[0], archive_path, MAX_UPLOAD_BYTES)
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    infos = [info for info in archive.infolist() if not info.is_dir()]
                    if len(infos) > MAX_AUDIO_FILES + 20:
                        raise HTTPException(status_code=400, detail="ZIP 内文件数量过多")
                    uncompressed = 0
                    for info in infos:
                        name = clean_upload_name(info.filename)
                        extension = Path(name).suffix.lower()
                        if extension not in AUDIO_EXTENSIONS:
                            continue
                        uncompressed += info.file_size
                        if info.file_size > MAX_AUDIO_BYTES or uncompressed > MAX_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="ZIP 解压后的音频超过大小限制")
                        segment_id = Path(name).stem.lower()
                        if segment_id not in expected:
                            extras.append(name)
                            continue
                        if segment_id in candidates:
                            errors[segment_id] = "压缩包中存在同名音频"
                            continue
                        destination = incoming_dir / f"{segment_id}{extension}"
                        with archive.open(info) as source, destination.open("wb") as target:
                            shutil.copyfileobj(source, target, length=1024 * 1024)
                        candidates[segment_id] = destination
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="ZIP 文件损坏或格式不正确") from None
            finally:
                archive_path.unlink(missing_ok=True)
        else:
            total_bytes = 0
            for upload, name in zip(files, names, strict=True):
                extension = Path(name).suffix.lower()
                segment_id = Path(name).stem.lower()
                if segment_id not in expected:
                    extras.append(name)
                    await upload.close()
                    continue
                if segment_id in candidates:
                    errors[segment_id] = "本次上传中存在同名音频"
                    await upload.close()
                    continue
                destination = incoming_dir / f"{segment_id}{extension}"
                total_bytes += await save_limited_upload(upload, destination, MAX_AUDIO_BYTES)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="本次上传的音频总大小超过 512 MB")
                candidates[segment_id] = destination

        for segment_id, source in candidates.items():
            if segment_id in errors:
                source.unlink(missing_ok=True)
                continue
            try:
                normalize_audio(source, normalized_dir / f"{segment_id}.wav")
                raw_destination = raw_dir / f"{segment_id}{source.suffix.lower()}"
                source.replace(raw_destination)
            except (ValueError, subprocess.SubprocessError) as exc:
                errors[segment_id] = str(exc).splitlines()[-1][:300]
            finally:
                source.unlink(missing_ok=True)

        report = build_pack_report(current_pack_id, manifest, errors, extras)
        payload = {
            "schema_version": 1,
            "created_at": time.time(),
            "manifest": manifest,
            "report": report,
        }
        (directory / "pack.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report
    except Exception:
        if not pack_id and not (directory / "pack.json").exists():
            shutil.rmtree(directory, ignore_errors=True)
        raise


@app.post("/api/jobs/audition", status_code=202)
def create_audition(request: AuditionRequest) -> dict:
    if youtube_id(request.url) != PROJECT_ID:
        raise HTTPException(status_code=409, detail=f"这个链接还没有完成预处理。当前可直接生成的是 {PROJECT_ID}。")
    invalid = [value for value in request.assignments.values() if value not in VOICE_IDS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"未知语音包：{', '.join(invalid)}")
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "排队",
        "progress": 0,
        "detail": "任务已创建",
        "created_at": time.time(),
        "assignments": dict(request.assignments),
    }
    threading.Thread(target=render_audition, args=(job_id, request), daemon=True).start()
    return jobs[job_id]


@app.post("/api/jobs/external-audition", status_code=202)
def create_external_audition(request: ExternalAuditionRequest) -> dict:
    if youtube_id(request.url) != PROJECT_ID:
        raise HTTPException(status_code=409, detail="这个链接还没有完成预处理")
    directory = pack_path(request.pack_id)
    payload = json.loads((directory / "pack.json").read_text(encoding="utf-8"))
    if not payload["report"]["ready"]:
        raise HTTPException(status_code=409, detail="配音包仍有缺失或无效音频，请先完成检查")
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "排队",
        "progress": 0,
        "detail": "外部配音任务已创建",
        "created_at": time.time(),
        "pack_id": request.pack_id,
        "source": "external_audio_pack",
    }
    threading.Thread(
        target=render_external_audition,
        args=(job_id, request),
        daemon=True,
    ).start()
    return jobs[job_id]


@app.post("/api/jobs/minimax-audition", status_code=202)
def create_minimax_audition(request: MiniMaxAuditionRequest) -> dict:
    if youtube_id(request.url) != PROJECT_ID:
        raise HTTPException(status_code=409, detail="这个链接还没有完成预处理")
    if not paid_audition_authorized():
        raise HTTPException(
            status_code=423,
            detail="请先为全部角色保存并锁定音色。",
        )
    try:
        config = SpeechConfig.from_mapping(request.config)
        if config.speed != 1.0:
            raise ValueError("本项目中文 TTS 必须使用模型原生 speed=1.0")
        client = MiniMaxClient(resolve_api_key())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except MiniMaxError as exc:
        raise HTTPException(status_code=409, detail=minimax_error_text(exc)) from None
    manifest = build_external_manifest(TRANSLATION_JSON, request.role_names)
    required_roles = {str(segment["role_id"]) for segment in manifest["segments"]}
    missing_roles = [
        role_id for role_id in sorted(required_roles) if not request.assignments.get(role_id, "").strip()
    ]
    if missing_roles:
        raise HTTPException(status_code=400, detail=f"这些角色还没有选择 MiniMax 音色：{', '.join(missing_roles)}")
    full_text = "".join(str(segment["text"]) for segment in manifest["segments"])
    cost = estimate_cost(full_text, config.model)
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "排队",
        "progress": 0,
        "detail": "MiniMax 任务已创建，等待逐句合成",
        "created_at": time.time(),
        "source": "minimax_api",
        "billing_estimate": cost,
        "assignments": dict(request.assignments),
        "config": config.public_dict(),
    }
    threading.Thread(
        target=render_minimax_audition,
        args=(job_id, request, client, config),
        daemon=True,
    ).start()
    return jobs[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(job)
