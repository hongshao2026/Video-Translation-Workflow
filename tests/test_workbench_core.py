from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.workbench.api import create_workbench_router
from backend.workbench.credentials import CredentialBroker, MemoryCredentialStore
from backend.workbench.settings import WorkbenchSettings


class WorkbenchCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        settings = WorkbenchSettings(
            state_dir=root / "state",
            library_dir=root / "library",
            database_path=root / "state" / "workbench.sqlite3",
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            yt_dlp="yt-dlp",
            worker_id="test-worker",
        )
        self.credentials = CredentialBroker(MemoryCredentialStore())
        router, self.context = create_workbench_router(settings, self.credentials)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def create_project(self) -> dict:
        response = self.client.post(
            "/api/library/projects",
            json={
                "source_kind": "video_url",
                "source": "https://www.youtube.com/watch?v=fixture123",
                "title": "Fixture",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_clean_checkout_creates_portable_project_manifest(self) -> None:
        project = self.create_project()
        run_dir = self.context.library.path_for(project)
        manifest = json.loads((run_dir / "project.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["paths_are_relative_to_project"])
        self.assertFalse(manifest["credentials_included"])
        self.assertIn("translation_mode=provider_agent_direct_quality_first", (run_dir / "PROJECT.md").read_text(encoding="utf-8"))
        listed = self.client.get("/api/library/projects").json()
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["projects"][0]["id"], project["id"])

    def test_local_source_can_be_registered_without_copying_media(self) -> None:
        source = Path(self.temp.name) / "source.mp4"
        source.write_bytes(b"not-a-real-video-fixture")
        response = self.client.post(
            "/api/library/projects",
            json={"source_kind": "local_file", "source": str(source)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertFalse(project["metadata"]["source_file"]["copied_into_project"])
        self.assertEqual(list(self.context.library.path_for(project).joinpath("source").iterdir()), [])

    def test_provider_secret_stays_out_of_database_and_responses(self) -> None:
        secret = "fixture-secret-never-store"
        response = self.client.post(
            "/api/providers/profiles",
            json={
                "id": "provider-minimax-test",
                "service_kind": "speech",
                "provider_id": "minimax.speech",
                "display_name": "MiniMax 测试",
                "model": "speech-2.8-hd",
                "api_key": secret,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotIn("credential_ref", body)
        self.assertTrue(body["credential"]["configured"])
        self.assertNotIn(secret, response.text)
        self.assertNotIn(secret.encode(), self.context.settings.database_path.read_bytes())

        listed = self.client.get("/api/providers/profiles").text
        self.assertNotIn(secret, listed)

    def test_external_plain_http_provider_url_is_rejected(self) -> None:
        response = self.client.post(
            "/api/providers/profiles",
            json={
                "service_kind": "llm",
                "provider_id": "compatible",
                "display_name": "Unsafe",
                "base_url": "http://example.com/v1",
                "model": "model",
            },
        )
        self.assertEqual(response.status_code, 422)
        local = self.client.post(
            "/api/providers/profiles",
            json={
                "service_kind": "llm",
                "provider_id": "compatible",
                "display_name": "Local",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "local-model",
            },
        )
        self.assertEqual(local.status_code, 201, local.text)

        embedded = self.client.post(
            "/api/providers/profiles",
            json={
                "service_kind": "llm",
                "provider_id": "openai-compatible",
                "display_name": "Embedded secret",
                "base_url": "https://user:secret@example.test/v1",
                "model": "model",
            },
        )
        self.assertEqual(embedded.status_code, 422)

    def test_provider_catalog_is_explicit_and_config_rejects_secret_fields(self) -> None:
        catalog = self.client.get("/api/providers/catalog")
        self.assertEqual(catalog.status_code, 200)
        pairs = {
            (row["provider_id"], row["service_kind"])
            for row in catalog.json()["providers"]
        }
        self.assertIn(("openai-compatible", "llm"), pairs)
        self.assertIn(("openai-compatible-speech", "speech"), pairs)
        response = self.client.post(
            "/api/providers/profiles",
            json={
                "service_kind": "llm",
                "provider_id": "openai-compatible",
                "display_name": "Unsafe config",
                "base_url": "https://example.test/v1",
                "model": "model",
                "config": {"nested": {"api_key": "must-not-store"}},
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(b"must-not-store", self.context.settings.database_path.read_bytes())

    def test_run_freezes_prompt_and_provider_versions(self) -> None:
        project = self.create_project()
        profile = self.client.post(
            "/api/providers/profiles",
            json={
                "id": "provider-llm-test",
                "service_kind": "llm",
                "provider_id": "openai.compatible",
                "display_name": "文本模型",
                "base_url": "https://api.example.test/v1",
                "model": "fixture-model",
            },
        ).json()
        response = self.client.post(
            f"/api/library/projects/{project['id']}/runs",
            json={
                "role_bindings": {"T": profile["id"], "A": profile["id"], "B": profile["id"], "C": profile["id"]}
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        run = response.json()
        self.assertEqual(len(run["stages"]), 8)
        self.assertEqual(run["provider_lock"]["fallback_policy"], "manual")
        self.assertRegex(run["prompt_lock"]["prompt_pack_sha256"], r"^[0-9a-f]{64}$")
        lock = json.loads(
            (self.context.library.path_for(project) / "qa" / "workflow_lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(lock["status"], "pass")
        self.assertEqual(lock["translation_mode"], "provider_agent_direct_quality_first")
        self.assertEqual(lock["chinese_tts_speed"], 1.0)

    def test_uncertain_paid_task_cannot_be_resumed_or_cancelled(self) -> None:
        project = self.create_project()
        run = self.client.post(f"/api/library/projects/{project['id']}/runs", json={}).json()
        task = self.context.database.create_task(
            run_id=run["id"],
            stage_key="07",
            kind="speech.synthesize",
            detail="已发送语音请求",
        )
        self.context.database.update_task(
            task["id"],
            status="blocked_uncertain",
            detail="计费状态待核对",
        )
        resume = self.client.post(f"/api/jobs/{task['id']}/resume", json={})
        cancel = self.client.post(f"/api/jobs/{task['id']}/cancel", json={})
        self.assertEqual(resume.status_code, 409)
        self.assertEqual(cancel.status_code, 409)
        self.assertIn("计费", resume.text)

    def test_events_are_persistent_and_ordered(self) -> None:
        project = self.create_project()
        events = self.context.database.events_after(0)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[-1]["project_id"], project["id"])
        self.assertEqual(sorted(event["id"] for event in events), [event["id"] for event in events])

    def test_reading_and_explicit_command_build_translation_gate(self) -> None:
        project = self.create_project()
        speech_profile = self.client.post(
            "/api/providers/profiles",
            json={
                "id": "speech-for-gate-test",
                "service_kind": "speech",
                "provider_id": "openai-compatible-speech",
                "display_name": "Fixture speech",
                "base_url": "https://speech.example.test/v1",
                "model": "fixture-voice-model",
                "api_key": "memory-only-fixture-key",
                "config": {"voices": ["voice-a"]},
            },
        ).json()
        run = self.client.post(
            f"/api/library/projects/{project['id']}/runs",
            json={"speech_profile_id": speech_profile["id"]},
        ).json()
        root = self.context.library.path_for(project)
        translation = root / "work" / "translation_final_v1.json"
        translation.write_text(
            json.dumps(
                {
                    "slots": [
                        {
                            "id": "S001",
                            "subtitle_zh": "第一句。",
                            "role_id": "host",
                            "start": 0.0,
                            "end": 1.0,
                        },
                        {
                            "id": "S002",
                            "subtitle_zh": "第二句！",
                            "role_id": "host",
                            "start": 1.0,
                            "end": 2.0,
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        evidence = {
            "translation_agent_t_manifest_v1.json": {
                "stable_ids": ["S001", "S002"],
                "missing_ids": [],
            },
            "translation_audit_agent_a_v1.json": {"missing_ids": [], "issues": []},
            "translation_audit_agent_b_v1.json": {"missing_ids": [], "issues": []},
            "translation_audit_decisions_v1.json": {"decisions": []},
            "translation_regression_v1.json": {"status": "pass"},
        }
        for name, payload in evidence.items():
            (root / "qa" / name).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        parent = self.context.database.create_task(
            run_id=run["id"], stage_key="04", kind="provider.llm.translation", detail="done"
        )
        for role, stage in (("t", "04"), ("a", "05"), ("b", "05"), ("c", "05")):
            child = self.context.database.create_task(
                parent_task_id=parent["id"],
                run_id=run["id"],
                stage_key=stage,
                kind=f"provider.llm.role_{role}",
                detail="done",
            )
            self.context.database.update_task(child["id"], status="completed")
        self.context.database.update_task(parent["id"], status="completed")

        reading = self.client.post(
            f"/api/workflows/runs/{run['id']}/chapter-reading",
            json={"translation_path": "work/translation_final_v1.json", "version": 1},
        )
        self.assertEqual(reading.status_code, 200, reading.text)
        reading_body = reading.json()
        generic = self.client.post(
            f"/api/workflows/runs/{run['id']}/approve-translation",
            json={
                "command": "继续",
                "translation_path": "work/translation_final_v1.json",
                "reading_path": reading_body["reading_path"],
                "validation_path": reading_body["validation_path"],
                "version": 1,
            },
        )
        self.assertEqual(generic.status_code, 409)
        approved = self.client.post(
            f"/api/workflows/runs/{run['id']}/approve-translation",
            json={
                "command": "开始选音",
                "translation_path": "work/translation_final_v1.json",
                "reading_path": reading_body["reading_path"],
                "validation_path": reading_body["validation_path"],
                "version": 1,
            },
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "pass")
        gate = json.loads((root / "qa" / "translation_gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["status"], "pass")
        self.assertTrue(gate["roles_are_distinct"])

        voice = self.client.post(
            f"/api/workflows/runs/{run['id']}/voice-lock",
            json={
                "assignments": {"host": "voice-a"},
                "role_names": {"host": "主持人"},
                "version": 1,
            },
        )
        self.assertEqual(voice.status_code, 200, voice.text)
        self.assertEqual(voice.json()["segments_path"], "work/tts_segments_v1.json")
        segments = root / voice.json()["segments_path"]
        self.assertTrue(segments.is_file())
        segment_payload = json.loads(segments.read_text(encoding="utf-8"))
        self.assertEqual(segment_payload["segment_count"], 2)
        self.assertEqual(segment_payload["segments"][0]["voice_id"], "voice-a")
        with self.assertRaisesRegex(RuntimeError, "生成全片"):
            self.context.production.prepare_full_speech_authorization(
                run["id"],
                command="继续",
                segments_path=voice.json()["segments_path"],
                voice_lock_path=voice.json()["voice_lock_path"],
                translation_gate_path="qa/translation_gate.json",
                profile_id=speech_profile["id"],
                version=1,
            )
        authorization = self.context.production.prepare_full_speech_authorization(
            run["id"],
            command="生成全片",
            segments_path=voice.json()["segments_path"],
            voice_lock_path=voice.json()["voice_lock_path"],
            translation_gate_path="qa/translation_gate.json",
            profile_id=speech_profile["id"],
            version=1,
        )
        self.assertEqual(authorization["authorization"]["status"], "pass")
        self.assertTrue(authorization["authorization"]["automatic_continue_after_dry_run"])
        self.assertEqual(authorization["authorization"]["dry_run"]["speed"], 1.0)

        prepared = self.context.production.prepare_audition(run["id"], version=1)
        self.assertEqual(prepared["scope"], "audition_current_inputs")
        self.assertTrue(prepared["all_roles_covered"])
        self.assertLessEqual(prepared["selected_character_units"], 120)
        self.assertFalse(prepared["paid_full_tts_authorized"])
        selection = json.loads(
            (root / prepared["selection_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(selection["source_tts_segments"]["path"], voice.json()["segments_path"])
        self.assertEqual(selection["speed"], 1.0)
        self.assertFalse(selection["full_tts_authorized"])
        with patch.object(self.context.runner, "enqueue") as enqueue:
            audition = self.client.post(
                f"/api/workflows/runs/{run['id']}/audition",
                json={"version": 1},
            )
        self.assertEqual(audition.status_code, 202, audition.text)
        audition_body = audition.json()
        self.assertEqual(audition_body["kind"], "provider.speech.audition")
        self.assertEqual(audition_body["stage_key"], "06")
        self.assertEqual(
            audition_body["audition"]["output_path"],
            "auditions/audition_v1/audition_v1.wav",
        )
        self.assertFalse(audition_body["audition"]["paid_full_tts_authorized"])
        enqueue.assert_called_once_with(audition_body["id"])


if __name__ == "__main__":
    unittest.main()
