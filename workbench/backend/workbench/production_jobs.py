"""Job handlers that connect frozen workflow runs to model and speech providers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.providers import ProviderProfile, builtin_registry

from .audio_timeline import AudioTimelineError, build_audio_timeline
from .audition import (
    AUDITION_CAPTURE_MODE,
    AUDITION_SCOPE,
    AuditionError,
    assemble_audition_audio,
    plan_audition_segments,
    write_json_once_or_same,
)
from .credentials import CredentialBroker
from .database import WorkbenchDatabase, utc_now
from .gates import sha256_file, validate_gate, validate_gate_file
from .library import ProjectLibrary, atomic_json
from .provider_ledger import with_provider_ledger
from .rendering import (
    artifact_ref,
    build_production_gate_report,
    build_render_plan,
    run_machine_qa,
)
from .runner import ProgressReporter, TaskToken
from .speech import SpeechPipeline, authorization_for_segments
from .translation import TranslationPipeline


class ProductionJobError(RuntimeError):
    pass


class ProductionJobs:
    def __init__(
        self,
        database: WorkbenchDatabase,
        library: ProjectLibrary,
        credentials: CredentialBroker,
    ) -> None:
        self.database = database
        self.library = library
        self.credentials = credentials

    @staticmethod
    def _inside(root: Path, relative: str) -> Path:
        if not relative or Path(relative).is_absolute():
            raise ProductionJobError("任务输入必须使用项目相对路径")
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ProductionJobError("任务输入路径超出项目目录")
        return path

    def _context(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        run = self.database.get_run(run_id)
        if run is None:
            raise ProductionJobError("运行不存在")
        project = self.database.get_project(run["project_id"])
        if project is None:
            raise ProductionJobError("项目不存在")
        return run, project, self.library.path_for(project)

    def _require_workflow_lock(
        self,
        run: Mapping[str, Any],
        project: Mapping[str, Any],
        root: Path,
    ) -> dict[str, Any]:
        lock_path = root / "qa" / "workflow_lock.json"
        lock = self._read_object(lock_path, "工作流锁")
        result = validate_gate("workflow_lock", lock)
        if not result.valid:
            raise ProductionJobError("工作流锁未通过：" + "; ".join(result.errors))
        if (
            str(lock.get("run_id") or "") != str(run["id"])
            or str(lock.get("project_id") or "") != str(project["id"])
            or lock.get("provider_lock") != run.get("provider_lock")
            or lock.get("prompt_lock") != run.get("prompt_lock")
        ):
            raise ProductionJobError("工作流锁没有绑定当前运行、Provider 或提示词版本")
        app_root = Path(__file__).resolve().parents[2]
        documents = lock.get("required_documents")
        if not isinstance(documents, list) or not documents:
            raise ProductionJobError("工作流锁缺少强制文档哈希")
        for binding in documents:
            if not isinstance(binding, Mapping):
                raise ProductionJobError("工作流锁文档绑定结构无效")
            relative = str(binding.get("path") or "")
            raw_path = Path(relative)
            if not relative or raw_path.is_absolute() or ".." in raw_path.parts:
                raise ProductionJobError("工作流锁包含不安全的文档路径")
            document = (root / raw_path) if relative == "PROJECT.md" else (app_root / raw_path)
            document = document.resolve()
            allowed_root = root.resolve() if relative == "PROJECT.md" else app_root.resolve()
            if not document.is_relative_to(allowed_root):
                raise ProductionJobError("工作流锁文档路径越界")
            if (
                not document.is_file()
                or str(binding.get("sha256") or "") != sha256_file(document)
            ):
                raise ProductionJobError(f"工作流锁文档哈希不匹配：{relative}")
        return lock

    @staticmethod
    def _read_object(path: Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionJobError(f"{label}不是可读取的 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ProductionJobError(f"{label}必须是 JSON 对象")
        return value

    def _bound_file(
        self,
        root: Path,
        binding: object,
        label: str,
        *,
        expected_path: Path | None = None,
    ) -> Path:
        if not isinstance(binding, Mapping):
            raise ProductionJobError(f"{label}缺少路径与哈希绑定")
        raw_path = str(binding.get("path") or "")
        expected_sha = str(binding.get("sha256") or "")
        path = self._inside(root, raw_path)
        if expected_path is not None and path != expected_path.resolve():
            raise ProductionJobError(f"{label}没有绑定当前文件")
        if not path.is_file() or len(expected_sha) != 64 or sha256_file(path) != expected_sha:
            raise ProductionJobError(f"{label}文件缺失或哈希不匹配")
        return path

    def _validate_voice_lock(
        self,
        *,
        root: Path,
        run_id: str,
        voice_lock_path: Path,
        translation_gate_path: Path,
        profile_id: str,
    ) -> dict[str, Any]:
        voice_lock = self._read_object(voice_lock_path, "音色锁")
        if voice_lock.get("status") != "voice_selection_locked_audition_ready":
            raise ProductionJobError("音色锁状态无效")
        if str(voice_lock.get("run_id") or "") != run_id:
            raise ProductionJobError("音色锁不属于当前运行")
        if str(voice_lock.get("speech_profile_id") or "") != profile_id:
            raise ProductionJobError("音色锁没有绑定当前语音 Provider")
        self._bound_file(
            root,
            voice_lock.get("translation_gate"),
            "音色锁中的翻译门禁",
            expected_path=translation_gate_path,
        )
        roles = voice_lock.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ProductionJobError("音色锁没有角色映射")
        seen: set[str] = set()
        for row in roles:
            if not isinstance(row, Mapping):
                raise ProductionJobError("音色锁角色结构无效")
            role = str(row.get("role_id") or "").strip()
            voice = str(row.get("voice_id") or "").strip()
            if not role or not voice or role in seen:
                raise ProductionJobError("音色锁角色缺失、重复或未选择音色")
            seen.add(role)
        return voice_lock

    def _validate_authorization(
        self,
        *,
        root: Path,
        run_id: str,
        authorization_path: Path,
        translation_gate_path: Path,
        profile_id: str,
        provider_id: str,
        model: str | None,
        expected_segments_path: Path | None = None,
    ) -> tuple[dict[str, Any], Path, Path, dict[str, Any]]:
        authorization = self._read_object(authorization_path, "全文 TTS 授权")
        if (
            authorization.get("status") != "pass"
            or authorization.get("scope") != "full_tts_current_inputs"
            or authorization.get("capture_mode") != "explicit_generate_full_command"
            or authorization.get("automatic_continue_after_dry_run") is not True
        ):
            raise ProductionJobError("全文 TTS 授权状态或范围无效")
        if (
            str(authorization.get("profile_id") or "") != profile_id
            or str(authorization.get("provider_id") or "") != provider_id
            or str(authorization.get("model") or "") != str(model or "")
        ):
            raise ProductionJobError("全文 TTS 授权没有绑定当前 Provider 与模型")
        segments_path = self._bound_file(
            root,
            authorization.get("segments"),
            "全文 TTS 授权中的语音输入",
            expected_path=expected_segments_path,
        )
        self._bound_file(
            root,
            authorization.get("translation_gate"),
            "全文 TTS 授权中的翻译门禁",
            expected_path=translation_gate_path,
        )
        voice_lock_path = self._bound_file(
            root,
            authorization.get("voice_lock"),
            "全文 TTS 授权中的音色锁",
        )
        voice_lock = self._validate_voice_lock(
            root=root,
            run_id=run_id,
            voice_lock_path=voice_lock_path,
            translation_gate_path=translation_gate_path,
            profile_id=profile_id,
        )
        dry_run = authorization.get("dry_run")
        if not isinstance(dry_run, Mapping) or dry_run.get("status") != "pass":
            raise ProductionJobError("全文 TTS 授权缺少已通过的自动 dry-run")
        if dry_run.get("speed") != 1.0 or dry_run.get("offline_rate") != 1.0:
            raise ProductionJobError("全文 TTS dry-run 没有锁定原生 1.0 速度")
        return authorization, segments_path, voice_lock_path, voice_lock

    def _ad_inputs(
        self,
        *,
        root: Path,
        ad_gate: Mapping[str, Any],
    ) -> tuple[Path, list[dict[str, Any]], float]:
        overlay_plan_path = self._bound_file(
            root, ad_gate.get("overlay_plan"), "广告遮盖计划"
        )
        overlay_plan = self._read_object(overlay_plan_path, "广告遮盖计划")
        if overlay_plan.get("status") != "pass":
            raise ProductionJobError("广告遮盖计划未通过")
        if overlay_plan.get("subtitle_layer_above_overlays") is not True:
            raise ProductionJobError("广告遮盖计划没有保证字幕位于最上层")
        regions = overlay_plan.get("regions")
        if not isinstance(regions, list):
            raise ProductionJobError("广告遮盖计划缺少 regions 数组")
        timeline_path = self._bound_file(
            root, ad_gate.get("timeline_mapping"), "原视频到工作母版时间映射"
        )
        timeline = self._read_object(timeline_path, "原视频到工作母版时间映射")
        timeline_segments = timeline.get("segments")
        try:
            working_duration = float(timeline.get("working_duration_seconds"))
        except (TypeError, ValueError) as exc:
            raise ProductionJobError("原视频到工作母版时间映射缺少有效时长") from exc
        if (
            timeline.get("status") != "pass"
            or timeline.get("mapping") != "source_to_working_master"
            or not isinstance(timeline_segments, list)
            or working_duration <= 0
        ):
            raise ProductionJobError("原视频到工作母版时间映射未通过")
        render_regions: list[dict[str, Any]] = []
        for row in regions:
            if not isinstance(row, Mapping):
                raise ProductionJobError("广告遮盖区域结构无效")
            pixels = row.get("pixels")
            if not isinstance(pixels, Mapping):
                raise ProductionJobError("广告遮盖区域缺少像素坐标")
            try:
                source_start = float(row.get("start"))
                source_end = float(row.get("end"))
            except (TypeError, ValueError) as exc:
                raise ProductionJobError("广告遮盖区域缺少有效源时轴区间") from exc
            overlay_id = str(row.get("candidate_id") or row.get("id") or "overlay")
            part = 0
            for mapping in timeline_segments:
                if not isinstance(mapping, Mapping) or mapping.get("kind") != "keep":
                    continue
                keep_start = float(mapping["source_start"])
                keep_end = float(mapping["source_end"])
                intersection_start = max(source_start, keep_start)
                intersection_end = min(source_end, keep_end)
                if intersection_end <= intersection_start:
                    continue
                part += 1
                working_start = float(mapping["working_start"]) + intersection_start - keep_start
                working_end = float(mapping["working_start"]) + intersection_end - keep_start
                render_regions.append(
                    {
                        "id": overlay_id if part == 1 else f"{overlay_id}-part-{part}",
                        "action": "mask",
                        # build_render_plan's source domain is the already
                        # edited formal working master, not the original media.
                        "source_start": working_start,
                        "source_end": working_end,
                        "x": pixels.get("x"),
                        "y": pixels.get("y"),
                        "width": pixels.get("width"),
                        "height": pixels.get("height"),
                        "color": "black",
                        "opacity": 1.0,
                        "verified": True,
                    }
                )
        removed_rows = ad_gate.get("removed_segments")
        if not isinstance(removed_rows, list):
            raise ProductionJobError("广告门禁缺少 removed_segments 数组")
        for row in removed_rows:
            if not isinstance(row, Mapping):
                raise ProductionJobError("广告删除区间结构无效")
        # The formal working master referenced by ad_edit_gate has already had
        # these source intervals removed. Passing them to the render planner
        # would delete a second, unrelated set of frames in working time.
        return overlay_plan_path, render_regions, working_duration

    def _validate_tts_manifest(
        self,
        *,
        root: Path,
        manifest_path: Path,
        authorization: Mapping[str, Any],
        profile_id: str,
        provider_id: str,
        model: str | None,
    ) -> dict[str, Any]:
        manifest = self._read_object(manifest_path, "TTS manifest")
        locks = manifest.get("locks")
        segments = manifest.get("segments")
        if manifest.get("status") != "ready" or not isinstance(locks, Mapping):
            raise ProductionJobError("TTS manifest 尚未完成或缺少冻结参数")
        if (
            str(locks.get("profile_id") or "") != profile_id
            or str(locks.get("provider_id") or "") != provider_id
            or str(locks.get("model") or "") != str(model or "")
            or str(locks.get("segments_sha256") or "")
            != str(authorization.get("segments_sha256") or "")
            or locks.get("speed") != 1.0
            or locks.get("offline_rate") != 1.0
        ):
            raise ProductionJobError("TTS manifest 与授权、Provider 或原生速度锁不一致")
        if not isinstance(segments, Mapping) or not segments:
            raise ProductionJobError("TTS manifest 没有已验证语音片段")
        if int(manifest.get("ready_count") or -1) != len(segments):
            raise ProductionJobError("TTS manifest 的完成数量不一致")
        manifest_root = manifest_path.parent.resolve()
        for segment_id, row in segments.items():
            if not isinstance(row, Mapping) or row.get("status") != "ready":
                raise ProductionJobError(f"TTS 片段尚未就绪：{segment_id}")
            relative = str(row.get("audio_path") or "")
            if not relative or Path(relative).is_absolute():
                raise ProductionJobError(f"TTS 片段路径无效：{segment_id}")
            audio_path = (manifest_root / relative).resolve()
            if not audio_path.is_relative_to(manifest_root):
                raise ProductionJobError(f"TTS 片段路径越出 manifest 目录：{segment_id}")
            expected_sha = str(row.get("sha256") or "")
            if not audio_path.is_file() or sha256_file(audio_path) != expected_sha:
                raise ProductionJobError(f"TTS 片段缺失或哈希不匹配：{segment_id}")
            if int(row.get("byte_size") or -1) != audio_path.stat().st_size:
                raise ProductionJobError(f"TTS 片段大小不匹配：{segment_id}")
        return manifest

    def _assemble_render_media(
        self,
        *,
        root: Path,
        segments_path: Path,
        manifest_path: Path,
        working_master_path: Path,
        ad_gate_path: Path,
        version: int,
        ffmpeg: str,
        ffprobe: str,
    ) -> dict[str, Any]:
        """Build and verify render inputs from the frozen paid-TTS artifacts."""

        try:
            assembled = build_audio_timeline(
                project_root=root,
                segments_path=segments_path,
                tts_manifest_path=manifest_path,
                working_master_path=working_master_path,
                ad_edit_gate_path=ad_gate_path,
                output_dir=root / f"full_dub_v{version}",
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                version=version,
            )
        except AudioTimelineError as exc:
            raise ProductionJobError(f"中文音频时间线组装失败：{exc}") from exc
        if assembled.get("status") != "pass":
            raise ProductionJobError("中文音频时间线组装没有通过")
        try:
            audio_path = self._inside(root, str(assembled["chinese_audio_path"]))
            subtitle_path = self._inside(root, str(assembled["subtitle_path"]))
            timeline_path = self._inside(root, str(assembled["chinese_timeline_path"]))
        except KeyError as exc:
            raise ProductionJobError("中文音频时间线组装结果缺少输出路径") from exc
        for path, label in (
            (audio_path, "中文 WAV"),
            (subtitle_path, "中文字幕"),
            (timeline_path, "中文时间轴"),
        ):
            if not path.is_file():
                raise ProductionJobError(f"{label}不存在")
        if str(assembled.get("chinese_timeline_sha256") or "") != sha256_file(
            timeline_path
        ):
            raise ProductionJobError("中文时间轴返回哈希不匹配")
        timeline = self._read_object(timeline_path, "中文时间轴")
        if (
            timeline.get("status") != "pass"
            or timeline.get("tts_native_speed") != 1.0
            or timeline.get("offline_rate") != 1.0
            or timeline.get("audio_time_stretch") is not False
            or timeline.get("subtitle_timeline_rebuilt") is not True
        ):
            raise ProductionJobError("中文时间轴没有锁定原生语速与视频重定时策略")
        inputs = timeline.get("inputs")
        outputs = timeline.get("outputs")
        if not isinstance(inputs, Mapping) or not isinstance(outputs, Mapping):
            raise ProductionJobError("中文时间轴缺少输入或输出哈希绑定")
        for key, expected, label in (
            ("segments", segments_path, "中文时间轴中的语音输入"),
            ("tts_manifest", manifest_path, "中文时间轴中的 TTS manifest"),
            ("working_master", working_master_path, "中文时间轴中的正式工作母版"),
            ("ad_edit_gate", ad_gate_path, "中文时间轴中的广告门禁"),
        ):
            self._bound_file(root, inputs.get(key), label, expected_path=expected)
        self._bound_file(
            root,
            outputs.get("chinese_audio"),
            "中文时间轴中的中文 WAV",
            expected_path=audio_path,
        )
        self._bound_file(
            root,
            outputs.get("subtitle_srt"),
            "中文时间轴中的中文字幕",
            expected_path=subtitle_path,
        )
        retime_segments = assembled.get("retime_segments")
        if (
            not isinstance(retime_segments, list)
            or not retime_segments
            or retime_segments != timeline.get("retime_segments")
        ):
            raise ProductionJobError("中文时间轴缺少一致且完整的视频重定时计划")
        try:
            source_duration = float(assembled["source_duration"])
            chinese_audio_duration = float(assembled["chinese_audio_duration"])
            frame_width = int(assembled["frame_width"])
            frame_height = int(assembled["frame_height"])
            frame_rate = assembled["frame_rate"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionJobError("中文音频时间线缺少有效媒体参数") from exc
        if source_duration <= 0 or chinese_audio_duration <= 0:
            raise ProductionJobError("中文音频时间线的媒体时长无效")
        return {
            "chinese_audio": audio_path,
            "subtitles": subtitle_path,
            "timeline_path": timeline_path,
            "timeline": timeline,
            "retime_segments": retime_segments,
            "source_duration": source_duration,
            "chinese_audio_duration": chinese_audio_duration,
            "frame_width": frame_width,
            "frame_height": frame_height,
            "frame_rate": frame_rate,
        }

    def _provider(
        self,
        run: Mapping[str, Any],
        profile_id: str,
        expected_kind: str,
        *,
        task_id: str | None = None,
    ):
        lock = (run.get("provider_lock") or {}).get("profiles", {}).get(profile_id)
        if not isinstance(lock, Mapping):
            raise ProductionJobError("Provider 没有冻结在当前运行中")
        profile = self.database.get_provider_profile(profile_id)
        if profile is None or not profile["enabled"]:
            raise ProductionJobError("Provider 配置已停用或不存在")
        if profile["service_kind"] != expected_kind:
            raise ProductionJobError("Provider 类型与任务不匹配")
        for field in ("provider_id", "service_kind", "model", "base_url"):
            if (profile.get(field) or None) != (lock.get(field) or None):
                raise ProductionJobError("Provider 配置已变化，请创建新的工作流运行")
        if dict(profile.get("config") or {}) != dict(lock.get("config") or {}):
            raise ProductionJobError("Provider 参数已变化，请创建新的工作流运行")
        if dict(profile.get("capability") or {}) != dict(lock.get("capability") or {}):
            raise ProductionJobError("Provider 能力记录已变化，请创建新的工作流运行")
        secret = self.credentials.resolve(profile.get("credential_ref"))
        if not secret:
            raise ProductionJobError("当前设备尚未绑定这个 Provider 的凭证")
        default_urls = {
            "minimax-llm": "https://api.minimax.cn/v1",
            "minimax-speech": "https://api.minimax.cn",
        }
        base_url = profile.get("base_url") or default_urls.get(profile["provider_id"])
        if not base_url:
            raise ProductionJobError("Provider 缺少接口地址")
        provider_profile = ProviderProfile(
            profile_id=profile["id"],
            provider_id=profile["provider_id"],
            kind=profile["service_kind"],
            base_url=base_url,
            model=profile["model"],
            credential_ref=profile.get("credential_ref"),
            # Build from the frozen options after proving the live profile is
            # unchanged.  This avoids a mutable-dict/TOCTOU drift between the
            # comparison above and adapter construction.
            options=dict(lock.get("config") or {}),
            enabled=profile["enabled"],
        )
        try:
            provider = builtin_registry().create(provider_profile, secret)
            return with_provider_ledger(provider, self.database, task_id) if task_id else provider
        finally:
            secret = ""

    def translation_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        run_id = str(payload["run_id"])
        run, project, root = self._context(run_id)
        self._require_workflow_lock(run, project, root)
        slots_path = self._inside(root, str(payload["slots_path"]))
        if not slots_path.is_file():
            raise ProductionJobError("冻结源文文件不存在")
        ad_gate_path = root / "qa" / "ad_edit_gate.json"
        ad_result = validate_gate_file(
            ad_gate_path, kind="ad_edit_gate", artifact_root=root
        )
        if not ad_result.valid:
            raise ProductionJobError("ad_edit_gate 未通过：" + "; ".join(ad_result.errors))
        ad_gate = self._read_object(ad_gate_path, "广告门禁")
        self._bound_file(
            root,
            ad_gate.get("frozen_source_transcript"),
            "广告门禁中的正式源文",
            expected_path=slots_path,
        )
        source = json.loads(slots_path.read_text(encoding="utf-8-sig"))
        slots = source if isinstance(source, list) else source.get("slots") or source.get("items")
        if not isinstance(slots, list):
            raise ProductionJobError("冻结源文缺少 slots/items 数组")
        glossary: dict[str, str] = {}
        if payload.get("glossary_path"):
            glossary_path = self._inside(root, str(payload["glossary_path"]))
            value = json.loads(glossary_path.read_text(encoding="utf-8-sig"))
            glossary = value.get("terms", value) if isinstance(value, dict) else {}
            if not isinstance(glossary, dict):
                raise ProductionJobError("术语表必须是 JSON 对象")

        role_bindings = (run.get("provider_lock") or {}).get("role_bindings") or {}
        if set(role_bindings) != {"T", "A", "B", "C"}:
            raise ProductionJobError("当前运行没有冻结完整的 T/A/B/C Provider")
        child_tasks = {str(k): str(v) for k, v in (payload.get("child_tasks") or {}).items()}
        providers = {
            role: self._provider(
                run,
                str(profile_id),
                "llm",
                task_id=child_tasks.get(role) or str(payload.get("task_id") or "") or None,
            )
            for role, profile_id in role_bindings.items()
        }
        preset = self.database.get_workflow_preset(run["preset_id"])
        if preset is None:
            raise ProductionJobError("工作流预设不存在")
        if (
            str(preset.get("prompt_pack_sha256") or "")
            != str((run.get("prompt_lock") or {}).get("prompt_pack_sha256") or "")
        ):
            raise ProductionJobError("提示词预设已变化，请创建新的工作流运行")
        frozen_prompt_path = self._bound_file(
            root,
            (run.get("prompt_lock") or {}).get("artifact"),
            "冻结提示词包",
        )
        frozen_prompt_pack = self._read_object(frozen_prompt_path, "冻结提示词包")
        role_order = {"T": 1, "A": 2, "B": 3, "C": 4}

        def report(role: str, current: int, total: int, detail: str) -> None:
            token.checkpoint()
            task_id = child_tasks.get(role)
            if task_id:
                task = self.database.get_task(task_id)
                if task and task["status"] == "queued":
                    self.database.update_task(
                        task_id,
                        status="running",
                        started_at=utc_now(),
                        attempt=int(task.get("attempt") or 0) + 1,
                    )
                self.database.update_task(
                    task_id,
                    progress_mode="exact",
                    progress_current=current,
                    progress_total=total,
                    progress_unit="stable_ids",
                    detail=detail,
                )
                if current == total:
                    self.database.update_task(
                        task_id,
                        status="completed",
                        completed_at=utc_now(),
                        detail=detail,
                    )
            progress.checkpointed(
                role_order[role],
                4,
                "roles",
                f"当前角色 {role}：{current}/{total} 个稳定 ID",
            )

        pipeline = TranslationPipeline(
            providers,
            frozen_prompt_pack,
            batch_size=int(payload.get("batch_size") or 40),
        )
        try:
            result = pipeline.run(
                slots,
                glossary=glossary,
                project_context=str(payload.get("project_context") or ""),
                output_dir=root,
                version=int(payload.get("version") or 1),
                progress=report,
            )
        except Exception as exc:
            uncertain = bool(getattr(exc, "uncertain_completion", False))
            for task_id in child_tasks.values():
                child = self.database.get_task(task_id)
                if child and child["status"] == "running":
                    self.database.update_task(
                        task_id,
                        status="blocked_uncertain" if uncertain else "repair_required",
                        error_code=str(getattr(exc, "code", type(exc).__name__)),
                        error={"automatic_retry": False},
                        detail=("外部请求结果不确定" if uncertain else "角色执行失败，已保留之前的验证批次"),
                    )
            raise
        return {
            "detail": "T/A/B/C 翻译、双审和裁决候选已完成",
            "slot_count": result["slot_count"],
            "source_sha256": result["source_sha256"],
            "candidate_sha256": result["candidate_sha256"],
            "final_translation_sha256": result["final_translation_sha256"],
            "translation_path": f"work/translation_final_v{int(payload.get('version') or 1)}.json",
        }

    def speech_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        run_id = str(payload["run_id"])
        run, project, root = self._context(run_id)
        self._require_workflow_lock(run, project, root)
        profile_id = str(payload.get("profile_id") or (run.get("provider_lock") or {}).get("speech_profile_id") or "")
        if not profile_id:
            raise ProductionJobError("当前运行没有冻结语音 Provider")
        provider = self._provider(
            run,
            profile_id,
            "speech",
            task_id=str(payload.get("task_id") or "") or None,
        )
        segments_path = self._inside(root, str(payload["segments_path"]))
        authorization_path = self._inside(root, str(payload["authorization_path"]))
        translation_gate_path = self._inside(root, str(payload["translation_gate_path"]))
        for path, label in (
            (segments_path, "语音输入"),
            (authorization_path, "全文生成授权"),
            (translation_gate_path, "翻译门禁"),
        ):
            if not path.is_file():
                raise ProductionJobError(f"{label}文件不存在")
        gate_result = validate_gate_file(
            translation_gate_path, kind="translation_gate", artifact_root=root
        )
        if not gate_result.valid:
            raise ProductionJobError(
                "translation_gate 尚未通过：" + "; ".join(gate_result.errors)
            )
        authorization, _segments_bound, _voice_lock_path, voice_lock = (
            self._validate_authorization(
                root=root,
                run_id=run_id,
                authorization_path=authorization_path,
                translation_gate_path=translation_gate_path,
                profile_id=profile_id,
                provider_id=provider.profile.provider_id,
                model=provider.profile.model,
                expected_segments_path=segments_path,
            )
        )
        value = json.loads(segments_path.read_text(encoding="utf-8-sig"))
        segments = value if isinstance(value, list) else value.get("segments") or value.get("items")
        if not isinstance(segments, list):
            raise ProductionJobError("语音输入缺少 segments/items 数组")
        locked = {
            str(row["role_id"]): str(row["voice_id"])
            for row in voice_lock["roles"]
        }
        for row in segments:
            if not isinstance(row, Mapping):
                raise ProductionJobError("语音片段结构无效")
            role = str(row.get("role_id") or "").strip()
            if not role or locked.get(role) != str(row.get("voice_id") or "").strip():
                raise ProductionJobError("语音片段角色或音色与当前音色锁不一致")
        output = self._inside(root, str(payload.get("output_dir") or "full_dub_v1/tts"))
        pipeline = SpeechPipeline(provider, output)

        def report(current: int, total: int, _entry: Mapping[str, Any]) -> None:
            token.checkpoint()
            progress.exact(current, total, "segments", f"已验证语音片段 {current}/{total}")

        dry_run = pipeline.dry_run(segments)
        if dry_run != authorization.get("dry_run"):
            raise ProductionJobError("自动 dry-run 与全文授权记录不一致")
        manifest = pipeline.synthesize(segments, authorization=authorization, progress=report)
        return {
            "detail": "全文语音已按原生 1.0 速度生成并验证",
            "manifest_path": str(pipeline.manifest_path.relative_to(root)).replace("\\", "/"),
            "ready_count": manifest["ready_count"],
            "dry_run": dry_run,
        }

    def prepare_audition(self, run_id: str, *, version: int) -> dict[str, Any]:
        """Freeze a bounded, role-covering audition projection and authorization."""

        if version < 1:
            raise ProductionJobError("试听版本号必须大于 0")
        run, project, root = self._context(run_id)
        self._require_workflow_lock(run, project, root)
        profile_id = str(
            (run.get("provider_lock") or {}).get("speech_profile_id") or ""
        )
        if not profile_id:
            raise ProductionJobError("当前运行没有冻结语音 Provider")
        provider = self._provider(run, profile_id, "speech")
        translation_gate_path = root / "qa" / "translation_gate.json"
        voice_lock_path = root / "work" / f"voice_selection_locked_v{version}.json"
        segments_path = root / "work" / f"tts_segments_v{version}.json"
        for path, label in (
            (translation_gate_path, "翻译门禁"),
            (voice_lock_path, "音色锁"),
            (segments_path, "自动生成的完整 TTS 输入"),
        ):
            if not path.is_file():
                raise ProductionJobError(f"{label}文件不存在")
        gate_result = validate_gate_file(
            translation_gate_path, kind="translation_gate", artifact_root=root
        )
        if not gate_result.valid:
            raise ProductionJobError(
                "translation_gate 尚未通过：" + "; ".join(gate_result.errors)
            )
        voice_lock = self._validate_voice_lock(
            root=root,
            run_id=run_id,
            voice_lock_path=voice_lock_path,
            translation_gate_path=translation_gate_path,
            profile_id=profile_id,
        )
        segments_document = self._read_object(segments_path, "完整 TTS 输入")
        if (
            segments_document.get("status") != "pass"
            or segments_document.get("speed") != 1.0
            or segments_document.get("offline_rate") != 1.0
        ):
            raise ProductionJobError("完整 TTS 输入没有锁定原生 speed=1.0")
        self._bound_file(
            root,
            segments_document.get("voice_lock"),
            "完整 TTS 输入中的音色锁",
            expected_path=voice_lock_path,
        )
        translation_gate = self._read_object(translation_gate_path, "翻译门禁")
        translation_path = self._bound_file(
            root,
            translation_gate.get("final_translation"),
            "翻译门禁中的正式翻译",
        )
        self._bound_file(
            root,
            segments_document.get("translation"),
            "完整 TTS 输入中的正式翻译",
            expected_path=translation_path,
        )
        full_segments = segments_document.get("segments")
        if not isinstance(full_segments, list) or not full_segments:
            raise ProductionJobError("完整 TTS 输入缺少非空 segments 数组")
        locked_roles = {
            str(row.get("role_id") or "").strip(): str(
                row.get("voice_id") or ""
            ).strip()
            for row in voice_lock.get("roles") or []
            if isinstance(row, Mapping)
        }
        try:
            plan = plan_audition_segments(full_segments, locked_roles)
        except AuditionError as exc:
            raise ProductionJobError(f"无法生成一分钟试听选择：{exc}") from exc

        profile_lock = (
            (run.get("provider_lock") or {}).get("profiles", {}).get(profile_id)
            or {}
        )
        selection_relative = f"work/audition_segments_v{version}.json"
        selection_path = self._inside(root, selection_relative)
        selection = {
            "schema_version": 1,
            "status": "pass",
            "version": version,
            "scope": AUDITION_SCOPE,
            "selection_policy": "all_roles_then_bounded_source_order",
            "run_id": run_id,
            "provider": {
                "profile_id": profile_id,
                "provider_id": provider.profile.provider_id,
                "model": provider.profile.model,
                "frozen_config": dict(profile_lock.get("config") or {}),
            },
            "source_tts_segments": artifact_ref(segments_path, relative_to=root),
            "voice_lock": artifact_ref(voice_lock_path, relative_to=root),
            "translation_gate": artifact_ref(
                translation_gate_path, relative_to=root
            ),
            "full_tts_authorized": False,
            **plan,
        }
        try:
            write_json_once_or_same(selection_path, selection)
        except AuditionError as exc:
            raise ProductionJobError(str(exc)) from exc

        output_dir_relative = f"auditions/audition_v{version}"
        output_dir = self._inside(root, output_dir_relative)
        pipeline = SpeechPipeline(provider, output_dir / "tts")
        dry_run = pipeline.dry_run(selection["segments"])
        authorization = {
            **authorization_for_segments(
                provider,
                selection["segments"],
                scope=AUDITION_SCOPE,
            ),
            "schema_version": 1,
            "version": version,
            "capture_mode": AUDITION_CAPTURE_MODE,
            "automatic_continue_after_dry_run": True,
            "full_tts_authorized": False,
            "profile_id": profile_id,
            "provider_id": provider.profile.provider_id,
            "model": provider.profile.model,
            "selection": artifact_ref(selection_path, relative_to=root),
            "source_tts_segments": artifact_ref(segments_path, relative_to=root),
            "voice_lock": artifact_ref(voice_lock_path, relative_to=root),
            "translation_gate": artifact_ref(
                translation_gate_path, relative_to=root
            ),
            "dry_run": dry_run,
        }
        authorization_relative = f"qa/audition_authorization_v{version}.json"
        authorization_path = self._inside(root, authorization_relative)
        try:
            write_json_once_or_same(authorization_path, authorization)
        except AuditionError as exc:
            raise ProductionJobError(str(exc)) from exc
        return {
            "status": "pass",
            "version": version,
            "scope": AUDITION_SCOPE,
            "selection_path": selection_relative,
            "authorization_path": authorization_relative,
            "source_segments_path": segments_path.relative_to(root).as_posix(),
            "voice_lock_path": voice_lock_path.relative_to(root).as_posix(),
            "translation_gate_path": translation_gate_path.relative_to(root).as_posix(),
            "output_dir": output_dir_relative,
            "output_path": f"{output_dir_relative}/audition_v{version}.wav",
            "manifest_path": f"{output_dir_relative}/audition_manifest_v{version}.json",
            "tts_output_dir": f"{output_dir_relative}/tts",
            "selected_segment_count": selection["selected_segment_count"],
            "selected_character_units": selection["selected_character_units"],
            "role_count": selection["role_count"],
            "all_roles_covered": selection["all_roles_covered"],
            "target_seconds": selection["target_seconds"],
            "dry_run": dry_run,
            "paid_audition_authorized": True,
            "paid_full_tts_authorized": False,
        }

    def audition_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        """Generate and assemble a frozen one-minute audition without full-TTS scope."""

        run_id = str(payload["run_id"])
        version = int(payload.get("version") or 1)
        run, project, root = self._context(run_id)
        self._require_workflow_lock(run, project, root)
        profile_id = str(
            (run.get("provider_lock") or {}).get("speech_profile_id") or ""
        )
        if not profile_id:
            raise ProductionJobError("当前运行没有冻结语音 Provider")
        provider = self._provider(
            run,
            profile_id,
            "speech",
            task_id=str(payload.get("task_id") or "") or None,
        )

        expected = {
            "selection_path": f"work/audition_segments_v{version}.json",
            "authorization_path": f"qa/audition_authorization_v{version}.json",
            "source_segments_path": f"work/tts_segments_v{version}.json",
            "voice_lock_path": f"work/voice_selection_locked_v{version}.json",
            "translation_gate_path": "qa/translation_gate.json",
            "tts_output_dir": f"auditions/audition_v{version}/tts",
            "output_path": f"auditions/audition_v{version}/audition_v{version}.wav",
            "manifest_path": (
                f"auditions/audition_v{version}/audition_manifest_v{version}.json"
            ),
        }
        for key, value in expected.items():
            if str(payload.get(key) or "") != value:
                raise ProductionJobError(f"试听任务中的 {key} 不是后端冻结路径")

        selection_path = self._inside(root, expected["selection_path"])
        authorization_path = self._inside(root, expected["authorization_path"])
        source_segments_path = self._inside(root, expected["source_segments_path"])
        voice_lock_path = self._inside(root, expected["voice_lock_path"])
        translation_gate_path = self._inside(root, expected["translation_gate_path"])
        for path, label in (
            (selection_path, "试听选择"),
            (authorization_path, "试听授权"),
            (source_segments_path, "完整 TTS 输入"),
            (voice_lock_path, "音色锁"),
            (translation_gate_path, "翻译门禁"),
        ):
            if not path.is_file():
                raise ProductionJobError(f"{label}文件不存在")
        gate_result = validate_gate_file(
            translation_gate_path, kind="translation_gate", artifact_root=root
        )
        if not gate_result.valid:
            raise ProductionJobError(
                "translation_gate 尚未通过：" + "; ".join(gate_result.errors)
            )
        voice_lock = self._validate_voice_lock(
            root=root,
            run_id=run_id,
            voice_lock_path=voice_lock_path,
            translation_gate_path=translation_gate_path,
            profile_id=profile_id,
        )
        authorization = self._read_object(authorization_path, "试听授权")
        if (
            authorization.get("status") != "pass"
            or authorization.get("scope") != AUDITION_SCOPE
            or authorization.get("capture_mode") != AUDITION_CAPTURE_MODE
            or authorization.get("automatic_continue_after_dry_run") is not True
            or authorization.get("full_tts_authorized") is not False
            or str(authorization.get("profile_id") or "") != profile_id
            or str(authorization.get("provider_id") or "")
            != provider.profile.provider_id
            or str(authorization.get("model") or "")
            != str(provider.profile.model or "")
        ):
            raise ProductionJobError("试听授权范围或冻结 Provider 绑定无效")
        for key, path, label in (
            ("selection", selection_path, "试听授权中的试听选择"),
            ("source_tts_segments", source_segments_path, "试听授权中的完整 TTS 输入"),
            ("voice_lock", voice_lock_path, "试听授权中的音色锁"),
            ("translation_gate", translation_gate_path, "试听授权中的翻译门禁"),
        ):
            self._bound_file(
                root,
                authorization.get(key),
                label,
                expected_path=path,
            )
        selection = self._read_object(selection_path, "试听选择")
        if (
            selection.get("status") != "pass"
            or selection.get("scope") != AUDITION_SCOPE
            or selection.get("run_id") != run_id
            or selection.get("full_tts_authorized") is not False
            or selection.get("all_roles_covered") is not True
            or selection.get("speed") != 1.0
            or selection.get("offline_rate") != 1.0
            or float(selection.get("target_seconds") or 0) > 60.0
        ):
            raise ProductionJobError("试听选择没有通过范围、角色或原生速度检查")
        for key, path, label in (
            ("source_tts_segments", source_segments_path, "试听选择中的完整 TTS 输入"),
            ("voice_lock", voice_lock_path, "试听选择中的音色锁"),
            ("translation_gate", translation_gate_path, "试听选择中的翻译门禁"),
        ):
            self._bound_file(root, selection.get(key), label, expected_path=path)
        segments = selection.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ProductionJobError("试听选择缺少非空 segments 数组")
        locked = {
            str(row["role_id"]): str(row["voice_id"])
            for row in voice_lock["roles"]
        }
        selected_roles: set[str] = set()
        for row in segments:
            if not isinstance(row, Mapping):
                raise ProductionJobError("试听语音片段结构无效")
            role = str(row.get("role_id") or "").strip()
            if (
                not role
                or locked.get(role) != str(row.get("voice_id") or "").strip()
                or row.get("speed", 1.0) != 1.0
            ):
                raise ProductionJobError("试听语音片段与当前音色锁不一致")
            selected_roles.add(role)
        if selected_roles != set(locked):
            raise ProductionJobError("试听语音片段没有覆盖全部角色")

        pipeline = SpeechPipeline(
            provider,
            self._inside(root, expected["tts_output_dir"]),
        )
        dry_run = pipeline.dry_run(segments)
        if dry_run != authorization.get("dry_run"):
            raise ProductionJobError("试听 dry-run 与冻结授权记录不一致")
        total = len(segments) + 1

        def report(current: int, _total: int, _entry: Mapping[str, Any]) -> None:
            token.checkpoint()
            progress.exact(
                current,
                total,
                "steps",
                f"已验证试听语音片段 {current}/{len(segments)}",
            )

        manifest = pipeline.synthesize(
            segments,
            authorization=authorization,
            authorization_scope=AUDITION_SCOPE,
            progress=report,
        )
        token.checkpoint()
        try:
            assembled = assemble_audition_audio(
                project_root=root,
                selection_path=selection_path,
                tts_manifest_path=pipeline.manifest_path,
                output_path=expected["output_path"],
                manifest_path=expected["manifest_path"],
                authorization_path=authorization_path,
                voice_lock_path=voice_lock_path,
                translation_gate_path=translation_gate_path,
                ffmpeg=str(payload.get("ffmpeg") or "ffmpeg"),
                ffprobe=str(payload.get("ffprobe") or "ffprobe"),
            )
        except AuditionError as exc:
            raise ProductionJobError(f"试听音频组装失败：{exc}") from exc
        progress.exact(total, total, "steps", "一分钟试听已组装并完成机器检查")
        return {
            "detail": "一分钟角色试听已按原生 1.0 速度生成并通过机器检查",
            "scope": AUDITION_SCOPE,
            "audio_only": True,
            "output_path": expected["output_path"],
            "output_sha256": (assembled.get("output") or {}).get("sha256"),
            "manifest_path": expected["manifest_path"],
            "tts_manifest_path": pipeline.manifest_path.relative_to(root).as_posix(),
            "ready_count": manifest.get("ready_count"),
            "role_count": selection.get("role_count"),
            "duration_seconds": assembled.get("duration_seconds"),
            "dry_run": dry_run,
            "paid_full_tts_authorized": False,
        }

    def prepare_full_speech_authorization(
        self,
        run_id: str,
        *,
        command: str,
        segments_path: str,
        voice_lock_path: str,
        translation_gate_path: str,
        profile_id: str | None,
        version: int,
    ) -> dict[str, Any]:
        normalized_command = "".join(str(command).split())
        if not any(phrase in normalized_command for phrase in ("生成全片", "生成全文")):
            raise ProductionJobError("全文付费 TTS 只接受“生成全片/生成全文”等明确指令")
        run, project, root = self._context(run_id)
        self._require_workflow_lock(run, project, root)
        resolved_profile = str(
            profile_id or (run.get("provider_lock") or {}).get("speech_profile_id") or ""
        )
        if not resolved_profile:
            raise ProductionJobError("当前运行没有冻结语音 Provider")
        provider = self._provider(run, resolved_profile, "speech")
        segments_file = self._inside(root, segments_path)
        voice_lock_file = self._inside(root, voice_lock_path)
        translation_gate_file = self._inside(root, translation_gate_path)
        for path, label in (
            (segments_file, "语音输入"),
            (voice_lock_file, "音色锁"),
            (translation_gate_file, "翻译门禁"),
        ):
            if not path.is_file():
                raise ProductionJobError(f"{label}文件不存在")
        gate = validate_gate_file(
            translation_gate_file, kind="translation_gate", artifact_root=root
        )
        if not gate.valid:
            raise ProductionJobError("翻译门禁未通过：" + "; ".join(gate.errors))
        voice_lock = self._validate_voice_lock(
            root=root,
            run_id=run_id,
            voice_lock_path=voice_lock_file,
            translation_gate_path=translation_gate_file,
            profile_id=resolved_profile,
        )
        value = json.loads(segments_file.read_text(encoding="utf-8-sig"))
        segments = value if isinstance(value, list) else value.get("segments") or value.get("items")
        if not isinstance(segments, list):
            raise ProductionJobError("语音输入缺少 segments/items 数组")
        locked = {
            str(row.get("role_id")): str(row.get("voice_id"))
            for row in voice_lock.get("roles") or []
            if isinstance(row, Mapping)
        }
        for row in segments:
            if not isinstance(row, Mapping):
                raise ProductionJobError("语音片段结构无效")
            role = str(row.get("role_id") or "").strip()
            if not role or locked.get(role) != str(row.get("voice_id") or "").strip():
                raise ProductionJobError(f"语音片段的音色与当前音色锁不一致：{role}")
        pipeline = SpeechPipeline(provider, root / "runtime" / "dry-run-only")
        dry_run = pipeline.dry_run(segments)
        authorization = {
            **authorization_for_segments(provider, segments),
            "schema_version": 1,
            "version": version,
            "capture_mode": "explicit_generate_full_command",
            "command": command,
            "profile_id": resolved_profile,
            "provider_id": provider.profile.provider_id,
            "model": provider.profile.model,
            "voice_lock": {
                "path": voice_lock_file.relative_to(root).as_posix(),
                "sha256": sha256_file(voice_lock_file),
            },
            "translation_gate": {
                "path": translation_gate_file.relative_to(root).as_posix(),
                "sha256": sha256_file(translation_gate_file),
            },
            "segments": {
                "path": segments_file.relative_to(root).as_posix(),
                "sha256": sha256_file(segments_file),
            },
            "dry_run": dry_run,
            "automatic_continue_after_dry_run": True,
        }
        relative = f"qa/full_tts_authorization_v{version}.json"
        atomic_json(self._inside(root, relative), authorization)
        return {"path": relative, "authorization": authorization}

    def render_handler(
        self,
        payload: Mapping[str, Any],
        progress: ProgressReporter,
        token: TaskToken,
    ) -> Mapping[str, Any]:
        run_id = str(payload["run_id"])
        run, project, root = self._context(run_id)
        self._require_workflow_lock(run, project, root)

        def inside(key: str) -> Path:
            return self._inside(root, str(payload[key]))

        working_master = inside("working_master_path")
        output = inside("output_path")
        ad_gate_path = inside("ad_edit_gate_path")
        translation_gate_path = inside("translation_gate_path")
        voice_lock_path = inside("voice_lock_path")
        authorization_path = inside("authorization_path")
        translation_path = inside("translation_path")
        tts_manifest_path = inside("tts_manifest_path")
        required = (
            working_master,
            ad_gate_path,
            translation_gate_path,
            voice_lock_path,
            authorization_path,
            translation_path,
            tts_manifest_path,
        )
        missing = [path.relative_to(root).as_posix() for path in required if not path.is_file()]
        if missing:
            raise ProductionJobError("渲染输入缺失：" + ", ".join(missing))
        if output.exists():
            raise ProductionJobError("目标成片已存在；请使用新的版本号，禁止覆盖旧版本")
        ad_validation = validate_gate_file(ad_gate_path, kind="ad_edit_gate", artifact_root=root)
        translation_validation = validate_gate_file(
            translation_gate_path, kind="translation_gate", artifact_root=root
        )
        if not ad_validation.valid:
            raise ProductionJobError("ad_edit_gate 未通过：" + "; ".join(ad_validation.errors))
        if not translation_validation.valid:
            raise ProductionJobError(
                "translation_gate 未通过：" + "; ".join(translation_validation.errors)
            )
        ad_gate = self._read_object(ad_gate_path, "广告门禁")
        translation_gate = self._read_object(translation_gate_path, "翻译门禁")
        self._bound_file(
            root,
            ad_gate.get("working_master"),
            "广告门禁中的正式工作母版",
            expected_path=working_master,
        )
        overlay_plan_path, gate_overlay_regions, working_duration = self._ad_inputs(
            root=root, ad_gate=ad_gate
        )
        speech_profile_id = str(
            (run.get("provider_lock") or {}).get("speech_profile_id") or ""
        )
        speech_lock = (
            (run.get("provider_lock") or {}).get("profiles", {}).get(speech_profile_id)
            if speech_profile_id
            else None
        )
        if not isinstance(speech_lock, Mapping):
            raise ProductionJobError("当前运行没有完整冻结语音 Provider")
        authorization, segments_path, bound_voice_lock_path, _voice_lock = (
            self._validate_authorization(
                root=root,
                run_id=run_id,
                authorization_path=authorization_path,
                translation_gate_path=translation_gate_path,
                profile_id=speech_profile_id,
                provider_id=str(speech_lock.get("provider_id") or ""),
                model=str(speech_lock.get("model") or ""),
            )
        )
        if bound_voice_lock_path != voice_lock_path.resolve():
            raise ProductionJobError("渲染请求中的音色锁不是全文授权绑定的版本")
        tts_manifest = self._validate_tts_manifest(
            root=root,
            manifest_path=tts_manifest_path,
            authorization=authorization,
            profile_id=speech_profile_id,
            provider_id=str(speech_lock.get("provider_id") or ""),
            model=str(speech_lock.get("model") or ""),
        )
        version = int(payload.get("version") or 1)
        ffmpeg = str(payload.get("ffmpeg") or "ffmpeg")
        ffprobe = str(payload.get("ffprobe") or "ffprobe")
        media = self._assemble_render_media(
            root=root,
            segments_path=segments_path,
            manifest_path=tts_manifest_path,
            working_master_path=working_master,
            ad_gate_path=ad_gate_path,
            version=version,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        if abs(float(media["source_duration"]) - working_duration) > 0.25:
            raise ProductionJobError("工作母版实测时长与广告门禁时间映射不一致")
        chinese_audio = media["chinese_audio"]
        subtitles = media["subtitles"]
        chinese_timeline_path = media["timeline_path"]
        chinese_timeline = media["timeline"]
        progress.checkpointed(
            1, 4, "gates", "已验证门禁并自动组装中文 WAV、字幕与视频重定时计划"
        )
        token.checkpoint()

        plan = build_render_plan(
            working_master=working_master,
            chinese_audio=chinese_audio,
            subtitle_file=subtitles,
            output_file=output,
            source_duration=float(media["source_duration"]),
            frame_width=int(media["frame_width"]),
            frame_height=int(media["frame_height"]),
            frame_rate=media["frame_rate"],
            retime_segments=media["retime_segments"],
            # The evidence gate is authoritative.  API payload copies are not
            # allowed to widen masks or delete additional source intervals.
            overlay_regions=gate_overlay_regions,
            removed_intervals=[],
            chinese_audio_duration=float(media["chinese_audio_duration"]),
            tts_native_speed=1.0,
            offline_rate=1.0,
            ffmpeg=ffmpeg,
            video_codec=str(payload.get("video_codec") or "libx264"),
        )
        plan_path = root / "work" / f"video_retime_plan_v{version}.json"
        atomic_json(plan_path, plan)
        artifacts = {
            "working_master": artifact_ref(working_master, relative_to=root),
            "ad_overlay_plan": artifact_ref(overlay_plan_path, relative_to=root),
            "translation": artifact_ref(translation_path, relative_to=root),
            "role_map": artifact_ref(voice_lock_path, relative_to=root),
            "voice_mapping": artifact_ref(voice_lock_path, relative_to=root),
            "tts_manifest": artifact_ref(tts_manifest_path, relative_to=root),
            "chinese_timeline": artifact_ref(chinese_timeline_path, relative_to=root),
            "video_retime_plan": artifact_ref(plan_path, relative_to=root),
        }
        gate = build_production_gate_report(
            plan,
            artifacts=artifacts,
            ad_edit_gate_status=str(ad_gate.get("status")),
            translation_gate_status=str(translation_gate.get("status")),
            working_master_and_overlay_hashes_match=True,
            translation_hash_matches=(
                (translation_gate.get("final_translation") or {}).get("sha256")
                == sha256_file(translation_path)
            ),
            voice_mapping_locked=True,
            authorization_bound=True,
            automatic_dry_run_recorded=(
                (authorization.get("dry_run") or {}).get("status") == "pass"
            ),
            tts_speeds=[float((tts_manifest.get("locks") or {}).get("speed", 0))],
            offline_rates=[
                float((tts_manifest.get("locks") or {}).get("offline_rate", 0))
            ],
            chinese_overlap_count=int(chinese_timeline.get("overlap_count") or 0),
            subtitle_timeline_rebuilt=(
                chinese_timeline.get("subtitle_timeline_rebuilt") is True
            ),
        )
        production_gate_path = root / "qa" / "production_gate.json"
        atomic_json(root / "qa" / f"production_gate_v{version}.json", gate)
        if gate.get("status") != "pass":
            raise ProductionJobError(
                "production_gate 未通过：" + ", ".join(gate.get("failure_codes") or [])
            )
        atomic_json(production_gate_path, gate)
        progress.checkpointed(2, 4, "gates", "production_gate 已通过，开始正式渲染")
        token.checkpoint()

        output.parent.mkdir(parents=True, exist_ok=True)
        command = list(plan["ffmpeg_command"])
        command[-1:-1] = ["-progress", "pipe:1", "-nostats"]
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        tail: list[str] = []
        try:
            assert process.stdout is not None
            duration = float(plan["expected_output"]["duration"])
            for line in process.stdout:
                value = line.strip()
                if not value:
                    continue
                tail.append(value)
                tail = tail[-40:]
                if value.startswith("out_time_ms="):
                    try:
                        current = min(duration, int(value.split("=", 1)[1]) / 1_000_000)
                        progress.exact(current, duration, "seconds", "正在渲染视频")
                    except ValueError:
                        pass
                token.checkpoint()
            code = process.wait()
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        if code != 0 or not output.is_file():
            raise ProductionJobError(
                f"FFmpeg 渲染失败（exit={code}）：" + " | ".join(tail[-8:])
            )
        progress.checkpointed(3, 4, "gates", "渲染完成，正在执行全片机器 QA")
        token.checkpoint()
        machine_qa = run_machine_qa(
            output,
            plan["expected_output"],
            ffprobe=ffprobe,
            ffmpeg=ffmpeg,
        )
        machine_qa_path = root / "qa" / f"final_machine_qa_v{version}.json"
        atomic_json(machine_qa_path, machine_qa)
        if machine_qa.get("status") != "pass":
            raise ProductionJobError(
                "最终机器 QA 未通过：" + ", ".join(machine_qa.get("failure_codes") or [])
            )
        progress.checkpointed(4, 4, "gates", "全片机器 QA 已通过，等待发布包")
        self.database.execute(
            "UPDATE projects SET status='machine_passed',current_stage='08',updated_at=? WHERE id=?",
            (utc_now(), project["id"]),
        )
        return {
            "detail": "正式成片与机器 QA 已通过；发布包尚待生成",
            "output_path": output.relative_to(root).as_posix(),
            "output_sha256": machine_qa["sha256"],
            "production_gate_path": "qa/production_gate.json",
            "machine_qa_path": machine_qa_path.relative_to(root).as_posix(),
        }
