from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from backend.workbench.audition import (
    AUDITION_SCOPE,
    AuditionError,
    assemble_audition_audio,
    plan_audition_segments,
)
from backend.workbench.gates import sha256_file


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _write_wav(path: Path, *, frames: int = 1600, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * frames)
    return path


def _binding(root: Path, path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "byte_size": path.stat().st_size,
    }


class AuditionPlanningTests(unittest.TestCase):
    def test_selection_covers_every_role_without_selecting_full_long_input(self) -> None:
        roles = {"host": "voice-a", "guest": "voice-b", "narrator": "voice-c"}
        rows = []
        for index in range(45):
            role = tuple(roles)[index % len(roles)]
            rows.append(
                {
                    "segment_id": f"S{index:03d}",
                    "role_id": role,
                    "voice_id": roles[role],
                    "text": f"这是第{index}条用于验证有界试听选择的中文句子。",
                    "speed": 1.0,
                }
            )
        result = plan_audition_segments(rows, roles)
        self.assertTrue(result["all_roles_covered"])
        self.assertEqual(result["selected_role_ids"], sorted(roles))
        self.assertLess(result["selected_segment_count"], len(rows))
        self.assertLessEqual(result["selected_character_units"], 120)
        self.assertLessEqual(result["estimated_duration_seconds"], 60)
        self.assertTrue(all(row["speed"] == 1.0 for row in result["segments"]))
        self.assertEqual(
            sum(bool(row["required_role_sample"]) for row in result["segments"]),
            len(roles),
        )

    def test_selection_rejects_voice_drift_and_non_native_speed(self) -> None:
        roles = {"host": "voice-a"}
        with self.assertRaisesRegex(AuditionError, "音色锁"):
            plan_audition_segments(
                [
                    {
                        "segment_id": "S1",
                        "role_id": "host",
                        "voice_id": "voice-other",
                        "text": "你好。",
                    }
                ],
                roles,
            )
        with self.assertRaisesRegex(AuditionError, "speed=1.0"):
            plan_audition_segments(
                [
                    {
                        "segment_id": "S1",
                        "role_id": "host",
                        "voice_id": "voice-a",
                        "text": "你好。",
                        "speed": 1.1,
                    }
                ],
                roles,
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required for audition assembly",
)
class AuditionAssemblyTests(unittest.TestCase):
    def test_ffmpeg_assembly_is_playable_bound_and_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selection = _write_json(
                root / "work" / "audition_segments_v1.json",
                {
                    "status": "pass",
                    "scope": AUDITION_SCOPE,
                    "target_seconds": 60.0,
                    "speed": 1.0,
                    "role_count": 2,
                    "all_roles_covered": True,
                    "segments": [
                        {
                            "segment_id": "AUD001",
                            "role_id": "a",
                            "required_role_sample": True,
                        },
                        {
                            "segment_id": "AUD002",
                            "role_id": "b",
                            "required_role_sample": True,
                        },
                    ],
                },
            )
            authorization = _write_json(root / "qa" / "audition_authorization_v1.json", {})
            voice_lock = _write_json(root / "work" / "voice_selection_locked_v1.json", {})
            gate = _write_json(root / "qa" / "translation_gate.json", {})
            tts_root = root / "auditions" / "audition_v1" / "tts"
            first = _write_wav(tts_root / "raw" / "AUD001.wav")
            second = _write_wav(tts_root / "raw" / "AUD002.wav")
            tts_manifest = _write_json(
                tts_root / "manifest.json",
                {
                    "status": "ready",
                    "locks": {
                        "authorization_scope": AUDITION_SCOPE,
                        "speed": 1.0,
                        "offline_rate": 1.0,
                    },
                    "segments": {
                        "AUD001": {
                            "status": "ready",
                            "audio_path": "raw/AUD001.wav",
                            "sha256": sha256_file(first),
                            "byte_size": first.stat().st_size,
                        },
                        "AUD002": {
                            "status": "ready",
                            "audio_path": "raw/AUD002.wav",
                            "sha256": sha256_file(second),
                            "byte_size": second.stat().st_size,
                        },
                    },
                },
            )
            kwargs = {
                "project_root": root,
                "selection_path": selection,
                "tts_manifest_path": tts_manifest,
                "output_path": "auditions/audition_v1/audition_v1.wav",
                "manifest_path": "auditions/audition_v1/audition_manifest_v1.json",
                "authorization_path": authorization,
                "voice_lock_path": voice_lock,
                "translation_gate_path": gate,
                "ffmpeg": str(shutil.which("ffmpeg")),
                "ffprobe": str(shutil.which("ffprobe")),
            }
            result = assemble_audition_audio(**kwargs)
            self.assertEqual(result["status"], "pass")
            self.assertTrue(result["audio_only"])
            self.assertFalse(result["audio_time_stretch"])
            output = root / str(result["output"]["path"])
            self.assertTrue(output.is_file())
            before = output.stat().st_mtime_ns
            cached = assemble_audition_audio(**kwargs)
            self.assertEqual(cached, result)
            self.assertEqual(output.stat().st_mtime_ns, before)

    def test_manifest_cannot_escape_tts_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selection = _write_json(
                root / "work" / "audition_segments_v1.json",
                {
                    "status": "pass",
                    "scope": AUDITION_SCOPE,
                    "target_seconds": 60.0,
                    "speed": 1.0,
                    "role_count": 1,
                    "all_roles_covered": True,
                    "segments": [
                        {
                            "segment_id": "AUD001",
                            "role_id": "a",
                            "required_role_sample": True,
                        }
                    ],
                },
            )
            authorization = _write_json(root / "qa" / "authorization.json", {})
            voice_lock = _write_json(root / "work" / "voice.json", {})
            gate = _write_json(root / "qa" / "gate.json", {})
            outside = _write_wav(root / "outside.wav")
            manifest = _write_json(
                root / "auditions" / "audition_v1" / "tts" / "manifest.json",
                {
                    "status": "ready",
                    "locks": {
                        "authorization_scope": AUDITION_SCOPE,
                        "speed": 1.0,
                        "offline_rate": 1.0,
                    },
                    "segments": {
                        "AUD001": {
                            "status": "ready",
                            "audio_path": "../../../outside.wav",
                            "sha256": sha256_file(outside),
                            "byte_size": outside.stat().st_size,
                        }
                    },
                },
            )
            with self.assertRaisesRegex(AuditionError, "越界"):
                assemble_audition_audio(
                    project_root=root,
                    selection_path=selection,
                    tts_manifest_path=manifest,
                    output_path="auditions/audition_v1/audition_v1.wav",
                    manifest_path="auditions/audition_v1/audition_manifest_v1.json",
                    authorization_path=authorization,
                    voice_lock_path=voice_lock,
                    translation_gate_path=gate,
                    ffmpeg=str(shutil.which("ffmpeg")),
                    ffprobe=str(shutil.which("ffprobe")),
                )


if __name__ == "__main__":
    unittest.main()
