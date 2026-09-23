"""High-level deterministic stage actions for reading and translation gates."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .audio_timeline import AudioTimelineError, build_tts_segments
from .database import WorkbenchDatabase, utc_now
from .gates import (
    bind_translation_approval,
    build_chapter_reading,
    sha256_file,
    validate_gate,
    validate_gate_file,
)
from .library import ProjectLibrary, atomic_json


class StageGateError(RuntimeError):
    pass


class StageGateService:
    def __init__(self, database: WorkbenchDatabase, library: ProjectLibrary, worker_id: str) -> None:
        self.database = database
        self.library = library
        self.worker_id = worker_id

    @staticmethod
    def _inside(root: Path, relative: str) -> Path:
        value = Path(relative)
        if value.is_absolute():
            raise StageGateError("制品路径必须是项目相对路径")
        path = (root / value).resolve()
        if not path.is_relative_to(root.resolve()):
            raise StageGateError("制品路径超出项目目录")
        return path

    def _context(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        run = self.database.get_run(run_id)
        if run is None:
            raise StageGateError("运行不存在")
        project = self.database.get_project(run["project_id"])
        if project is None:
            raise StageGateError("项目不存在")
        return run, project, self.library.path_for(project)

    def build_reading(
        self,
        run_id: str,
        *,
        translation_path: str,
        chapters: Sequence[Mapping[str, Any]] | None,
        version: int,
        timeline_path: str | None = None,
    ) -> dict[str, Any]:
        _run, project, root = self._context(run_id)
        translation = self._inside(root, translation_path)
        if not translation.is_file():
            raise StageGateError("正式翻译文件不存在")
        video_id = str(project["id"]).removeprefix("prj_")
        reading_relative = f"deliverables/{video_id}_中文阅读版_按章节_v{version}.md"
        validation_relative = f"qa/chapter_reading_validation_v{version}.json"
        timeline = self._inside(root, timeline_path) if timeline_path else None
        report = build_chapter_reading(
            translation,
            chapters,
            self._inside(root, reading_relative),
            self._inside(root, validation_relative),
            video_id=video_id,
            chapter_source="formal_working_master" if chapters else "generated",
            source_to_edit_timeline_path=timeline,
            run_root=root,
        )
        self._set_stage(run_id, "05", "waiting_user", "章节阅读稿已验证，等待整体审阅")
        return {
            "status": "waiting_user",
            "reading_path": reading_relative,
            "validation_path": validation_relative,
            "report": report,
        }

    def approve_translation(
        self,
        run_id: str,
        *,
        command: str,
        translation_path: str,
        reading_path: str,
        validation_path: str,
        version: int,
    ) -> dict[str, Any]:
        _run, _project, root = self._context(run_id)
        translation = self._inside(root, translation_path)
        reading = self._inside(root, reading_path)
        validation = self._inside(root, validation_path)
        approval_relative = f"qa/translation_approval_v{version}.json"
        approval_path = self._inside(root, approval_relative)
        approval = bind_translation_approval(
            command,
            translation_path=translation,
            reading_path=reading,
            validation_path=validation,
            approval_path=approval_path,
            run_root=root,
            version=version,
        )
        gate = self._translation_gate(
            run_id,
            root=root,
            translation=translation,
            reading=reading,
            validation=validation,
            approval_path=approval_path,
            approval=approval,
            version=version,
        )
        result = validate_gate("translation_gate", gate, artifact_root=root)
        if not result.valid:
            failed = {**gate, "status": "fail", "validation_errors": list(result.errors)}
            atomic_json(self._inside(root, f"qa/translation_gate_failed_v{version}.json"), failed)
            raise StageGateError("translation_gate 未通过：" + "; ".join(result.errors))
        gate_path = self._inside(root, "qa/translation_gate.json")
        atomic_json(gate_path, gate)
        atomic_json(self._inside(root, f"qa/translation_gate_v{version}.json"), gate)
        self._set_stage(run_id, "05", "completed", "翻译门禁已绑定当前阅读稿与明确指令")
        self._set_stage(run_id, "06", "ready", "可进入角色与选音")
        return {
            "status": "pass",
            "approval_path": approval_relative,
            "translation_gate_path": "qa/translation_gate.json",
            "gate": gate,
        }

    def lock_voices(
        self,
        run_id: str,
        *,
        assignments: Mapping[str, str],
        role_names: Mapping[str, str] | None,
        translation_gate_path: str,
        version: int,
    ) -> dict[str, Any]:
        run, _project, root = self._context(run_id)
        gate_path = self._inside(root, translation_gate_path)
        gate_result = validate_gate_file(
            gate_path, kind="translation_gate", artifact_root=root
        )
        if not gate_result.valid:
            raise StageGateError("翻译门禁未通过：" + "; ".join(gate_result.errors))
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StageGateError("翻译门禁无法读取") from exc
        final_translation = gate.get("final_translation")
        if not isinstance(final_translation, Mapping):
            raise StageGateError("翻译门禁缺少正式翻译哈希绑定")
        translation_path = self._inside(root, str(final_translation.get("path") or ""))
        if (
            not translation_path.is_file()
            or str(final_translation.get("sha256") or "")
            != sha256_file(translation_path)
        ):
            raise StageGateError("翻译门禁中的正式翻译缺失或哈希不匹配")
        normalized = {
            str(role).strip(): str(voice).strip()
            for role, voice in assignments.items()
            if str(role).strip() and str(voice).strip()
        }
        if not normalized or len(normalized) != len(assignments):
            raise StageGateError("每个角色必须且只能锁定一个非空音色")
        profile_id = str((run.get("provider_lock") or {}).get("speech_profile_id") or "")
        if not profile_id:
            raise StageGateError("当前运行没有冻结语音 Provider")
        names = {str(key): str(value).strip() for key, value in (role_names or {}).items()}
        lock_relative = f"work/voice_selection_locked_v{version}.json"
        qa_relative = f"qa/voice_selection_lock_v{version}.json"
        lock_path = self._inside(root, lock_relative)
        payload = {
            "schema_version": 1,
            "version": version,
            "status": "voice_selection_locked_audition_ready",
            "run_id": run_id,
            "translation_gate": {
                "path": gate_path.relative_to(root).as_posix(),
                "sha256": sha256_file(gate_path),
            },
            "speech_profile_id": profile_id,
            "roles": [
                {
                    "role_id": role,
                    "display_name": names.get(role) or role,
                    "voice_id": voice,
                }
                for role, voice in sorted(normalized.items())
            ],
            "authorization": {
                "paid_one_minute_sample": True,
                "paid_full_tts": False,
                "render": False,
            },
        }
        atomic_json(lock_path, payload)
        segments_relative = f"work/tts_segments_v{version}.json"
        try:
            segments = build_tts_segments(
                project_root=root,
                translation_path=translation_path,
                output_path=self._inside(root, segments_relative),
                version=version,
                assignments=normalized,
                voice_lock_path=lock_path,
                time_basis="working_master",
            )
        except AudioTimelineError as exc:
            raise StageGateError(f"无法从正式翻译生成冻结 TTS 片段：{exc}") from exc
        qa = {
            "schema_version": 1,
            "status": "pass",
            "voice_selection": {
                "path": lock_relative,
                "sha256": sha256_file(lock_path),
            },
            "tts_segments": {
                "path": segments_relative,
                "sha256": str(segments["sha256"]),
            },
            "checks": {
                "translation_gate_pass": True,
                "all_roles_selected_once": True,
                "speech_provider_frozen": True,
                "audition_authorized": True,
                "full_tts_authorized": False,
                "tts_segments_frozen": True,
            },
        }
        atomic_json(self._inside(root, qa_relative), qa)
        self._set_stage(run_id, "06", "completed", "角色与音色已锁定，可直接生成试听")
        self._set_stage(run_id, "07", "ready", "等待生成全片指令或一分钟试听")
        return {
            "status": "pass",
            "voice_lock_path": lock_relative,
            "segments_path": segments_relative,
            "qa_path": qa_relative,
            "paid_audition_authorized": True,
            "paid_full_tts_authorized": False,
        }

    def _translation_gate(
        self,
        run_id: str,
        *,
        root: Path,
        translation: Path,
        reading: Path,
        validation: Path,
        approval_path: Path,
        approval: Mapping[str, Any],
        version: int,
    ) -> dict[str, Any]:
        parent = self.database.fetch_one(
            """SELECT * FROM tasks WHERE run_id=? AND kind='provider.llm.translation'
            AND status='completed' ORDER BY completed_at DESC LIMIT 1""",
            (run_id,),
        )
        if parent is None:
            raise StageGateError("没有找到已完成的 T/A/B/C 翻译任务")
        children = self.database.fetch_all(
            "SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at", (parent["id"],)
        )
        by_role = {
            str(row["kind"]).rsplit("_", 1)[-1].upper(): row
            for row in children
        }
        if set(by_role) != {"T", "A", "B", "C"} or any(
            row["status"] != "completed" for row in by_role.values()
        ):
            raise StageGateError("T/A/B/C 没有全部独立完成")
        manifest_path = self._inside(root, f"qa/translation_agent_t_manifest_v{version}.json")
        audit_a_path = self._inside(root, f"qa/translation_audit_agent_a_v{version}.json")
        audit_b_path = self._inside(root, f"qa/translation_audit_agent_b_v{version}.json")
        decisions_path = self._inside(root, f"qa/translation_audit_decisions_v{version}.json")
        regression_path = self._inside(root, f"qa/translation_regression_v{version}.json")
        for path in (manifest_path, audit_a_path, audit_b_path, decisions_path, regression_path):
            if not path.is_file():
                raise StageGateError(f"翻译证据缺失：{path.name}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        audit_a = json.loads(audit_a_path.read_text(encoding="utf-8-sig"))
        audit_b = json.loads(audit_b_path.read_text(encoding="utf-8-sig"))
        decisions = json.loads(decisions_path.read_text(encoding="utf-8-sig"))
        regression = json.loads(regression_path.read_text(encoding="utf-8-sig"))
        reading_report = json.loads(validation.read_text(encoding="utf-8-sig"))
        stable_ids = [str(value) for value in manifest.get("stable_ids") or []]
        issue_count = len(
            {
                str(row.get("id"))
                for report in (audit_a, audit_b)
                for row in (report.get("issues") or [])
                if isinstance(row, Mapping) and row.get("id")
            }
        )
        decision_count = len(decisions.get("decisions") or [])
        unresolved = max(0, issue_count - decision_count)
        role_ids = [by_role[role]["id"] for role in ("T", "A", "B", "C")]
        roles_are_distinct = len(set(role_ids + [self.worker_id])) == 5

        def artifact(path: Path) -> dict[str, str]:
            return {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}

        coverage = (
            f"{stable_ids[0]}-{stable_ids[-1]} ({len(stable_ids)})" if stable_ids else "empty"
        )
        gate = {
            "schema_version": 1,
            "status": "pass",
            "mode": "provider_agent_direct_quality_first",
            "translator": {
                "id": by_role["T"]["id"],
                "missing": len(manifest.get("missing_ids") or []),
                "coverage": coverage,
                "report": artifact(manifest_path),
            },
            "reviewers": [
                {
                    "id": by_role[role]["id"],
                    "missing": len(report.get("missing_ids") or []),
                    "coverage": coverage,
                    "report": artifact(path),
                }
                for role, report, path in (
                    ("A", audit_a, audit_a_path),
                    ("B", audit_b, audit_b_path),
                )
            ],
            "orchestrator_id": f"{self.worker_id}:{by_role['C']['id']}",
            "roles_are_distinct": roles_are_distinct,
            "unresolved_high": 0 if unresolved == 0 else unresolved,
            "unresolved_total": unresolved,
            "regression_status": regression.get("status"),
            "final_translation": artifact(translation),
            "chapter_reading": {
                **artifact(reading),
                "validation": artifact(validation),
                "status": reading_report.get("status"),
                "missing_slots": int(reading_report.get("missing_slot_count") or 0),
                "duplicate_slots": int(reading_report.get("duplicate_slot_count") or 0),
                "subtitle_text_used_verbatim": reading_report.get("subtitle_text_used_verbatim") is True,
                "subtitle_text_reconstructable_per_stable_id": reading_report.get("subtitle_text_reconstructable_per_stable_id") is True,
                "layout": reading_report.get("chapter_reading_layout"),
            },
            "user_approval": {
                "artifact": artifact(approval_path),
                "approved": approval.get("approved") is True,
                "capture_mode": approval.get("capture_mode"),
                "command": approval.get("command"),
                "reading_sha256": sha256_file(reading),
                "final_translation_sha256": sha256_file(translation),
            },
        }
        return gate

    def _set_stage(self, run_id: str, stage_key: str, status: str, detail: str) -> None:
        now = utc_now()
        self.database.execute(
            """UPDATE stage_runs SET status=?,detail=?,updated_at=?,
            completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,?) ELSE completed_at END
            WHERE run_id=? AND stage_key=?""",
            (status, detail, now, status, now, run_id, stage_key),
        )
        run = self.database.fetch_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
        if run:
            self.database.execute(
                "UPDATE workflow_runs SET status=?,current_stage=?,updated_at=? WHERE id=?",
                ("waiting_user" if status == "waiting_user" else "running", stage_key, now, run_id),
            )
            project_status = "waiting_user" if status == "waiting_user" else "running"
            self.database.execute(
                "UPDATE projects SET status=?,current_stage=?,updated_at=? WHERE id=?",
                (project_status, stage_key, now, run["project_id"]),
            )
