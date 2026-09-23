"""Workflow preset locking and eight-stage run creation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .database import WorkbenchDatabase
from .library import ProjectLibrary, atomic_json, sha256_file

STAGES = [
    {"key": "01", "title": "立项与工作流锁", "progress_mode": "gate"},
    {"key": "02", "title": "下载与源文件验收", "progress_mode": "exact"},
    {"key": "03", "title": "广告处理、分离与转写", "progress_mode": "checkpointed"},
    {"key": "04", "title": "翻译 T", "progress_mode": "exact"},
    {"key": "05", "title": "双审、裁决与阅读稿", "progress_mode": "checkpointed"},
    {"key": "06", "title": "角色与选音", "progress_mode": "gate"},
    {"key": "07", "title": "TTS、对轴与渲染", "progress_mode": "checkpointed"},
    {"key": "08", "title": "QA、封面与发布包", "progress_mode": "checkpointed"},
]


class WorkflowService:
    def __init__(self, database: WorkbenchDatabase, library: ProjectLibrary, app_root: Path) -> None:
        self.database = database
        self.library = library
        self.app_root = app_root
        self.prompt_pack_path = app_root / "backend" / "prompt_packs" / "quality_zh_v1.json"

    def seed(self) -> None:
        prompt_pack = json.loads(self.prompt_pack_path.read_text(encoding="utf-8"))
        self.database.seed_workflow_preset(
            preset_id="quality-zh-v1",
            name=str(prompt_pack["name"]),
            version=int(prompt_pack["version"]),
            description="T 直译、A/B 独立全文审核、C 裁决；原速语音与画面重定时。",
            locked=True,
            prompt_pack=prompt_pack,
            prompt_pack_sha256=sha256_file(self.prompt_pack_path),
        )

    def create_run(
        self,
        project_id: str,
        *,
        preset_id: str = "quality-zh-v1",
        role_bindings: Mapping[str, str] | None = None,
        speech_profile_id: str | None = None,
    ) -> dict[str, Any]:
        project = self.database.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        preset = self.database.get_workflow_preset(preset_id)
        if preset is None:
            raise KeyError(preset_id)
        role_bindings = {str(key): str(value) for key, value in (role_bindings or {}).items()}
        unknown_roles = set(role_bindings) - {"T", "A", "B", "C"}
        if unknown_roles:
            raise ValueError(f"Unknown role bindings: {sorted(unknown_roles)}")
        profile_ids = set(role_bindings.values()) | ({speech_profile_id} if speech_profile_id else set())
        profiles: dict[str, dict[str, Any]] = {}
        for profile_id in profile_ids:
            profile = self.database.get_provider_profile(str(profile_id))
            if profile is None or not profile["enabled"]:
                raise ValueError(f"Provider profile is not available: {profile_id}")
            profiles[str(profile_id)] = profile

        provider_lock = {
            "schema_version": 1,
            "fallback_policy": "manual",
            "role_bindings": role_bindings,
            "speech_profile_id": speech_profile_id,
            "profiles": {
                profile_id: {
                    "provider_id": profile["provider_id"],
                    "service_kind": profile["service_kind"],
                    "model": profile["model"],
                    "base_url": profile["base_url"],
                    # Provider options affect endpoint routes, structured
                    # output semantics, pricing and voices.  They are part of
                    # the reproducible run and must not float with later
                    # settings edits.
                    "config": profile["config"],
                    "capability": profile["capability"],
                    "credential_configured": bool(profile.get("credential_ref")),
                }
                for profile_id, profile in profiles.items()
            },
        }
        run_dir = self.library.path_for(project)
        prompt_artifact_relative = (
            "work/prompt_packs/"
            f"{preset['prompt_pack_sha256']}.json"
        )
        prompt_artifact_path = run_dir / prompt_artifact_relative
        atomic_json(prompt_artifact_path, dict(preset["prompt_pack"]))
        prompt_lock = {
            "preset_id": preset_id,
            "name": preset["name"],
            "version": preset["version"],
            "prompt_pack_sha256": preset["prompt_pack_sha256"],
            "artifact": {
                "path": prompt_artifact_relative,
                "sha256": sha256_file(prompt_artifact_path),
            },
            "locked": preset["locked"],
        }
        run = self.database.create_run(
            project_id=project_id,
            preset_id=preset_id,
            stages=STAGES,
            provider_lock=provider_lock,
            prompt_lock=prompt_lock,
        )
        lock = self._workflow_lock(project, run, provider_lock, prompt_lock)
        lock_path = run_dir / "qa" / "workflow_lock.json"
        atomic_json(lock_path, lock)
        self.library.bind_workflow(
            project,
            run_id=run["id"],
            preset_id=preset_id,
            workflow_lock_path=lock_path,
        )
        return self.database.get_run(run["id"]) or run

    def _workflow_lock(
        self,
        project: Mapping[str, Any],
        run: Mapping[str, Any],
        provider_lock: Mapping[str, Any],
        prompt_lock: Mapping[str, Any],
    ) -> dict[str, Any]:
        required = [
            "docs/LOCAL_DUBBING_WORKFLOW.md",
            "docs/TRANSLATION_REVIEW_SOP.md",
            "docs/AD_DETECTION_AND_OVERLAY_SOP.md",
            "docs/workflow.definition.json",
            "docs/EVENT_DRIVEN_EXECUTION_SOP.md",
        ]
        documents = []
        for relative in required:
            path = self.app_root / relative
            if not path.is_file():
                raise FileNotFoundError(path)
            documents.append({"path": relative, "sha256": sha256_file(path)})
        project_file = self.library.path_for(dict(project)) / "PROJECT.md"
        documents.append({"path": "PROJECT.md", "sha256": sha256_file(project_file)})
        return {
            "schema_version": 3,
            "status": "pass",
            "project_id": project["id"],
            "run_id": run["id"],
            "required_documents": documents,
            "provider_lock": provider_lock,
            "prompt_lock": prompt_lock,
            "current_stage": "01",
            "next_gate": "media_format_selection",
            "execution_mode": "local_runner_event_driven",
            "model_progress_polling": "forbidden",
            "document_loading": "first_load_then_hash_check_in_retained_context",
            "agent_handoff": "minimal_frozen_role_packet",
            "media_format_selection": "automatic_after_probe",
            "ad_policy": "detect_then_apply_evidence_based",
            "translation_mode": "provider_agent_direct_quality_first",
            "translation_review": "two_independent_agents_full_coverage",
            "chapter_reading_review": "required_before_translation_gate",
            "chapter_reading_layout": "sentence_aligned_verbatim",
            "translation_approval": "explicit_downstream_command_binds_current_version",
            "chinese_tts_speed": 1.0,
            "sync_strategy": "video_retime_only",
            "preserve_all_formal_working_master_frames": True,
            "audition_authorization": "voice_selection_implies_audition",
            "full_tts_authorization": "user_generate_full_command",
            "publication_package": "required_after_final_machine_qa",
            "publication_text_format": "utf8_txt_only",
            "cover_variants": "16x9_and_4x3",
        }
