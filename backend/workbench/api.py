"""FastAPI routes for the multi-project workbench core."""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr

from backend.providers import ProviderProfile, builtin_registry

from .artifact_services import ArtifactServiceError, WorkflowArtifactService
from .credentials import CredentialBroker
from .database import PROJECT_STATUSES, WorkbenchDatabase, new_id
from .ingest import AdEvidenceError, PathSafetyError, TranscriptError
from .library import ProjectLibrary
from .media import MediaService
from .media_artifacts import MediaArtifactJobs
from .production_jobs import ProductionJobs
from .publication import PublicationError
from .runner import LocalTaskRunner
from .settings import WorkbenchSettings
from .stage_gates import StageGateError, StageGateService
from .workflows import WorkflowService

PROFILE_ID = re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
PROVIDER_CATALOG = (
    {
        "provider_id": "openai-compatible",
        "service_kind": "llm",
        "label": "OpenAI-compatible 翻译模型",
        "base_url_required": True,
        "recommended_config": {"structured_output_mode": "prompt_json"},
    },
    {
        "provider_id": "minimax-llm",
        "service_kind": "llm",
        "label": "MiniMax 翻译模型",
        "default_base_url": "https://api.minimax.cn/v1",
        "recommended_config": {"structured_output_mode": "prompt_json"},
    },
    {
        "provider_id": "openai-compatible-speech",
        "service_kind": "speech",
        "label": "OpenAI-compatible 语音",
        "base_url_required": True,
        "recommended_config": {"voices": []},
    },
    {
        "provider_id": "minimax-speech",
        "service_kind": "speech",
        "label": "MiniMax 语音",
        "default_base_url": "https://api.minimax.cn",
        "recommended_config": {},
    },
)
PROVIDER_ALIASES = {
    "compatible": "openai-compatible",
    "openai.compatible": "openai-compatible",
    "openai.speech": "openai-compatible-speech",
    "minimax.llm": "minimax-llm",
    "minimax.speech": "minimax-speech",
}


class ProjectCreateRequest(BaseModel):
    source_kind: Literal["video_url", "local_file"]
    source: str = Field(min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=240)
    copy_local_file: bool = False


class ProjectAttachRequest(BaseModel):
    library_path: str = Field(min_length=1, max_length=1024)


class ProjectSourceRebindRequest(BaseModel):
    source_kind: Literal["video_url", "local_file"]
    source: str = Field(min_length=1, max_length=4096)


class RunCreateRequest(BaseModel):
    preset_id: str = "quality-zh-v1"
    role_bindings: dict[Literal["T", "A", "B", "C"], str] = Field(default_factory=dict)
    speech_profile_id: str | None = None


class ProviderProfileRequest(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    service_kind: Literal["llm", "speech"]
    provider_id: str = Field(min_length=2, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str = Field(min_length=1, max_length=240)
    api_key: SecretStr | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TaskActionRequest(BaseModel):
    repair_evidence: str | None = Field(default=None, max_length=2000)


class MediaJobRequest(BaseModel):
    expected_language: str | None = Field(default=None, max_length=32)
    cookie_file: str | None = Field(default=None, max_length=4096)


class TranslationJobRequest(BaseModel):
    slots_path: str = Field(min_length=1, max_length=1024)
    glossary_path: str | None = Field(default=None, max_length=1024)
    project_context: str = Field(default="", max_length=12000)
    version: int = Field(default=1, ge=1, le=999)
    batch_size: int = Field(default=40, ge=1, le=200)


class SpeechJobRequest(BaseModel):
    segments_path: str = Field(min_length=1, max_length=1024)
    authorization_path: str = Field(min_length=1, max_length=1024)
    translation_gate_path: str = Field(default="qa/translation_gate.json", min_length=1, max_length=1024)
    profile_id: str | None = Field(default=None, max_length=120)
    output_dir: str = Field(default="full_dub_v1/tts", min_length=1, max_length=1024)


class ChapterReadingRequest(BaseModel):
    translation_path: str = Field(min_length=1, max_length=1024)
    chapters: list[dict[str, Any]] | None = None
    timeline_path: str | None = Field(default=None, max_length=1024)
    version: int = Field(default=1, ge=1, le=999)


class TranslationApprovalRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    translation_path: str = Field(min_length=1, max_length=1024)
    reading_path: str = Field(min_length=1, max_length=1024)
    validation_path: str = Field(min_length=1, max_length=1024)
    version: int = Field(default=1, ge=1, le=999)


class VoiceLockRequest(BaseModel):
    assignments: dict[str, str]
    role_names: dict[str, str] = Field(default_factory=dict)
    translation_gate_path: str = Field(default="qa/translation_gate.json", max_length=1024)
    version: int = Field(default=1, ge=1, le=999)


class AuditionJobRequest(BaseModel):
    """Start the already-authorized bounded audition for one frozen version."""

    version: int = Field(default=1, ge=1, le=999)


class GenerateFullSpeechRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    segments_path: str = Field(min_length=1, max_length=1024)
    voice_lock_path: str = Field(min_length=1, max_length=1024)
    translation_gate_path: str = Field(default="qa/translation_gate.json", max_length=1024)
    profile_id: str | None = Field(default=None, max_length=120)
    output_dir: str = Field(default="full_dub_v1/tts", min_length=1, max_length=1024)
    version: int = Field(default=1, ge=1, le=999)


class RenderJobRequest(BaseModel):
    working_master_path: str = Field(min_length=1, max_length=1024)
    output_path: str = Field(min_length=1, max_length=1024)
    ad_edit_gate_path: str = Field(default="qa/ad_edit_gate.json", max_length=1024)
    translation_gate_path: str = Field(default="qa/translation_gate.json", max_length=1024)
    voice_lock_path: str = Field(min_length=1, max_length=1024)
    authorization_path: str = Field(min_length=1, max_length=1024)
    translation_path: str = Field(min_length=1, max_length=1024)
    tts_manifest_path: str = Field(min_length=1, max_length=1024)
    video_codec: str = "libx264"
    version: int = Field(default=1, ge=1, le=999)


class TranscriptImportRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=1024)
    language: str | None = Field(default=None, max_length=32)
    version: int = Field(default=1, ge=1, le=999)


class EmbeddedSubtitlePlanRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=1024)
    output_path: str = Field(min_length=1, max_length=1024)
    stream_index: int = Field(ge=0, le=999)


class EmbeddedSubtitleJobRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=1024)
    output_path: str | None = Field(default=None, max_length=1024)
    stream_index: int = Field(ge=0, le=999)
    language: str | None = Field(default=None, max_length=32)
    version: int = Field(default=1, ge=1, le=999)


class AdEvidenceRequest(BaseModel):
    source_master_path: str = Field(min_length=1, max_length=1024)
    working_master_path: str = Field(min_length=1, max_length=1024)
    analysis: dict[str, Any] = Field(default_factory=dict)
    source_duration: float = Field(gt=0)
    frame_width: int | None = Field(default=None, gt=0)
    frame_height: int | None = Field(default=None, gt=0)
    frozen_source_path: str | None = Field(default=None, max_length=1024)


class AdEditJobRequest(AdEvidenceRequest):
    version: int = Field(default=1, ge=1, le=999)


class CoverSourceJobRequest(BaseModel):
    working_master_path: str = Field(min_length=1, max_length=1024)
    at_seconds: float = Field(ge=0)
    output_path: str | None = Field(default=None, max_length=1024)
    version: int = Field(default=1, ge=1, le=999)


class PublicationPackageRequest(BaseModel):
    video_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$",
    )
    version: int = Field(default=1, ge=1, le=999)
    final_video_path: str = Field(min_length=1, max_length=1024)
    final_machine_qa_path: str = Field(min_length=1, max_length=1024)
    production_gate_path: str = Field(
        default="qa/production_gate.json", min_length=1, max_length=1024
    )
    ad_edit_gate_path: str = Field(
        default="qa/ad_edit_gate.json", min_length=1, max_length=1024
    )
    source_chapters: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    source_to_working_path: str = Field(
        default="qa/source_to_edit_timeline.json", min_length=1, max_length=1024
    )
    working_to_final_path: str = Field(min_length=1, max_length=1024)
    approved_chapter_count: int | None = Field(default=None, ge=1, le=1000)
    titles: list[str] = Field(min_length=1, max_length=5)
    description: str = Field(min_length=1, max_length=30000)
    books: list[str] = Field(default_factory=list, max_length=500)
    original_video_url: str = Field(min_length=1, max_length=4096)
    allow_single_title: bool = False
    foreign_names_verified: bool
    removed_promotion_categories: list[str] = Field(default_factory=list, max_length=100)
    cover_source_path: str = Field(min_length=1, max_length=1024)
    cover_title: str = Field(min_length=1, max_length=240)
    cover_source_authorized: bool
    cover_source_clean_verified: bool
    cover_identity_verified: bool
    cover_text_verified: bool
    cover_design_notes: str = Field(
        default="授权源帧与标题分区重排，保留完整源帧内容。",
        max_length=2000,
    )


def _safe_base_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("接口地址不能包含账号、密钥、查询参数或片段")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https" and parsed.hostname:
        return value.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in local_hosts:
        return value.rstrip("/")
    raise ValueError("自定义接口地址必须使用 HTTPS；本机 localhost 可以使用 HTTP")


def _public_profile(profile: dict[str, Any], credentials: CredentialBroker) -> dict[str, Any]:
    profile = dict(profile)
    reference = profile.pop("credential_ref", None)
    profile["credential"] = credentials.status(reference)
    if profile.get("base_url"):
        parsed = urllib.parse.urlparse(str(profile["base_url"]))
        profile["base_url"] = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
        ).rstrip("/")
    return profile


class WorkbenchContext:
    def __init__(
        self,
        settings: WorkbenchSettings,
        credentials: CredentialBroker | None = None,
    ) -> None:
        settings.ensure_directories()
        self.settings = settings
        self.database = WorkbenchDatabase(settings.database_path)
        self.database.initialize()
        self.credentials = credentials or CredentialBroker.from_environment()
        self.library = ProjectLibrary(self.database, settings)
        self.workflows = WorkflowService(
            self.database,
            self.library,
            Path(__file__).resolve().parents[2],
        )
        self.workflows.seed()
        self.runner = LocalTaskRunner(self.database, max_workers=2, worker_id=settings.worker_id)
        self.media = MediaService(self.database, self.library, settings)
        self.production = ProductionJobs(self.database, self.library, self.credentials)
        self.artifacts = WorkflowArtifactService(self.database, self.library)
        self.media_artifacts = MediaArtifactJobs(
            self.database,
            self.library,
            settings,
            self.artifacts,
        )
        self.stage_gates = StageGateService(
            self.database, self.library, settings.worker_id
        )
        self.runner.register("media.probe", self.media.probe_handler)
        self.runner.register("media.download", self.media.download_handler)
        self.runner.register(
            "media.extract_embedded_subtitle",
            self.media_artifacts.extract_embedded_subtitle_handler,
        )
        self.runner.register("media.apply_ad_edit", self.media_artifacts.ad_edit_handler)
        self.runner.register(
            "media.extract_cover_source",
            self.media_artifacts.cover_source_handler,
        )
        self.runner.register("provider.llm.translation", self.production.translation_handler)
        self.runner.register("provider.speech.audition", self.production.audition_handler)
        self.runner.register("provider.speech.synthesize", self.production.speech_handler)
        self.runner.register("production.render", self.production.render_handler)
        self.runner.recover_interrupted()


def create_workbench_router(
    settings: WorkbenchSettings | None = None,
    credentials: CredentialBroker | None = None,
) -> tuple[APIRouter, WorkbenchContext]:
    context = WorkbenchContext(settings or WorkbenchSettings.from_environment(), credentials)
    router = APIRouter(tags=["workbench-v2"])

    @router.get("/api/workbench/health")
    def workbench_health() -> dict[str, Any]:
        projects = context.database.list_projects()
        active_tasks = context.database.list_tasks(active_only=True)
        return {
            "status": "ok",
            "schema_version": 1,
            "projects": len(projects),
            "active_tasks": len(active_tasks),
            "credential_backend": (
                "os_keyring" if context.credentials.store.persistent else "process_memory"
            ),
            "environment": context.settings.public_environment(),
        }

    @router.get("/api/library/projects")
    def list_projects(status: str | None = Query(default=None)) -> dict[str, Any]:
        if status and status not in PROJECT_STATUSES:
            raise HTTPException(status_code=422, detail="未知项目状态")
        projects = context.database.list_projects(status)
        return {"projects": projects, "total": len(projects)}

    @router.post("/api/library/projects", status_code=201)
    def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
        try:
            return context.library.create(**request.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=f"无法创建项目目录：{type(exc).__name__}") from exc

    @router.post("/api/library/projects/attach", status_code=201)
    def attach_project(request: ProjectAttachRequest) -> dict[str, Any]:
        try:
            relative = Path(request.library_path)
            if relative.is_absolute():
                raise ValueError("library_path 必须相对于资料库根目录")
            return context.library.restore_existing(
                context.settings.library_dir / relative
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/library/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        project = context.library.detail(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return project

    @router.post("/api/library/projects/{project_id}/source-binding")
    def rebind_project_source(
        project_id: str, request: ProjectSourceRebindRequest
    ) -> dict[str, Any]:
        try:
            return context.library.rebind_source(project_id, **request.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/library/projects/{project_id}/runs", status_code=201)
    def create_run(project_id: str, request: RunCreateRequest) -> dict[str, Any]:
        try:
            return context.workflows.create_run(project_id, **request.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"项目或预设不存在：{exc.args[0]}") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/library/projects/{project_id}/runs")
    def list_project_runs(project_id: str) -> dict[str, Any]:
        if context.database.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        runs = context.database.list_runs(project_id)
        return {"runs": runs, "total": len(runs)}

    @router.get("/api/workflows/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = context.database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        return run

    @router.post("/api/workflows/runs/{run_id}/ingest/source-transcript")
    def import_source_transcript(
        run_id: str, request: TranscriptImportRequest
    ) -> dict[str, Any]:
        try:
            return context.artifacts.import_transcript(run_id, **request.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行或项目不存在") from exc
        except (ArtifactServiceError, TranscriptError, PathSafetyError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/workflows/runs/{run_id}/ingest/embedded-subtitle-plan")
    def plan_embedded_subtitle(
        run_id: str, request: EmbeddedSubtitlePlanRequest
    ) -> dict[str, Any]:
        try:
            return context.artifacts.embedded_subtitle_plan(
                run_id,
                **request.model_dump(),
                ffmpeg_binary=context.settings.ffmpeg,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行或项目不存在") from exc
        except (ArtifactServiceError, PathSafetyError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/api/workflows/runs/{run_id}/ingest/embedded-subtitle",
        status_code=202,
    )
    def extract_embedded_subtitle(
        run_id: str, request: EmbeddedSubtitleJobRequest
    ) -> dict[str, Any]:
        if context.database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="03",
            kind="media.extract_embedded_subtitle",
            detail="等待 FFmpeg 提取并冻结内嵌字幕",
            progress_mode="exact",
            progress_total=3,
            progress_unit="steps",
            input_payload={**request.model_dump(), "run_id": run_id},
        )
        context.runner.enqueue(task["id"])
        return task

    @router.post("/api/workflows/runs/{run_id}/ad-edit", status_code=202)
    def start_ad_edit(run_id: str, request: AdEditJobRequest) -> dict[str, Any]:
        if context.database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="03",
            kind="media.apply_ad_edit",
            detail="等待按冻结广告证据生成正式工作母版",
            progress_mode="exact",
            progress_total=4,
            progress_unit="steps",
            input_payload={**request.model_dump(), "run_id": run_id},
        )
        context.runner.enqueue(task["id"])
        return task

    @router.post("/api/workflows/runs/{run_id}/cover-source", status_code=202)
    def extract_cover_source(
        run_id: str, request: CoverSourceJobRequest
    ) -> dict[str, Any]:
        if context.database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="08",
            kind="media.extract_cover_source",
            detail="等待从正式工作母版抽取 PNG 封面源帧",
            progress_mode="exact",
            progress_total=2,
            progress_unit="steps",
            input_payload={**request.model_dump(), "run_id": run_id},
        )
        context.runner.enqueue(task["id"])
        return task

    @router.post("/api/workflows/runs/{run_id}/ad-edit-gate")
    def create_ad_edit_gate(run_id: str, request: AdEvidenceRequest) -> dict[str, Any]:
        try:
            return context.artifacts.apply_ad_evidence(run_id, **request.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行或项目不存在") from exc
        except (
            ArtifactServiceError,
            AdEvidenceError,
            PathSafetyError,
            OSError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/workflows/runs/{run_id}/publication-package")
    def create_publication_package(
        run_id: str, request: PublicationPackageRequest
    ) -> dict[str, Any]:
        try:
            return context.artifacts.create_publication_package(
                run_id, **request.model_dump()
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行或项目不存在") from exc
        except (
            ArtifactServiceError,
            PublicationError,
            PathSafetyError,
            OSError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _media_task(run_id: str, kind: str, request: MediaJobRequest) -> dict[str, Any]:
        run = context.database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        project = context.database.get_project(run["project_id"])
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="02",
            kind=kind,
            detail="等待本地运行器",
            input_payload={
                "project_id": project["id"],
                "expected_language": request.expected_language,
                "cookie_file": request.cookie_file,
            },
        )
        context.runner.enqueue(task["id"])
        return task

    @router.post("/api/workflows/runs/{run_id}/probe", status_code=202)
    def start_media_probe(run_id: str, request: MediaJobRequest) -> dict[str, Any]:
        return _media_task(run_id, "media.probe", request)

    @router.post("/api/workflows/runs/{run_id}/download", status_code=202)
    def start_media_download(run_id: str, request: MediaJobRequest) -> dict[str, Any]:
        run = context.database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        project = context.database.get_project(run["project_id"])
        if project is None or project["source_kind"] != "video_url":
            raise HTTPException(status_code=409, detail="这个项目不需要链接下载")
        return _media_task(run_id, "media.download", request)

    @router.post("/api/workflows/runs/{run_id}/translate", status_code=202)
    def start_translation(run_id: str, request: TranslationJobRequest) -> dict[str, Any]:
        run = context.database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        bindings = (run.get("provider_lock") or {}).get("role_bindings") or {}
        if set(bindings) != {"T", "A", "B", "C"}:
            raise HTTPException(status_code=409, detail="请先为 T/A/B/C 配置并冻结翻译 Provider")
        parent = context.database.create_task(
            run_id=run_id,
            stage_key="04",
            kind="provider.llm.translation",
            detail="等待翻译 T",
            progress_mode="checkpointed",
            progress_total=4,
            progress_unit="roles",
            input_payload={},
        )
        children = {}
        for role, label, stage in (
            ("T", "翻译 T", "04"),
            ("A", "中文审核 A", "05"),
            ("B", "忠实审核 B", "05"),
            ("C", "裁决 C", "05"),
        ):
            child = context.database.create_task(
                parent_task_id=parent["id"],
                run_id=run_id,
                stage_key=stage,
                kind=f"provider.llm.role_{role.lower()}",
                detail=f"等待{label}",
                progress_mode="exact",
            )
            children[role] = child["id"]
        payload = {
            **request.model_dump(),
            "run_id": run_id,
            "task_id": parent["id"],
            "child_tasks": children,
        }
        # Replace the initially empty frozen input before the task can start.
        context.database.update_task(parent["id"], input=payload)
        context.runner.enqueue(parent["id"])
        refreshed = context.database.get_task(parent["id"])
        assert refreshed is not None
        refreshed["children"] = [context.database.get_task(task_id) for task_id in children.values()]
        return refreshed

    @router.post("/api/workflows/runs/{run_id}/synthesize", status_code=202)
    def start_speech(run_id: str, request: SpeechJobRequest) -> dict[str, Any]:
        run = context.database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        profile_id = request.profile_id or (run.get("provider_lock") or {}).get("speech_profile_id")
        if not profile_id:
            raise HTTPException(status_code=409, detail="请先配置并冻结语音 Provider")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="07",
            kind="provider.speech.synthesize",
            detail="等待全文 TTS",
            progress_mode="exact",
            progress_unit="segments",
            input_payload={**request.model_dump(), "profile_id": profile_id, "run_id": run_id},
        )
        task = context.database.update_task(
            task["id"],
            input={
                **request.model_dump(),
                "profile_id": profile_id,
                "run_id": run_id,
                "task_id": task["id"],
            },
        )
        context.runner.enqueue(task["id"])
        return task

    @router.post("/api/workflows/runs/{run_id}/chapter-reading")
    def create_chapter_reading(run_id: str, request: ChapterReadingRequest) -> dict[str, Any]:
        try:
            return context.stage_gates.build_reading(
                run_id,
                translation_path=request.translation_path,
                chapters=request.chapters,
                timeline_path=request.timeline_path,
                version=request.version,
            )
        except (StageGateError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/workflows/runs/{run_id}/approve-translation")
    def approve_translation(run_id: str, request: TranslationApprovalRequest) -> dict[str, Any]:
        try:
            return context.stage_gates.approve_translation(
                run_id,
                command=request.command,
                translation_path=request.translation_path,
                reading_path=request.reading_path,
                validation_path=request.validation_path,
                version=request.version,
            )
        except (StageGateError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/workflows/runs/{run_id}/voice-lock")
    def lock_voices(run_id: str, request: VoiceLockRequest) -> dict[str, Any]:
        try:
            return context.stage_gates.lock_voices(
                run_id,
                assignments=request.assignments,
                role_names=request.role_names,
                translation_gate_path=request.translation_gate_path,
                version=request.version,
            )
        except (StageGateError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/workflows/runs/{run_id}/audition", status_code=202)
    def start_audition(run_id: str, request: AuditionJobRequest) -> dict[str, Any]:
        """Generate a bounded audio-only audition; voice-lock is its authorization."""

        try:
            prepared = context.production.prepare_audition(
                run_id,
                version=request.version,
            )
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Avoid concurrent duplicate paid work.  Completed retries are safe and
        # run through the provider ledger/cache, but an active matching task is
        # returned directly.
        active = context.database.fetch_all(
            """SELECT * FROM tasks WHERE run_id=? AND kind='provider.speech.audition'
            AND status IN ('queued','running','pausing','waiting_provider','waiting_worker')
            ORDER BY created_at DESC""",
            (run_id,),
        )
        for task in active:
            task_input = task.get("input") or {}
            if (
                int(task_input.get("version") or 0) == request.version
                and task_input.get("authorization_path")
                == prepared["authorization_path"]
            ):
                return {**task, "audition": prepared, "deduplicated": True}

        task = context.database.create_task(
            run_id=run_id,
            stage_key="06",
            kind="provider.speech.audition",
            detail="音色锁已授权，等待约一分钟原速试听",
            progress_mode="exact",
            progress_total=int(prepared["selected_segment_count"]) + 1,
            progress_unit="steps",
            input_payload={},
        )
        payload = {
            **prepared,
            "run_id": run_id,
            "task_id": task["id"],
            "ffmpeg": context.settings.ffmpeg,
            "ffprobe": context.settings.ffprobe,
        }
        task = context.database.update_task(task["id"], input=payload)
        context.runner.enqueue(task["id"])
        return {**task, "audition": prepared, "deduplicated": False}

    @router.post("/api/workflows/runs/{run_id}/generate-full", status_code=202)
    def generate_full_speech(run_id: str, request: GenerateFullSpeechRequest) -> dict[str, Any]:
        try:
            prepared = context.production.prepare_full_speech_authorization(
                run_id,
                command=request.command,
                segments_path=request.segments_path,
                voice_lock_path=request.voice_lock_path,
                translation_gate_path=request.translation_gate_path,
                profile_id=request.profile_id,
                version=request.version,
            )
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        run = context.database.get_run(run_id)
        assert run is not None
        profile_id = request.profile_id or (run.get("provider_lock") or {}).get("speech_profile_id")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="07",
            kind="provider.speech.synthesize",
            detail="全文指令与 dry-run 已通过，等待原速 TTS",
            progress_mode="exact",
            progress_unit="segments",
            input_payload={},
        )
        payload = {
            "segments_path": request.segments_path,
            "authorization_path": prepared["path"],
            "translation_gate_path": request.translation_gate_path,
            "profile_id": profile_id,
            "output_dir": request.output_dir,
            "run_id": run_id,
            "task_id": task["id"],
        }
        task = context.database.update_task(task["id"], input=payload)
        context.runner.enqueue(task["id"])
        return {**task, "authorization": prepared["authorization"]}

    @router.post("/api/workflows/runs/{run_id}/render", status_code=202)
    def start_render(run_id: str, request: RenderJobRequest) -> dict[str, Any]:
        if context.database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        task = context.database.create_task(
            run_id=run_id,
            stage_key="07",
            kind="production.render",
            detail="等待 production gate 与正式渲染",
            progress_mode="checkpointed",
            progress_total=4,
            progress_unit="gates",
            input_payload={},
        )
        payload = {
            **request.model_dump(),
            "run_id": run_id,
            "task_id": task["id"],
            "ffmpeg": context.settings.ffmpeg,
            "ffprobe": context.settings.ffprobe,
        }
        task = context.database.update_task(task["id"], input=payload)
        context.runner.enqueue(task["id"])
        return task

    @router.get("/api/workflow-presets")
    def list_presets() -> dict[str, Any]:
        public = []
        for preset in context.database.list_workflow_presets():
            pack = preset.pop("prompt_pack", {})
            public.append(
                {
                    **preset,
                    "editable_fields": pack.get("editable_fields", []),
                    "fallback_policy": pack.get("fallback_policy", "manual"),
                    "role_summary": {
                        key: value.get("purpose", "")
                        for key, value in (pack.get("roles") or {}).items()
                    },
                }
            )
        return {"presets": public, "total": len(public)}

    @router.get("/api/providers/profiles")
    def list_provider_profiles() -> dict[str, Any]:
        profiles = [
            _public_profile(profile, context.credentials)
            for profile in context.database.list_provider_profiles()
        ]
        return {"profiles": profiles, "total": len(profiles)}

    @router.get("/api/providers/catalog")
    def provider_catalog() -> dict[str, Any]:
        registered = set(builtin_registry().registered())
        providers = [
            value
            for value in PROVIDER_CATALOG
            if (value["provider_id"], value["service_kind"]) in registered
        ]
        return {"providers": providers, "total": len(providers)}

    @router.get("/api/providers/profiles/{profile_id}/catalog")
    def provider_profile_catalog(profile_id: str) -> dict[str, Any]:
        """Return the non-secret model or voice choices visible to a profile.

        This endpoint never performs generation.  Speech adapters without a
        vendor voice-list endpoint return the explicit ``config.voices`` rows
        frozen in the profile instead.
        """

        profile = context.database.get_provider_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Provider 配置不存在")
        secret = context.credentials.resolve(profile.get("credential_ref"))
        if not secret:
            raise HTTPException(status_code=409, detail="请先为这个 Provider 保存凭证")
        try:
            default_urls = {
                "minimax-llm": "https://api.minimax.cn/v1",
                "minimax-speech": "https://api.minimax.cn",
            }
            base_url = profile.get("base_url") or default_urls.get(profile["provider_id"])
            if not base_url:
                raise ValueError("这个 Provider 需要配置接口地址")
            adapter = builtin_registry().create(
                ProviderProfile(
                    profile_id=profile["id"],
                    provider_id=profile["provider_id"],
                    kind=profile["service_kind"],
                    base_url=base_url,
                    model=profile["model"],
                    credential_ref=profile.get("credential_ref"),
                    options=profile.get("config", {}),
                    enabled=profile["enabled"],
                ),
                secret,
            )
            if profile["service_kind"] == "llm":
                models = [asdict(value) for value in adapter.list_models()]  # type: ignore[attr-defined]
                return {
                    "profile_id": profile_id,
                    "service_kind": "llm",
                    "models": models,
                    "voices": [],
                    "total": len(models),
                    "generation_performed": False,
                }
            voices = [asdict(value) for value in adapter.list_voices()]  # type: ignore[attr-defined]
            return {
                "profile_id": profile_id,
                "service_kind": "speech",
                "models": [],
                "voices": voices,
                "total": len(voices),
                "generation_performed": False,
            }
        except Exception as exc:
            error_code = getattr(exc, "code", "provider_catalog_failed")
            raise HTTPException(
                status_code=502,
                detail={"code": error_code, "message": str(exc)},
            ) from exc
        finally:
            secret = ""

    @router.post("/api/providers/profiles", status_code=201)
    def save_provider_profile(request: ProviderProfileRequest) -> dict[str, Any]:
        profile_id = request.id or new_id("provider")
        if not PROFILE_ID.fullmatch(profile_id):
            raise HTTPException(status_code=422, detail="Provider 配置 ID 含有不支持的字符")
        try:
            base_url = _safe_base_url(request.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        provider_id = PROVIDER_ALIASES.get(request.provider_id, request.provider_id)
        registered = set(builtin_registry().registered())
        if (provider_id, request.service_kind) not in registered:
            raise HTTPException(status_code=422, detail="尚未安装这个 Provider 适配器")
        default_urls = {
            "minimax-llm": "https://api.minimax.cn/v1",
            "minimax-speech": "https://api.minimax.cn",
        }
        resolved_base_url = base_url or default_urls.get(provider_id)
        if not resolved_base_url:
            raise HTTPException(status_code=422, detail="这个 Provider 需要配置接口地址")
        try:
            ProviderProfile(
                profile_id=profile_id,
                provider_id=provider_id,
                kind=request.service_kind,
                base_url=resolved_base_url,
                model=request.model,
                options=request.config,
                enabled=request.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        existing = context.database.get_provider_profile(profile_id)
        credential_ref = existing.get("credential_ref") if existing else None
        if request.api_key is not None:
            secret = request.api_key.get_secret_value()
            try:
                credential_ref = context.credentials.save(profile_id, secret)
            finally:
                secret = ""
        profile = context.database.upsert_provider_profile(
            {
                "id": profile_id,
                "service_kind": request.service_kind,
                "provider_id": provider_id,
                "display_name": request.display_name,
                "base_url": base_url,
                "model": request.model,
                "credential_ref": credential_ref,
                "config": request.config,
                "capability": existing.get("capability", {}) if existing else {},
                "enabled": request.enabled,
                "created_at": existing.get("created_at") if existing else None,
            }
        )
        context.database.emit_event(
            "provider.saved",
            {"profile_id": profile_id, "provider_id": provider_id},
        )
        return _public_profile(profile, context.credentials)

    @router.delete("/api/providers/profiles/{profile_id}/credential")
    def delete_provider_credential(profile_id: str) -> dict[str, Any]:
        profile = context.database.get_provider_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Provider 配置不存在")
        context.credentials.remove(profile.get("credential_ref"))
        profile = context.database.upsert_provider_profile(
            {
                **profile,
                "credential_ref": None,
                "config": profile.get("config", {}),
                "capability": profile.get("capability", {}),
            }
        )
        return _public_profile(profile, context.credentials)

    @router.post("/api/providers/profiles/{profile_id}/probe")
    def probe_provider(profile_id: str) -> dict[str, Any]:
        profile = context.database.get_provider_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Provider 配置不存在")
        secret = context.credentials.resolve(profile.get("credential_ref"))
        if not secret:
            raise HTTPException(status_code=409, detail="请先为这个 Provider 保存凭证")
        try:
            default_urls = {
                "minimax-llm": "https://api.minimax.cn/v1",
                "minimax-speech": "https://api.minimax.cn",
            }
            base_url = profile.get("base_url") or default_urls.get(profile["provider_id"])
            if not base_url:
                raise ValueError("这个 Provider 需要配置接口地址")
            provider_profile = ProviderProfile(
                profile_id=profile["id"],
                provider_id=profile["provider_id"],
                kind=profile["service_kind"],
                base_url=base_url,
                model=profile["model"],
                credential_ref=profile.get("credential_ref"),
                options=profile.get("config", {}),
                enabled=profile["enabled"],
            )
            report = asdict(builtin_registry().create(provider_profile, secret).probe())
        except ImportError as exc:
            raise HTTPException(status_code=501, detail="Provider 适配器尚未安装") from exc
        except Exception as exc:
            error_code = getattr(exc, "code", "provider_probe_failed")
            raise HTTPException(status_code=502, detail={"code": error_code, "message": str(exc)}) from exc
        finally:
            secret = ""
        updated = context.database.upsert_provider_profile(
            {
                **profile,
                "config": profile.get("config", {}),
                "capability": report.get("capabilities", {}),
            }
        )
        return {"profile": _public_profile(updated, context.credentials), "report": report}

    @router.get("/api/jobs")
    def list_jobs(active_only: bool = Query(default=False)) -> dict[str, Any]:
        tasks = context.database.list_tasks(active_only=active_only)
        return {"jobs": tasks, "total": len(tasks)}

    @router.get("/api/jobs/{task_id}")
    def get_job(task_id: str) -> dict[str, Any]:
        task = context.database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        children = context.database.fetch_all(
            "SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at", (task_id,)
        )
        task["children"] = children
        task["provider_requests"] = context.database.list_provider_requests(task_id)
        return task

    @router.get("/api/providers/requests")
    def list_provider_requests(task_id: str | None = Query(default=None)) -> dict[str, Any]:
        requests = context.database.list_provider_requests(task_id)
        return {"requests": requests, "total": len(requests)}

    @router.post("/api/jobs/{task_id}/pause")
    def pause_job(task_id: str, _request: TaskActionRequest | None = None) -> dict[str, Any]:
        task = context.database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] not in {"queued", "running", "retry_wait", "waiting_provider"}:
            raise HTTPException(status_code=409, detail="当前任务状态不能暂停")
        if task["status"] == "running":
            try:
                context.runner.request_pause(task_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            refreshed = context.database.get_task(task_id)
            assert refreshed is not None
            return refreshed
        return context.database.update_task(task_id, status="paused", detail="已在队列检查点暂停")

    @router.post("/api/jobs/{task_id}/resume")
    def resume_job(task_id: str, request: TaskActionRequest | None = None) -> dict[str, Any]:
        task = context.database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] == "blocked_uncertain":
            raise HTTPException(status_code=409, detail="请先核对可能已计费的请求，不能直接重试")
        if task["status"] == "repair_required" and not (request and request.repair_evidence):
            raise HTTPException(status_code=409, detail="修复后恢复需要新的修复证据")
        if task["status"] not in {
            "paused",
            "repair_required",
            "waiting_provider",
            "waiting_worker",
        }:
            raise HTTPException(status_code=409, detail="当前任务状态不能继续")
        resumed = context.database.update_task(task_id, status="queued", detail="已加入恢复队列")
        try:
            context.runner.enqueue(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return resumed

    @router.post("/api/jobs/{task_id}/cancel")
    def cancel_job(task_id: str, _request: TaskActionRequest | None = None) -> dict[str, Any]:
        task = context.database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] == "blocked_uncertain":
            raise HTTPException(status_code=409, detail="计费状态待核对时不能用取消掩盖请求结果")
        if task["status"] in {"completed", "cancelled", "superseded"}:
            raise HTTPException(status_code=409, detail="任务已经结束")
        if task["status"] == "running":
            try:
                context.runner.request_cancel(task_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            refreshed = context.database.get_task(task_id)
            assert refreshed is not None
            return refreshed
        return context.database.update_task(
            task_id,
            status="cancelled",
            detail="已在队列中取消",
            completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @router.get("/api/events")
    def stream_events(request: Request, after: int = Query(default=0, ge=0)) -> StreamingResponse:
        header_id = request.headers.get("last-event-id")
        cursor = max(after, int(header_id) if header_id and header_id.isdigit() else 0)

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            last_keepalive = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                events = context.database.events_after(cursor)
                if events:
                    for event in events:
                        cursor = int(event["id"])
                        yield (
                            f"id: {cursor}\n"
                            f"event: {event['event_type']}\n"
                            f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
                        )
                    last_keepalive = time.monotonic()
                elif time.monotonic() - last_keepalive >= 15:
                    yield ": keepalive\n\n"
                    last_keepalive = time.monotonic()
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router, context
