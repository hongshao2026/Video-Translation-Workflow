from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.workbench.gates import sha256_file
from backend.workbench.production_jobs import ProductionJobError, ProductionJobs


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _binding(root: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}


class ProductionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = ProductionJobs(None, None, None)  # type: ignore[arg-type]

    def test_ad_gate_is_authoritative_for_masks_and_removed_intervals(self) -> None:
        overlay = _write(
            self.root / "qa" / "ad_overlay_plan.json",
            {
                "status": "pass",
                "subtitle_layer_above_overlays": True,
                "regions": [
                    {
                        "candidate_id": "AD-1",
                        "start": 2.0,
                        "end": 4.0,
                        "pixels": {"x": 10, "y": 20, "width": 30, "height": 40},
                    }
                ],
            },
        )
        timeline = _write(
            self.root / "qa" / "source_to_edit_timeline.json",
            {
                "status": "pass",
                "mapping": "source_to_working_master",
                "working_duration_seconds": 8.0,
                "segments": [
                    {
                        "kind": "remove",
                        "source_start": 0.0,
                        "source_end": 2.0,
                        "working_anchor": 0.0,
                    },
                    {
                        "kind": "keep",
                        "source_start": 2.0,
                        "source_end": 10.0,
                        "working_start": 0.0,
                        "working_end": 8.0,
                    },
                ],
            },
        )
        gate = {
            "overlay_plan": _binding(self.root, overlay),
            "timeline_mapping": _binding(self.root, timeline),
            "removed_segments": [{"id": "AD-2", "start": 7.0, "end": 9.0}],
        }
        path, masks, working_duration = self.jobs._ad_inputs(root=self.root, ad_gate=gate)
        self.assertEqual(path, overlay)
        self.assertEqual(masks[0]["source_start"], 0.0)
        self.assertEqual(masks[0]["source_end"], 2.0)
        self.assertEqual(masks[0]["x"], 10)
        self.assertTrue(masks[0]["verified"])
        self.assertEqual(working_duration, 8.0)

    def test_authorization_binds_run_profile_gate_voice_lock_and_segments(self) -> None:
        gate = _write(self.root / "qa" / "translation_gate.json", {"status": "pass"})
        segments = _write(self.root / "work" / "segments.json", [{"segment_id": "S1"}])
        voice = _write(
            self.root / "work" / "voice.json",
            {
                "status": "voice_selection_locked_audition_ready",
                "run_id": "run-1",
                "speech_profile_id": "speech-1",
                "translation_gate": _binding(self.root, gate),
                "roles": [{"role_id": "host", "voice_id": "voice-a"}],
            },
        )
        authorization = _write(
            self.root / "qa" / "authorization.json",
            {
                "status": "pass",
                "scope": "full_tts_current_inputs",
                "capture_mode": "explicit_generate_full_command",
                "automatic_continue_after_dry_run": True,
                "profile_id": "speech-1",
                "provider_id": "minimax-speech",
                "model": "speech-model",
                "segments": _binding(self.root, segments),
                "translation_gate": _binding(self.root, gate),
                "voice_lock": _binding(self.root, voice),
                "dry_run": {"status": "pass", "speed": 1.0, "offline_rate": 1.0},
            },
        )
        value, bound_segments, bound_voice, _ = self.jobs._validate_authorization(
            root=self.root,
            run_id="run-1",
            authorization_path=authorization,
            translation_gate_path=gate,
            profile_id="speech-1",
            provider_id="minimax-speech",
            model="speech-model",
            expected_segments_path=segments,
        )
        self.assertEqual(value["status"], "pass")
        self.assertEqual(bound_segments, segments)
        self.assertEqual(bound_voice, voice)
        with self.assertRaisesRegex(ProductionJobError, "Provider"):
            self.jobs._validate_authorization(
                root=self.root,
                run_id="run-1",
                authorization_path=authorization,
                translation_gate_path=gate,
                profile_id="speech-other",
                provider_id="minimax-speech",
                model="speech-model",
            )

    def test_tts_manifest_requires_ready_hash_verified_files_inside_its_directory(self) -> None:
        manifest_path = self.root / "full_dub_v1" / "tts" / "manifest.json"
        audio = manifest_path.parent / "raw" / "S1.mp3"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"fixture-audio")
        manifest = {
            "status": "ready",
            "locks": {
                "profile_id": "speech-1",
                "provider_id": "minimax-speech",
                "model": "speech-model",
                "segments_sha256": "a" * 64,
                "speed": 1.0,
                "offline_rate": 1.0,
            },
            "ready_count": 1,
            "segments": {
                "S1": {
                    "status": "ready",
                    "audio_path": "raw/S1.mp3",
                    "sha256": sha256_file(audio),
                    "byte_size": audio.stat().st_size,
                }
            },
        }
        _write(manifest_path, manifest)
        result = self.jobs._validate_tts_manifest(
            root=self.root,
            manifest_path=manifest_path,
            authorization={"segments_sha256": "a" * 64},
            profile_id="speech-1",
            provider_id="minimax-speech",
            model="speech-model",
        )
        self.assertEqual(result["status"], "ready")

        manifest["segments"]["S1"]["audio_path"] = "../../../outside.mp3"
        _write(manifest_path, manifest)
        with self.assertRaisesRegex(ProductionJobError, "越出"):
            self.jobs._validate_tts_manifest(
                root=self.root,
                manifest_path=manifest_path,
                authorization={"segments_sha256": "a" * 64},
                profile_id="speech-1",
                provider_id="minimax-speech",
                model="speech-model",
            )

    def test_render_media_is_assembled_from_frozen_inputs(self) -> None:
        segments = _write(self.root / "work" / "segments.json", {"segments": []})
        manifest = _write(self.root / "full_dub_v3" / "tts" / "manifest.json", {})
        working_master = self.root / "media" / "working.mp4"
        working_master.parent.mkdir(parents=True)
        working_master.write_bytes(b"video")
        ad_gate = _write(self.root / "qa" / "ad_edit_gate.json", {"status": "pass"})
        audio = self.root / "full_dub_v3" / "chinese_voice_v3.wav"
        subtitle = self.root / "full_dub_v3" / "subtitles_zh_v3.srt"
        audio.write_bytes(b"wav")
        subtitle.write_text("subtitle", encoding="utf-8")
        retime = [
            {
                "kind": "dialogue",
                "source_start": 0.0,
                "source_end": 1.0,
                "target_start": 0.0,
                "target_end": 1.25,
            }
        ]
        timeline = _write(
            self.root / "full_dub_v3" / "chinese_timeline_v3.json",
            {
                "status": "pass",
                "tts_native_speed": 1.0,
                "offline_rate": 1.0,
                "audio_time_stretch": False,
                "subtitle_timeline_rebuilt": True,
                "inputs": {
                    "segments": _binding(self.root, segments),
                    "tts_manifest": _binding(self.root, manifest),
                    "working_master": _binding(self.root, working_master),
                    "ad_edit_gate": _binding(self.root, ad_gate),
                },
                "outputs": {
                    "chinese_audio": _binding(self.root, audio),
                    "subtitle_srt": _binding(self.root, subtitle),
                },
                "retime_segments": retime,
            },
        )
        assembled = {
            "status": "pass",
            "chinese_audio_path": audio.relative_to(self.root).as_posix(),
            "subtitle_path": subtitle.relative_to(self.root).as_posix(),
            "chinese_timeline_path": timeline.relative_to(self.root).as_posix(),
            "chinese_timeline_sha256": sha256_file(timeline),
            "retime_segments": retime,
            "source_duration": 1.0,
            "chinese_audio_duration": 1.25,
            "frame_width": 1920,
            "frame_height": 1080,
            "frame_rate": "30000/1001",
        }
        with patch(
            "backend.workbench.production_jobs.build_audio_timeline",
            return_value=assembled,
        ) as builder:
            result = self.jobs._assemble_render_media(
                root=self.root,
                segments_path=segments,
                manifest_path=manifest,
                working_master_path=working_master,
                ad_gate_path=ad_gate,
                version=3,
                ffmpeg="ffmpeg-test",
                ffprobe="ffprobe-test",
            )
        self.assertEqual(result["chinese_audio"], audio)
        self.assertEqual(result["retime_segments"], retime)
        builder.assert_called_once_with(
            project_root=self.root,
            segments_path=segments,
            tts_manifest_path=manifest,
            working_master_path=working_master,
            ad_edit_gate_path=ad_gate,
            output_dir=self.root / "full_dub_v3",
            ffmpeg="ffmpeg-test",
            ffprobe="ffprobe-test",
            version=3,
        )


if __name__ == "__main__":
    unittest.main()
