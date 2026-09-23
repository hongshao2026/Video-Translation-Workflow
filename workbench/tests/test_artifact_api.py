from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.workbench import publication
from backend.workbench.api import create_workbench_router
from backend.workbench.credentials import CredentialBroker, MemoryCredentialStore
from backend.workbench.settings import WorkbenchSettings


def _json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtifactApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        settings = WorkbenchSettings(
            state_dir=base / "state",
            library_dir=base / "library",
            database_path=base / "state" / "workbench.sqlite3",
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            yt_dlp="yt-dlp",
            worker_id="artifact-api-test",
        )
        router, self.context = create_workbench_router(
            settings, CredentialBroker(MemoryCredentialStore())
        )
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        project_response = self.client.post(
            "/api/library/projects",
            json={
                "source_kind": "video_url",
                "source": "https://www.youtube.com/watch?v=artifact-fixture",
                "title": "Artifact API fixture",
            },
        )
        self.assertEqual(project_response.status_code, 201, project_response.text)
        self.project = project_response.json()
        run_response = self.client.post(
            f"/api/library/projects/{self.project['id']}/runs", json={}
        )
        self.assertEqual(run_response.status_code, 201, run_response.text)
        self.run = run_response.json()
        self.root = self.context.library.path_for(self.project)

    def _write_masters(self) -> None:
        (self.root / "source" / "original.mp4").write_bytes(b"source-master")
        (self.root / "work" / "working_v1.mp4").write_bytes(b"working-master")

    def test_provider_catalog_uses_current_china_defaults(self) -> None:
        response = self.client.get("/api/providers/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        providers = {row["provider_id"]: row for row in response.json()["providers"]}
        self.assertEqual(
            providers["minimax-llm"]["default_base_url"],
            "https://api.minimax.cn/v1",
        )
        self.assertEqual(
            providers["minimax-speech"]["default_base_url"],
            "https://api.minimax.cn",
        )

    def test_profile_catalog_returns_configured_voices_without_generation(self) -> None:
        saved = self.client.post(
            "/api/providers/profiles",
            json={
                "id": "speech-fixture",
                "service_kind": "speech",
                "provider_id": "openai-compatible-speech",
                "display_name": "Fixture speech",
                "base_url": "http://127.0.0.1:9999/v1",
                "model": "fixture-tts",
                "api_key": "fixture-secret-value",
                "config": {
                    "model_listing": False,
                    "voices": [
                        {
                            "voice_id": "voice-a",
                            "name": "Voice A",
                            "language": "zh-CN",
                        }
                    ],
                },
            },
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        response = self.client.get(
            "/api/providers/profiles/speech-fixture/catalog"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["generation_performed"])
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["voices"][0]["voice_id"], "voice-a")

    def _pass_ad_gate(self) -> dict:
        self._write_masters()
        response = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/ad-edit-gate",
            json={
                "source_master_path": "source/original.mp4",
                "working_master_path": "work/working_v1.mp4",
                "source_duration": 30,
                "frame_width": 1920,
                "frame_height": 1080,
                "analysis": {
                    "status": "pass",
                    "content_scan_complete": True,
                    "visual_scan_complete": True,
                    "semantic_analysis_required": False,
                    "semantic_analysis_complete": False,
                    "analysis_method": "deterministic_full_scan",
                    "decision": "no_ads_detected",
                    "candidates": [],
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_transcript_import_freezes_ids_and_rejects_paths_outside_project(self) -> None:
        subtitle = self.root / "work" / "source.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\nWorld\n",
            encoding="utf-8",
        )
        response = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/ingest/source-transcript",
            json={"source_path": "work/source.srt", "language": "en", "version": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["stable_ids"], ["S000001", "S000002"])
        frozen = self.root / payload["artifact"]["path"]
        self.assertEqual(_sha(frozen), payload["artifact"]["sha256"])

        outside = Path(self.temp.name) / "outside.srt"
        outside.write_text("1\n00:00:00,000 --> 00:00:01,000\nNo\n", encoding="utf-8")
        escaped = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/ingest/source-transcript",
            json={"source_path": str(outside), "version": 2},
        )
        self.assertEqual(escaped.status_code, 409)
        self.assertFalse((self.root / "work" / "frozen_source_v2.json").exists())

    def test_embedded_subtitle_extraction_is_only_a_safe_shell_free_plan(self) -> None:
        self._write_masters()
        response = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/ingest/embedded-subtitle-plan",
            json={
                "source_path": "source/original.mp4",
                "output_path": "work/embedded_en.vtt",
                "stream_index": 3,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()["plan"]
        self.assertFalse(plan["shell"])
        self.assertIn("-n", plan["argv"])
        self.assertNotIn("-y", plan["argv"])
        self.assertFalse((self.root / "work" / "embedded_en.vtt").exists())

    def test_missing_semantic_ad_evidence_is_explicit_and_creates_no_gate(self) -> None:
        self._write_masters()
        response = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/ad-edit-gate",
            json={
                "source_master_path": "source/original.mp4",
                "working_master_path": "work/working_v1.mp4",
                "source_duration": 30,
                "analysis": {
                    "status": "analysis_required",
                    "content_scan_complete": True,
                    "visual_scan_complete": True,
                    "semantic_analysis_required": True,
                    "semantic_analysis_complete": False,
                    "candidates": [],
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "analysis_required")
        self.assertIn("semantic_analysis_required", response.json()["blockers"])
        self.assertFalse((self.root / "qa" / "ad_edit_gate.json").exists())
        project = self.context.database.get_project(self.project["id"])
        self.assertEqual(project["status"], "waiting_user")

    def test_completed_ad_evidence_writes_and_hash_validates_gate(self) -> None:
        result = self._pass_ad_gate()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["decision"], "no_ads_detected")
        gate = self.root / result["gate"]["path"]
        self.assertEqual(_sha(gate), result["gate"]["sha256"])
        project = self.context.database.get_project(self.project["id"])
        self.assertEqual(project["current_stage"], "03")
        self.assertFalse(project["needs_attention"])

    def _prepare_publication_inputs(self) -> dict:
        self._pass_ad_gate()
        final_video = self.root / "deliverables" / "final_v1.mp4"
        final_video.write_bytes(b"final-video-fixture")
        machine_qa = self.root / "qa" / "final_machine_qa_v1.json"
        _json(
            machine_qa,
            {
                "schema_version": 1,
                "status": "pass",
                "sha256": _sha(final_video),
                "observed": {"duration": 30.0},
            },
        )
        retime_plan = self.root / "work" / "video_retime_plan_v1.json"
        _json(
            retime_plan,
            {
                "schema_version": 1,
                "status": "ready",
                "video_retime_segments": [
                    {
                        "source_start": 0,
                        "source_end": 30,
                        "target_start": 0,
                        "target_end": 30,
                    }
                ],
            },
        )
        production_gate = self.root / "qa" / "production_gate.json"
        _json(
            production_gate,
            {
                "schema_version": 1,
                "status": "pass",
                "tts_native_speed": 1.0,
                "offline_rate": 1.0,
                "audio_time_stretch": False,
                "forbidden_audio_processors": [],
                "sync_strategy": "video_retime_only",
                "working_master_frames_preserved": True,
                "video_retime_plan": {
                    "path": "work/video_retime_plan_v1.json",
                    "sha256": _sha(retime_plan),
                },
                "coverage_complete": True,
            },
        )
        frame = self.root / "source" / "cover_frame.png"
        if publication.Image is None:
            self.skipTest("Pillow is not installed")
        image = publication.Image.new("RGB", (640, 360), "#183B31")
        image.save(frame, format="PNG")
        self.context.database.update_project(
            self.project["id"],
            {"status": "machine_passed", "current_stage": "08", "needs_attention": False},
        )
        return {
            "video_id": "artifact_fixture",
            "version": 1,
            "final_video_path": "deliverables/final_v1.mp4",
            "final_machine_qa_path": "qa/final_machine_qa_v1.json",
            "production_gate_path": "qa/production_gate.json",
            "ad_edit_gate_path": "qa/ad_edit_gate.json",
            "source_chapters": [
                {"id": "C01", "title": "开场", "source_seconds": 0},
                {"id": "C02", "title": "核心讨论", "source_seconds": 10},
                {"id": "C03", "title": "总结", "source_seconds": 20},
            ],
            "source_to_working_path": "qa/source_to_edit_timeline.json",
            "working_to_final_path": "work/video_retime_plan_v1.json",
            "titles": ["如何改善决策", "一次深度决策对话", "从理论到实践的决策课"],
            "description": (
                "本期讨论决策、实践与经验。\n"
                "原视频：https://www.youtube.com/watch?v=artifact-fixture"
            ),
            "books": ["思考，快与慢"],
            "original_video_url": "https://www.youtube.com/watch?v=artifact-fixture",
            "foreign_names_verified": True,
            "removed_promotion_categories": ["subscription", "discount"],
            "cover_source_path": "source/cover_frame.png",
            "cover_title": "如何做出更好的决策",
            "cover_source_authorized": True,
            "cover_source_clean_verified": True,
            "cover_identity_verified": True,
            "cover_text_verified": True,
        }

    def test_publication_package_returns_utf8_copy_and_stays_waiting_user(self) -> None:
        request = self._prepare_publication_inputs()
        response = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/publication-package",
            json=request,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["delivery_state"], "ready_for_human_review")
        self.assertEqual(payload["encoding"], "utf-8")
        self.assertIn("中文标题候选", payload["copyable_text"])
        self.assertIn(request["original_video_url"], payload["copyable_text"])
        text_ref = payload["publication_text"]
        text_path = self.root / text_ref["path"]
        self.assertEqual(text_path.read_text(encoding="utf-8"), payload["copyable_text"])
        self.assertEqual(_sha(text_path), text_ref["sha256"])
        project = self.context.database.get_project(self.project["id"])
        refreshed_run = self.context.database.get_run(self.run["id"])
        self.assertEqual(project["status"], "waiting_user")
        self.assertEqual(refreshed_run["status"], "waiting_user")
        self.assertTrue(project["needs_attention"])

    def test_publication_rejects_tampered_machine_qa_before_writing_outputs(self) -> None:
        request = self._prepare_publication_inputs()
        (self.root / "deliverables" / "final_v1.mp4").write_bytes(b"tampered")
        response = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/publication-package",
            json=request,
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("哈希不匹配", response.text)
        self.assertFalse((self.root / "qa" / "chapter_timeline_v1.json").exists())
        self.assertFalse((self.root / "deliverables" / "artifact_fixture_发布材料_v1.txt").exists())

    def test_publication_rejects_unbound_timeline_and_substituted_source_url(self) -> None:
        request = self._prepare_publication_inputs()
        alternate = self.root / "qa" / "alternate_source_map.json"
        _json(
            alternate,
            {
                "segments": [
                    {
                        "source_start": 0,
                        "source_end": 30,
                        "working_start": 0,
                        "working_end": 30,
                    }
                ]
            },
        )
        request["source_to_working_path"] = "qa/alternate_source_map.json"
        unbound = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/publication-package",
            json=request,
        )
        self.assertEqual(unbound.status_code, 409)
        self.assertIn("不是同一文件", unbound.text)

        request["source_to_working_path"] = "qa/source_to_edit_timeline.json"
        request["original_video_url"] = "https://example.com/substituted"
        request["description"] = "原视频：https://example.com/substituted"
        substituted = self.client.post(
            f"/api/workflows/runs/{self.run['id']}/publication-package",
            json=request,
        )
        self.assertEqual(substituted.status_code, 409)
        self.assertIn("当前项目的原视频链接", substituted.text)
        self.assertFalse((self.root / "qa" / "chapter_timeline_v1.json").exists())


if __name__ == "__main__":
    unittest.main()
