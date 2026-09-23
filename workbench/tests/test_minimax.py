from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.minimax_client import (
    MiniMaxClient,
    MiniMaxError,
    SpeechConfig,
    billable_characters,
    clear_runtime_api_key,
    estimate_cost,
)
from backend.minimax_pipeline import synthesize_minimax_pack
from backend.minimax_preview_cache import (
    cached_voice_rows,
    enrich_voices_with_previews,
    is_mandarin_system_voice,
)


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._body


class MiniMaxClientTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_api_key()

    def test_billing_matches_chinese_character_rule(self) -> None:
        self.assertEqual(billable_characters("中文 A!"), 7)
        estimate = estimate_cost("中文 A!", "speech-2.8-hd")
        self.assertEqual(estimate["billable_characters"], 7)
        self.assertEqual(estimate["price_per_10k_cny"], 3.5)

    def test_mandarin_filter_includes_legacy_prefixed_and_special_ids(self) -> None:
        for voice_id in (
            "male-qn-qingse",
            "Chinese (Mandarin)_Reliable_Executive",
            "Arrogant_Miss",
            "Robot_Armor",
        ):
            self.assertTrue(
                is_mandarin_system_voice(
                    {"voice_id": voice_id, "category": "system"}
                )
            )
        self.assertFalse(
            is_mandarin_system_voice(
                {"voice_id": "Cantonese_ProfessionalHost（F)", "category": "system"}
            )
        )
        self.assertFalse(
            is_mandarin_system_voice(
                {"voice_id": "male-qn-qingse", "category": "voice_cloning"}
            )
        )

    def test_local_preview_manifest_enriches_catalog_without_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preview_dir = Path(temp_dir)
            (preview_dir / "voice.mp3").write_bytes(b"ID3demo")
            manifest_path = preview_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "voices": {
                            "male-qn-qingse": {
                                "voice_id": "male-qn-qingse",
                                "voice_name": "青涩青年音色",
                                "description": "青涩青年声音",
                                "category": "system",
                                "catalog_index": 1,
                                "status": "ready",
                                "filename": "voice.mp3",
                                "model": "speech-2.8-hd",
                                "sample_text": "你好",
                                "audio_length_ms": 800,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            rows = cached_voice_rows(manifest_path, preview_dir)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["preview_ready"])
            self.assertEqual(rows[0]["language"], "zh-CN")
            self.assertEqual(
                rows[0]["preview_url"],
                "/media/minimax/catalog/voice.mp3",
            )

            missing = enrich_voices_with_previews(
                [{"voice_id": "Robot_Armor", "category": "system"}],
                manifest_path,
                preview_dir,
            )
            self.assertFalse(missing[0]["preview_ready"])
            self.assertIsNone(missing[0]["preview_url"])

    def test_config_builds_documented_payload(self) -> None:
        config = SpeechConfig.from_mapping(
            {
                "model": "speech-2.6-turbo",
                "emotion": "whisper",
                "speed": 1.15,
                "modifier_timbre": 25,
                "sound_effect": "lofi_telephone",
            }
        )
        payload = config.request_payload("你好", "voice-test")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["output_format"], "hex")
        self.assertEqual(payload["voice_setting"]["voice_id"], "voice-test")
        self.assertEqual(payload["voice_setting"]["emotion"], "whisper")
        self.assertEqual(payload["voice_modify"]["timbre"], 25)

    def test_client_decodes_hex_audio_and_never_puts_key_in_payload(self) -> None:
        session = Mock()
        session.post.return_value = FakeResponse(
            {
                "data": {"audio": b"RIFFdemo".hex(), "status": 2},
                "extra_info": {
                    "audio_format": "wav",
                    "usage_characters": 8,
                },
                "trace_id": "trace-123",
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )
        client = MiniMaxClient("secret-key-value", session=session)
        result = client.synthesize("你好", "voice-test", SpeechConfig(format="wav"))
        self.assertEqual(result.audio, b"RIFFdemo")
        self.assertEqual(result.trace_id, "trace-123")
        call = session.post.call_args
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer secret-key-value")
        self.assertNotIn("secret-key-value", repr(call.kwargs["json"]))

    def test_billable_timeout_is_reported_as_uncertain_and_not_retried(self) -> None:
        session = Mock()
        session.post.side_effect = requests.Timeout()
        client = MiniMaxClient("secret-key-value", session=session)
        with self.assertRaises(MiniMaxError) as raised:
            client.synthesize("你好", "voice-test", SpeechConfig())
        self.assertTrue(raised.exception.uncertain_completion)
        self.assertEqual(session.post.call_count, 1)

    def test_api_catalog_requires_voice_lock_but_not_cost_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_module, "VOICE_SELECTION_WORK_DIR", Path(temp_dir)
        ):
            client = TestClient(app_module.app)
            catalog = client.get("/api/minimax/catalog")
            self.assertEqual(catalog.status_code, 200)
            payload = catalog.json()
            self.assertFalse(payload["configured"])
            self.assertGreaterEqual(len(payload["voices"]), 6)
            self.assertEqual(payload["rate_limits"]["free_rpm"], 10)

            preview = client.post(
                "/api/minimax/preview",
                json={
                    "text": "你好",
                    "voice_id": payload["voices"][0]["voice_id"],
                    "config": payload["defaults"],
                },
            )
            self.assertEqual(preview.status_code, 423)
            self.assertIn("锁定音色", preview.json()["detail"])

    def test_clean_checkout_does_not_claim_an_unavailable_project_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_module, "VOICE_SELECTION_WORK_DIR", Path(temp_dir)
        ), patch.object(app_module, "TRANSLATION_JSON", Path(temp_dir) / "missing-translation.json"), patch.object(
            app_module, "SPEAKER_TRANSLATION_JSON", Path(temp_dir) / "missing-roles.json"
        ), patch.object(app_module, "VOICE_HANDOFF_JSON", Path(temp_dir) / "missing-handoff.json"), patch.object(
            app_module, "BACKGROUND_AUDIO", Path(temp_dir) / "missing-background.flac"
        ), patch.object(app_module, "SOURCE_VIDEO", Path(temp_dir) / "missing-video.mp4"):
            client = TestClient(app_module.app)
            response = client.post(
                "/api/analyze",
                json={
                    "url": f"https://www.youtube.com/watch?v={app_module.PROJECT_ID}",
                    "expected_roles": 3,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertFalse(payload["ready"])
            self.assertEqual([role["id"] for role in payload["roles"]], ["role_1", "role_2", "role_3"])
            self.assertTrue(all("default_minimax_voice" not in role for role in payload["roles"]))
            self.assertFalse(payload["paid_audition_authorized"])
            self.assertIn("尚未完成", payload["notice"])

    def test_voice_selection_fails_closed_without_frozen_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(app_module, "VOICE_SELECTION_WORK_DIR", root / "work"), patch.object(
                app_module, "VOICE_SELECTION_QA_DIR", root / "qa"
            ), patch.object(app_module, "VOICE_HANDOFF_JSON", root / "missing-handoff.json"):
                client = TestClient(app_module.app)
                request = {
                    "url": f"https://www.youtube.com/watch?v={app_module.PROJECT_ID}",
                    "assignments": {},
                    "role_names": {},
                }
                response = client.post("/api/voice-selection", json=request)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("选音交接", response.text)
                self.assertFalse((root / "work").exists())
                self.assertFalse((root / "qa").exists())

    def test_preview_endpoint_with_mock_transport_writes_playable_result(self) -> None:
        fake_result = Mock(
            audio=b"ID3demo",
            audio_format="mp3",
            trace_id="trace-preview",
            extra_info={"usage_characters": 12, "audio_length": 900},
        )
        fake_client = Mock()
        fake_client.synthesize.return_value = fake_result
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_module, "MINIMAX_PREVIEW_DIR", Path(temp_dir)
        ), patch.object(
            app_module, "resolve_api_key", return_value="hidden-test-key"
        ), patch.object(
            app_module, "MiniMaxClient", return_value=fake_client
        ), patch.object(
            app_module, "paid_audition_authorized", return_value=True
        ):
            client = TestClient(app_module.app)
            response = client.post(
                "/api/minimax/preview",
                json={
                    "text": "你好",
                    "voice_id": "voice-test",
                    "config": {"format": "mp3"},
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["usage_characters"], 12)
            self.assertEqual(payload["trace_id"], "trace-preview")
            files = list(Path(temp_dir).glob("*.mp3"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"ID3demo")

    def test_batch_pipeline_stages_sentence_pack_without_real_api(self) -> None:
        fake_client = Mock()
        fake_client.synthesize.return_value = SimpleNamespace(
            audio=b"ID3demo",
            audio_format="mp3",
            trace_id="trace-batch",
            extra_info={"usage_characters": 8},
        )
        manifest = {
            "clip_start_seconds": 0,
            "clip_end_seconds": 2,
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "role_id": "host",
                    "role_name": "主持人",
                    "text": "你好",
                },
                {
                    "segment_id": "segment_0002",
                    "role_id": "guest",
                    "role_name": "嘉宾",
                    "text": "欢迎",
                },
            ],
        }

        def fake_normalize(_source: Path, destination: Path, _cwd: Path) -> float:
            destination.write_bytes(b"RIFFnormalized")
            return 0.25

        progress_rows: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "backend.minimax_pipeline.normalize_audio", side_effect=fake_normalize
        ), patch("backend.minimax_pipeline.time.sleep") as mocked_sleep:
            report = synthesize_minimax_pack(
                client=fake_client,
                manifest=manifest,
                assignments={"host": "voice-a", "guest": "voice-b"},
                config=SpeechConfig(),
                pack_dir=Path(temp_dir),
                project_root=Path(temp_dir),
                requests_per_minute=10,
                progress=lambda current, total, _row: progress_rows.append((current, total)),
            )
            self.assertTrue(Path(report["pack_json"]).exists())
            self.assertEqual(fake_client.synthesize.call_count, 2)
            self.assertEqual(progress_rows, [(1, 2), (2, 2)])
            self.assertEqual(mocked_sleep.call_count, 1)
            self.assertTrue((Path(temp_dir) / "normalized" / "segment_0002.wav").exists())


if __name__ == "__main__":
    unittest.main()
