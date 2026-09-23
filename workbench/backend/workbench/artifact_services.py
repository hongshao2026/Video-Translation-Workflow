"""Run-scoped ingest, advertisement and publication orchestration.

The lower-level :mod:`ingest` and :mod:`publication` modules are deliberately
pure and reusable.  This service adds the workbench invariants that an HTTP
caller must not be allowed to bypass: every path belongs to the selected
project, the workflow lock belongs to the selected run, immutable gates are
validated with their on-disk hashes, and publication can only start from a
machine-passed project.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .database import WorkbenchDatabase, utc_now
from .gates import validate_gate_file
from .ingest import (
    AdEvidenceError,
    PathSafetyError,
    TranscriptError,
    build_ad_edit_artifacts,
    build_embedded_subtitle_plan,
    import_source_transcript,
    materialize_ad_edit_artifacts,
    safe_project_path,
    sha256_file,
    write_frozen_source,
)
from .library import ProjectLibrary
from .publication import (
    PublicationError,
    build_publication_package_gate,
    create_cover_art,
    create_publication_materials,
    write_final_chapter_timeline,
)


class ArtifactServiceError(RuntimeError):
    """Raised when a run-scoped API invariant is not satisfied."""


class WorkflowArtifactService:
    """Bind deterministic artifact helpers to a project and workflow run."""

    def __init__(self, database: WorkbenchDatabase, library: ProjectLibrary) -> None:
        self.database = database
        self.library = library

    def import_transcript(
        self,
        run_id: str,
        *,
        source_path: str,
        language: str | None,
        version: int,
    ) -> dict[str, Any]:
        run, project, root = self._scope(run_id)
        self._require_workflow_lock(root, run, project)
        if (root / "qa" / "ad_edit_gate.json").exists():
            raise ArtifactServiceError(
                "广告处理门禁已经冻结；不能再替换源文，请新建项目运行"
            )
        source = self._file(root, source_path)
        document = import_source_transcript(
            source,
            project_root=root,
            language=language,
        )
        relative_output = f"work/frozen_source_v{int(version)}.json"
        artifact = write_frozen_source(document, relative_output, project_root=root)
        self._set_state(
            run_id=run_id,
            project_id=project["id"],
            status="running",
            stage="03",
            stage_status="running",
            detail="源字幕已导入并冻结稳定 ID",
            needs_attention=False,
        )
        self.database.emit_event(
            "ingest.transcript_frozen",
            {
                "artifact": artifact,
                "source_file_sha256": document["source_file"]["sha256"],
                "slot_count": document["slot_count"],
            },
            project_id=project["id"],
            run_id=run_id,
        )
        return {
            "status": "pass",
            "artifact": artifact,
            "source_format": document["source_format"],
            "source_language": document["source_language"],
            "slot_count": document["slot_count"],
            "stable_ids": document["stable_ids"],
        }

    def embedded_subtitle_plan(
        self,
        run_id: str,
        *,
        source_path: str,
        output_path: str,
        stream_index: int,
        ffmpeg_binary: str,
    ) -> dict[str, Any]:
        run, project, root = self._scope(run_id)
        self._require_workflow_lock(root, run, project)
        source = self._file(root, source_path)
        # Resolving the output before handing it to the planner makes the API's
        # project-boundary guarantee explicit even though the helper checks it
        # again internally.
        output = safe_project_path(root, output_path)
        plan = build_embedded_subtitle_plan(
            source,
            output,
            stream_index=stream_index,
            project_root=root,
            ffmpeg_binary=ffmpeg_binary,
        )
        return {"status": "planned", "execution": "local_runner_required", "plan": plan.to_dict()}

    def apply_ad_evidence(
        self,
        run_id: str,
        *,
        source_master_path: str,
        working_master_path: str,
        analysis: Mapping[str, Any],
        source_duration: float,
        frame_width: int | None,
        frame_height: int | None,
        frozen_source_path: str | None,
    ) -> dict[str, Any]:
        run, project, root = self._scope(run_id)
        self._require_workflow_lock(root, run, project)
        if (root / "qa" / "translation_gate.json").exists():
            raise ArtifactServiceError(
                "翻译门禁已经冻结；不能再变更广告剪辑与工作母版"
            )
        source = self._file(root, source_master_path)
        working = self._file(root, working_master_path)
        frozen = self._file(root, frozen_source_path) if frozen_source_path else None
        bundle = build_ad_edit_artifacts(
            project_root=root,
            source_master=source,
            working_master=working,
            analysis=analysis,
            source_duration=source_duration,
            frame_width=frame_width,
            frame_height=frame_height,
            frozen_source_path=frozen,
        )
        if bundle.get("status") == "analysis_required":
            self._set_state(
                run_id=run_id,
                project_id=project["id"],
                status="waiting_user",
                stage="03",
                stage_status="waiting_user",
                detail="缺少完整广告语义/画面证据，尚未生成 ad_edit_gate",
                needs_attention=True,
            )
            self.database.emit_event(
                "ad.analysis_required",
                {"blockers": list(bundle.get("blockers") or [])},
                project_id=project["id"],
                run_id=run_id,
            )
            return dict(bundle)

        written = materialize_ad_edit_artifacts(bundle, project_root=root)
        gate_path = root / "qa" / "ad_edit_gate.json"
        validation = validate_gate_file(
            gate_path,
            kind="ad_edit_gate",
            artifact_root=root,
        )
        if not validation.valid:
            raise ArtifactServiceError(
                "广告门禁落盘后校验失败：" + "; ".join(validation.errors)
            )
        self._set_state(
            run_id=run_id,
            project_id=project["id"],
            status="running",
            stage="03",
            stage_status="completed",
            detail="广告证据与工作母版门禁已通过",
            needs_attention=False,
        )
        self.database.emit_event(
            "ad.edit_gate_passed",
            {
                "decision": bundle["ad_edit_gate"]["decision"],
                "gate_sha256": sha256_file(gate_path),
            },
            project_id=project["id"],
            run_id=run_id,
        )
        return {
            "status": "pass",
            "decision": bundle["ad_edit_gate"]["decision"],
            "gate": {
                "path": "qa/ad_edit_gate.json",
                "sha256": sha256_file(gate_path),
            },
            "artifacts": written,
        }

    def create_publication_package(
        self,
        run_id: str,
        *,
        video_id: str,
        version: int,
        final_video_path: str,
        final_machine_qa_path: str,
        production_gate_path: str,
        ad_edit_gate_path: str,
        source_chapters: Sequence[Mapping[str, Any]],
        source_to_working_path: str,
        working_to_final_path: str,
        titles: Sequence[str],
        description: str,
        books: Sequence[str],
        original_video_url: str,
        allow_single_title: bool,
        foreign_names_verified: bool,
        removed_promotion_categories: Sequence[str],
        cover_source_path: str,
        cover_title: str,
        cover_source_authorized: bool,
        cover_source_clean_verified: bool,
        cover_identity_verified: bool,
        cover_text_verified: bool,
        cover_design_notes: str,
        approved_chapter_count: int | None,
    ) -> dict[str, Any]:
        run, project, root = self._scope(run_id)
        self._require_workflow_lock(root, run, project)
        if project["current_stage"] != "08" or project["status"] not in {
            "machine_passed",
            "repair_required",
            "waiting_user",
        }:
            raise ArtifactServiceError(
                "项目尚未进入机器 QA 通过后的发布阶段，不能生成正式发布包"
            )

        final_video = self._file(root, final_video_path)
        final_machine_qa = self._file(root, final_machine_qa_path)
        production_gate = self._file(root, production_gate_path)
        ad_edit_gate = self._file(root, ad_edit_gate_path)
        source_to_working = self._file(root, source_to_working_path)
        working_to_final = self._file(root, working_to_final_path)
        cover_source = self._file(root, cover_source_path)

        self._require_gate(root, production_gate, "production_gate")
        self._require_gate(root, ad_edit_gate, "ad_edit_gate")
        production_payload = self._json_object(production_gate, "production gate")
        ad_payload = self._json_object(ad_edit_gate, "ad edit gate")
        self._require_bound_input(
            root,
            ad_payload.get("timeline_mapping"),
            source_to_working,
            "ad_edit_gate.timeline_mapping",
        )
        self._require_bound_input(
            root,
            production_payload.get("video_retime_plan"),
            working_to_final,
            "production_gate.video_retime_plan",
        )
        if (
            project.get("source_kind") == "video_url"
            and str(original_video_url).strip() != str(project.get("source") or "").strip()
        ):
            raise ArtifactServiceError("发布简介必须保留当前项目的原视频链接")
        machine_payload = self._json_object(final_machine_qa, "最终机器 QA")
        final_sha = sha256_file(final_video)
        if machine_payload.get("status") != "pass":
            raise ArtifactServiceError("最终机器 QA 未通过")
        if machine_payload.get("sha256") != final_sha:
            raise ArtifactServiceError("最终机器 QA 与当前成片哈希不匹配")

        timeline = write_final_chapter_timeline(
            root,
            video_id,
            version,
            final_video_path=final_video,
            final_machine_qa_path=final_machine_qa,
            source_chapters=source_chapters,
            source_to_working_path=source_to_working,
            working_to_final_path=working_to_final,
            approved_chapter_count=approved_chapter_count,
        )
        timeline_path = root / "qa" / f"chapter_timeline_v{int(version)}.json"
        materials = create_publication_materials(
            root,
            video_id,
            version,
            final_video_path=final_video,
            final_machine_qa_path=final_machine_qa,
            chapter_timeline_path=timeline_path,
            titles=titles,
            description=description,
            books=books,
            original_video_url=original_video_url,
            allow_single_title=allow_single_title,
            foreign_names_verified=foreign_names_verified,
            removed_promotion_categories=removed_promotion_categories,
        )
        if materials.get("status") != "pass":
            self._publication_failed(run_id, project["id"], "发布文字材料校验失败")
            raise ArtifactServiceError(
                "发布文字材料校验失败：" + json.dumps(materials.get("issues") or [], ensure_ascii=False)
            )
        cover = create_cover_art(
            root,
            video_id,
            version,
            source_image_path=cover_source,
            title=cover_title,
            source_authorized=cover_source_authorized,
            source_clean_verified=cover_source_clean_verified,
            identity_verified=cover_identity_verified,
            text_verified=cover_text_verified,
            design_notes=cover_design_notes,
        )
        if cover.get("status") != "pass":
            self._publication_failed(run_id, project["id"], "封面校验失败或生成能力不可用")
            raise ArtifactServiceError(
                "封面校验失败或生成能力不可用："
                + json.dumps(cover.get("failure_codes") or cover.get("issues") or [], ensure_ascii=False)
            )

        materials_path = root / "qa" / f"publication_materials_v{int(version)}.json"
        cover_path = root / "qa" / f"cover_art_v{int(version)}.json"
        gate = build_publication_package_gate(
            root,
            version,
            final_video_path=final_video,
            ad_edit_gate_path=ad_edit_gate,
            final_machine_qa_path=final_machine_qa,
            chapter_timeline_path=timeline_path,
            publication_materials_path=materials_path,
            cover_art_path=cover_path,
        )
        gate_path = root / "qa" / f"publication_package_gate_v{int(version)}.json"
        validation = validate_gate_file(
            gate_path,
            kind="publication_package_gate",
            artifact_root=root,
        )
        if gate.get("status") != "pass" or not validation.valid:
            self._publication_failed(run_id, project["id"], "发布包门禁未通过")
            errors = list(gate.get("failure_codes") or []) + list(validation.errors)
            raise ArtifactServiceError("发布包门禁未通过：" + "; ".join(errors))

        text_ref = gate.get("publication_text") or {}
        text_path = self._file(root, str(text_ref.get("path") or ""))
        raw_text = text_path.read_bytes()
        if raw_text.startswith(b"\xef\xbb\xbf"):
            raise ArtifactServiceError("发布文字不得包含 UTF-8 BOM")
        copyable_text = raw_text.decode("utf-8")
        if sha256_file(text_path) != text_ref.get("sha256"):
            raise ArtifactServiceError("发布文字在读取时发生哈希变化")

        self._set_state(
            run_id=run_id,
            project_id=project["id"],
            status="waiting_user",
            stage="08",
            stage_status="waiting_user",
            detail="发布包机器门禁已通过，等待用户人工抽看",
            needs_attention=True,
        )
        self.database.emit_event(
            "publication.package_ready_for_review",
            {
                "gate_path": gate_path.relative_to(root).as_posix(),
                "gate_sha256": sha256_file(gate_path),
                "delivery_state": "ready_for_human_review",
            },
            project_id=project["id"],
            run_id=run_id,
        )
        return {
            "status": "pass",
            "delivery_state": "ready_for_human_review",
            "project_status": "waiting_user",
            "encoding": "utf-8",
            "copyable_text": copyable_text,
            "publication_text": dict(text_ref),
            "chapter_timeline": timeline,
            "publication_materials": materials,
            "cover_art": cover,
            "publication_package_gate": gate,
        }

    def _scope(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        run = self.database.get_run(run_id)
        if run is None:
            raise KeyError("run")
        project = self.database.get_project(run["project_id"])
        if project is None:
            raise KeyError("project")
        root = self.library.path_for(project).resolve()
        return run, project, root

    @staticmethod
    def _file(root: Path, path: str | Path | None) -> Path:
        if path is None or not str(path).strip():
            raise PathSafetyError("文件路径不能为空")
        return safe_project_path(root, path, must_exist=True, require_file=True)

    @staticmethod
    def _json_object(path: Path, label: str) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactServiceError(f"{label}不是可读 JSON") from exc
        if not isinstance(payload, dict):
            raise ArtifactServiceError(f"{label}必须是 JSON 对象")
        return payload

    def _require_workflow_lock(
        self,
        root: Path,
        run: Mapping[str, Any],
        project: Mapping[str, Any],
    ) -> None:
        lock_path = root / "qa" / "workflow_lock.json"
        lock = self._json_object(lock_path, "工作流锁")
        if lock.get("run_id") != run["id"] or lock.get("project_id") != project["id"]:
            raise ArtifactServiceError("工作流锁与当前项目或运行不匹配")
        self._require_gate(root, lock_path, "workflow_lock")

    @staticmethod
    def _require_gate(root: Path, path: Path, kind: str) -> None:
        validation = validate_gate_file(path, kind=kind, artifact_root=root)
        if not validation.valid:
            raise ArtifactServiceError(
                f"{kind} 未通过：" + "; ".join(validation.errors)
            )

    @staticmethod
    def _require_bound_input(
        root: Path,
        reference: object,
        actual_path: Path,
        label: str,
    ) -> None:
        if not isinstance(reference, Mapping):
            raise ArtifactServiceError(f"{label} 缺少制品绑定")
        raw_path = reference.get("path")
        expected_sha = str(reference.get("sha256") or "")
        if not isinstance(raw_path, str) or not raw_path:
            raise ArtifactServiceError(f"{label} 缺少路径")
        bound_path = safe_project_path(root, raw_path, must_exist=True, require_file=True)
        if bound_path.resolve() != actual_path.resolve():
            raise ArtifactServiceError(f"{label} 与本次发布输入不是同一文件")
        if sha256_file(bound_path) != expected_sha:
            raise ArtifactServiceError(f"{label} 哈希不匹配")

    def _publication_failed(self, run_id: str, project_id: str, detail: str) -> None:
        self._set_state(
            run_id=run_id,
            project_id=project_id,
            status="repair_required",
            stage="08",
            stage_status="repair_required",
            detail=detail,
            needs_attention=True,
        )

    def _set_state(
        self,
        *,
        run_id: str,
        project_id: str,
        status: str,
        stage: str,
        stage_status: str,
        detail: str,
        needs_attention: bool,
    ) -> None:
        now = utc_now()
        self.database.execute(
            "UPDATE workflow_runs SET status=?,current_stage=?,updated_at=? WHERE id=?",
            (status, stage, now, run_id),
        )
        self.database.execute(
            """UPDATE stage_runs SET status=?,detail=?,updated_at=?
               WHERE run_id=? AND stage_key=?""",
            (stage_status, detail, now, run_id, stage),
        )
        self.database.update_project(
            project_id,
            {
                "status": status,
                "current_stage": stage,
                "needs_attention": needs_attention,
            },
        )


__all__ = [
    "AdEvidenceError",
    "ArtifactServiceError",
    "PathSafetyError",
    "PublicationError",
    "TranscriptError",
    "WorkflowArtifactService",
]
