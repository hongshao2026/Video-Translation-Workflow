from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from backend.workbench.audio_timeline import (
    AudioTimelineError,
    build_audio_timeline,
    build_tts_segments,
    plan_audio_timeline,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pcm(path: Path, seconds: float, *, sample_rate: int = 48_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x00\x00" * round(seconds * sample_rate))


class TtsSegmentBuilderTests(unittest.TestCase):
    def test_builds_paid_inputs_from_approved_subtitles_and_locked_voices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            translation = root / "work" / "translation_final_v1.json"
            _write_json(
                translation,
                {
                    "slots": [
                        {
                            "id": "S0001",
                            "start": 0.5,
                            "end": 1.5,
                            "speaker": "host",
                            "subtitle_zh": "欢迎来到节目。",
                            "recording_zh": "欢迎来到节目。",
                        },
                        {
                            "id": "S0002",
                            "start": 2.0,
                            "end": 3.0,
                            "speaker": "guest",
                            "subtitle_zh": "这是 GPT-5。",
                            "recording_zh": "这是 G P T five。",
                        },
                    ]
                },
            )
            result = build_tts_segments(
                project_root=root,
                translation_path="work/translation_final_v1.json",
                output_path="work/tts_segments_v1.json",
                version=1,
                assignments={"host": "voice-a", "guest": "voice-b"},
            )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["time_basis"], "working_master")
            self.assertEqual(result["segments"][1]["subtitle_zh"], "这是 GPT-5。")
            self.assertEqual(result["segments"][1]["text"], "这是 G P T five。")
            self.assertEqual(result["segments"][1]["voice_id"], "voice-b")
            self.assertEqual(result["segments"][1]["speed"], 1.0)
            self.assertEqual(result["path"], "work/tts_segments_v1.json")
            self.assertEqual(result["sha256"], _sha(root / result["path"]))

            # An identical retry is cache-safe; a different frozen assignment
            # cannot silently overwrite the same version.
            same = build_tts_segments(
                project_root=root,
                translation_path=translation,
                output_path="work/tts_segments_v1.json",
                version=1,
                assignments={"host": "voice-a", "guest": "voice-b"},
            )
            self.assertEqual(same["sha256"], result["sha256"])
            with self.assertRaisesRegex(AudioTimelineError, "已存在"):
                build_tts_segments(
                    project_root=root,
                    translation_path=translation,
                    output_path="work/tts_segments_v1.json",
                    version=1,
                    assignments={"host": "voice-a", "guest": "voice-c"},
                )

    def test_missing_role_assignment_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            translation = root / "translation.json"
            _write_json(
                translation,
                {
                    "slots": [
                        {
                            "id": "S1",
                            "start": 0,
                            "end": 1,
                            "speaker": "guest",
                            "subtitle_zh": "你好。",
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(AudioTimelineError, "没有锁定音色"):
                build_tts_segments(
                    project_root=root,
                    translation_path=translation,
                    output_path="tts_segments_v1.json",
                    version=1,
                    assignments={"host": "voice-a"},
                )


class PureTimelinePlanTests(unittest.TestCase):
    def test_preserves_dialogue_free_gaps_and_covers_every_working_frame(self) -> None:
        plan = plan_audio_timeline(
            [
                {
                    "segment_id": "S1",
                    "working_start": 1.0,
                    "working_end": 2.0,
                    "subtitle_zh": "第一句。",
                },
                {
                    "segment_id": "S2",
                    "working_start": 3.0,
                    "working_end": 3.5,
                    "subtitle_zh": "第二句。",
                },
            ],
            {"S1": 24_000, "S2": 48_000},
            working_duration=4.0,
            sample_rate=48_000,
        )
        # Original silent gaps: 1.0 + 1.0 + 0.5 seconds.  Native TTS:
        # 0.5 + 1.0 seconds.  No voiced content was squeezed into old slots.
        self.assertAlmostEqual(plan["target_duration"], 4.0, places=6)
        self.assertEqual(plan["segments"][0]["target_start"], 1.0)
        self.assertEqual(plan["segments"][0]["target_end"], 1.5)
        self.assertEqual(plan["segments"][1]["target_start"], 2.5)
        self.assertEqual(plan["segments"][1]["target_end"], 3.5)
        self.assertEqual(plan["retime_segments"][0]["source_start"], 0.0)
        self.assertEqual(plan["retime_segments"][-1]["source_end"], 4.0)
        self.assertTrue(plan["source_coverage_complete"])
        self.assertEqual(plan["overlap_count"], 0)
        for previous, current in zip(
            plan["retime_segments"], plan["retime_segments"][1:]
        ):
            self.assertEqual(previous["source_end"], current["source_start"])
            self.assertEqual(previous["target_end"], current["target_start"])

    def test_rejects_overlapping_frozen_segments(self) -> None:
        with self.assertRaisesRegex(AudioTimelineError, "重叠"):
            plan_audio_timeline(
                [
                    {"segment_id": "S1", "working_start": 0, "working_end": 2},
                    {"segment_id": "S2", "working_start": 1, "working_end": 3},
                ],
                {"S1": 48_000, "S2": 48_000},
                working_duration=4,
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe are required for the media smoke test",
)
class AudioTimelineMediaSmokeTests(unittest.TestCase):
    def test_converts_pcm_maps_deleted_source_time_and_resumes_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            working = root / "source" / "working_v1.mp4"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x240:r=25:d=4",
                    "-an",
                    "-c:v",
                    "mpeg4",
                    str(working),
                ],
                check=True,
                capture_output=True,
            )

            timeline_mapping = root / "qa" / "source_to_edit_timeline.json"
            mapping = {
                "schema_version": 1,
                "status": "pass",
                "mapping": "source_to_working_master",
                "source_duration_seconds": 5.0,
                "working_duration_seconds": 4.0,
                "identity": False,
                "continuous": True,
                "monotonic": True,
                "segments": [
                    {
                        "kind": "keep",
                        "source_start": 0.0,
                        "source_end": 2.0,
                        "working_start": 0.0,
                        "working_end": 2.0,
                    },
                    {
                        "kind": "remove",
                        "candidate_id": "AD1",
                        "source_start": 2.0,
                        "source_end": 3.0,
                        "working_anchor": 2.0,
                    },
                    {
                        "kind": "keep",
                        "source_start": 3.0,
                        "source_end": 5.0,
                        "working_start": 2.0,
                        "working_end": 4.0,
                    },
                ],
            }
            _write_json(timeline_mapping, mapping)
            ad_gate = root / "qa" / "ad_edit_gate.json"
            _write_json(
                ad_gate,
                {
                    "schema_version": 2,
                    "status": "pass",
                    "working_master": {
                        "path": "source/working_v1.mp4",
                        "sha256": _sha(working),
                    },
                    "timeline_mapping": {
                        "path": "qa/source_to_edit_timeline.json",
                        "sha256": _sha(timeline_mapping),
                    },
                    "removed_segments": [{"id": "AD1", "start": 2, "end": 3}],
                    "formal_slots_based_on_working_master": False,
                },
            )

            segments = root / "work" / "tts_segments_v1.json"
            _write_json(
                segments,
                {
                    "schema_version": 1,
                    "status": "pass",
                    "time_basis": "source_master",
                    "segments": [
                        {
                            "segment_id": "S1",
                            "source_start": 1.0,
                            "source_end": 2.0,
                            "text": "第一句。",
                            "subtitle_zh": "第一句。",
                            "voice_id": "voice-a",
                            "speed": 1.0,
                        },
                        {
                            "segment_id": "S2",
                            "source_start": 3.0,
                            "source_end": 3.5,
                            "text": "第二句。",
                            "subtitle_zh": "第二句。",
                            "voice_id": "voice-a",
                            "speed": 1.0,
                        },
                    ],
                },
            )
            tts = root / "full_dub_v1" / "tts"
            raw_one = tts / "raw" / "S1.wav"
            raw_two = tts / "raw" / "S2.wav"
            _write_pcm(raw_one, 0.5, sample_rate=24_000)
            _write_pcm(raw_two, 0.75, sample_rate=32_000)
            manifest = tts / "manifest.json"
            _write_json(
                manifest,
                {
                    "schema_version": 1,
                    "status": "ready",
                    "locks": {
                        "provider_id": "fixture",
                        "profile_id": "fixture",
                        "model": "fixture",
                        "speed": 1.0,
                        "offline_rate": 1.0,
                        "segments_sha256": "f" * 64,
                    },
                    "ready_count": 2,
                    "segments": {
                        "S1": {
                            "segment_id": "S1",
                            "status": "ready",
                            "audio_path": "raw/S1.wav",
                            "sha256": _sha(raw_one),
                            "byte_size": raw_one.stat().st_size,
                        },
                        "S2": {
                            "segment_id": "S2",
                            "status": "ready",
                            "audio_path": "raw/S2.wav",
                            "sha256": _sha(raw_two),
                            "byte_size": raw_two.stat().st_size,
                        },
                    },
                },
            )
            result = build_audio_timeline(
                project_root=root,
                segments_path=segments,
                tts_manifest_path=manifest,
                working_master_path=working,
                ad_edit_gate_path=ad_gate,
                output_dir="full_dub_v1",
                ffmpeg=shutil.which("ffmpeg") or "ffmpeg",
                ffprobe=shutil.which("ffprobe") or "ffprobe",
                version=1,
            )
            self.assertEqual(result["status"], "pass")
            self.assertFalse(result["cached"])
            self.assertEqual(result["frame_width"], 320)
            self.assertEqual(result["frame_height"], 240)
            self.assertAlmostEqual(result["source_duration"], 4.0, places=2)
            # Source S2 3.0-3.5 maps to formal working-master 2.0-2.5,
            # proving deleted source time is not passed to the renderer.
            retime_dialogue = [
                row for row in result["retime_segments"] if row["kind"] == "dialogue"
            ]
            self.assertEqual(retime_dialogue[1]["source_start"], 2.0)
            self.assertEqual(retime_dialogue[1]["source_end"], 2.5)
            self.assertAlmostEqual(result["chinese_audio_duration"], 3.75, places=3)

            with wave.open(str(root / result["chinese_audio_path"]), "rb") as stream:
                self.assertEqual(stream.getframerate(), 48_000)
                self.assertEqual(stream.getnchannels(), 1)
                self.assertAlmostEqual(
                    stream.getnframes() / stream.getframerate(), 3.75, places=3
                )
            srt = (root / result["subtitle_path"]).read_text(encoding="utf-8")
            self.assertIn("第一句。", srt)
            self.assertIn("第二句。", srt)
            timeline = json.loads(
                (root / result["chinese_timeline_path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(timeline["subtitle_timeline_rebuilt"])
            self.assertEqual(timeline["overlap_count"], 0)
            self.assertEqual(timeline["tts_native_speed"], 1.0)
            self.assertEqual(timeline["offline_rate"], 1.0)
            self.assertEqual(timeline["tempo_filters"], [])
            serialized = json.dumps(timeline).lower()
            self.assertNotIn("atempo", serialized)
            self.assertNotIn("rubberband", serialized)

            resumed = build_audio_timeline(
                project_root=root,
                segments_path=segments,
                tts_manifest_path=manifest,
                working_master_path=working,
                ad_edit_gate_path=ad_gate,
                output_dir="full_dub_v1",
                ffmpeg=shutil.which("ffmpeg") or "ffmpeg",
                ffprobe=shutil.which("ffprobe") or "ffprobe",
                version=1,
            )
            self.assertTrue(resumed["cached"])
            self.assertEqual(
                resumed["chinese_timeline_sha256"], result["chinese_timeline_sha256"]
            )

    def test_refuses_a_source_segment_that_crosses_deleted_advertising(self) -> None:
        # This invariant is also exercised before any paid cache can be mapped:
        # a sentence crossing a removed interval must be regenerated from the
        # formal working master, never compressed across the edit.
        from backend.workbench.audio_timeline import _normalise_timeline_segments

        mapping = {
            "segments": [
                {
                    "kind": "keep",
                    "source_start": 0,
                    "source_end": 2,
                    "working_start": 0,
                    "working_end": 2,
                },
                {
                    "kind": "remove",
                    "source_start": 2,
                    "source_end": 3,
                    "working_anchor": 2,
                },
                {
                    "kind": "keep",
                    "source_start": 3,
                    "source_end": 5,
                    "working_start": 2,
                    "working_end": 4,
                },
            ],
            "source_duration_seconds": 5,
            "working_duration_seconds": 4,
        }
        with self.assertRaisesRegex(AudioTimelineError, "已删除广告区间"):
            _normalise_timeline_segments(
                [
                    {
                        "segment_id": "S1",
                        "source_start": 1.5,
                        "source_end": 3.5,
                        "text": "不应跨广告。",
                    }
                ],
                document={"time_basis": "source_master"},
                ad_gate={
                    "removed_segments": [{"start": 2, "end": 3}],
                    "formal_slots_based_on_working_master": False,
                },
                mapping=mapping,
                working_duration=4,
            )


if __name__ == "__main__":
    unittest.main()
