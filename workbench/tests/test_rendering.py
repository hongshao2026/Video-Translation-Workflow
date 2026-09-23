from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.workbench.rendering import (
    PRODUCTION_ARTIFACT_KEYS,
    MachineQAError,
    RenderPlanError,
    build_machine_qa_commands,
    build_production_gate_report,
    build_render_plan,
    evaluate_machine_qa,
    run_machine_qa,
    validate_render_plan,
)


def _segments() -> list[dict[str, float]]:
    return [
        {
            "source_start": 0.0,
            "source_end": 4.0,
            "target_start": 0.0,
            "target_end": 5.0,
        },
        {
            "source_start": 4.0,
            "source_end": 10.0,
            "target_start": 5.0,
            "target_end": 11.0,
        },
    ]


def _plan(**overrides):
    values = {
        "working_master": "media/working-master.mp4",
        "chinese_audio": "audio/chinese.wav",
        "subtitle_file": "subtitles/subtitles.zh.srt",
        "output_file": "deliverables/final.zh.mp4",
        "source_duration": 10.0,
        "frame_width": 1920,
        "frame_height": 1080,
        "frame_rate": "30000/1001",
        "retime_segments": _segments(),
        "chinese_audio_duration": 11.0,
    }
    values.update(overrides)
    return build_render_plan(**values)


def _probe_payload(*, subtitle: bool = True, dts=(0.0, 0.033, 0.067)):
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
        },
    ]
    if subtitle:
        streams.append(
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "mov_text",
                "nb_frames": "2",
                "tags": {"language": "zho"},
            }
        )
    return {
        "format": {"duration": "11.000", "size": "1000"},
        "streams": streams,
        "packets": [{"dts_time": str(value)} for value in dts],
    }


class RenderPlanTests(unittest.TestCase):
    def test_plan_retimes_only_video_masks_before_subtitles_and_maps_soft_track(self) -> None:
        plan = _plan(
            subtitle_file=r"C:\project\subtitles.zh.srt",
            overlay_regions=[
                {
                    "id": "promo-box",
                    "source_start": 2.0,
                    "source_end": 3.0,
                    "x": 100,
                    "y": 80,
                    "width": 420,
                    "height": 120,
                    "verified": True,
                    "color": "#101010",
                    "opacity": 0.95,
                }
            ],
        )
        graph = plan["filter_graph"]
        self.assertIn("setpts=(PTS-STARTPTS)*1.25", graph)
        self.assertIn("drawbox=", graph)
        self.assertIn("subtitles=filename='C\\:/project/subtitles.zh.srt'", graph)
        self.assertLess(graph.index("setpts="), graph.index("drawbox="))
        self.assertLess(graph.index("drawbox="), graph.rindex("subtitles="))
        self.assertTrue(graph.endswith("[vout]"))
        self.assertEqual(plan["overlay_regions"][0]["target_start"], 2.5)
        self.assertEqual(plan["overlay_regions"][0]["target_end"], 3.75)
        command = plan["ffmpeg_command"]
        maps = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "-map"]
        self.assertEqual(maps, ["[vout]", "1:a:0", "2:0"])
        self.assertIn("mov_text", command)
        self.assertNotIn("-shortest", command)
        self.assertNotIn("-af", command)
        self.assertEqual(validate_render_plan(plan), [])

    def test_rejects_any_source_gap(self) -> None:
        segments = _segments()
        segments[1]["source_start"] = 4.5
        with self.assertRaisesRegex(RenderPlanError, "cover every retained"):
            _plan(retime_segments=segments)

    def test_explicit_ad_deletion_allows_only_the_retained_complement(self) -> None:
        plan = _plan(
            retime_segments=[
                {"source_start": 0, "source_end": 4, "target_start": 0, "target_end": 4},
                {"source_start": 6, "source_end": 10, "target_start": 4, "target_end": 8},
            ],
            removed_intervals=[(4, 6)],
            chinese_audio_duration=8,
        )
        self.assertTrue(plan["coverage_complete"])
        self.assertEqual(plan["removed_ad_intervals"][0]["source_start"], 4)

    def test_rejects_unverified_or_out_of_bounds_overlay(self) -> None:
        unverified = {
            "source_start": 1,
            "source_end": 2,
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 100,
        }
        with self.assertRaisesRegex(RenderPlanError, "lacks"):
            _plan(overlay_regions=[unverified])
        invalid = {**unverified, "verified": True, "x": 1900}
        with self.assertRaisesRegex(RenderPlanError, "outside"):
            _plan(overlay_regions=[invalid])

    def test_rejects_audio_speed_change_or_duration_truncation(self) -> None:
        with self.assertRaisesRegex(RenderPlanError, "exactly 1.0"):
            _plan(tts_native_speed=1.1)
        with self.assertRaisesRegex(RenderPlanError, "exactly 1.0"):
            _plan(offline_rate=0.9)
        with self.assertRaisesRegex(RenderPlanError, "must not be stretched or truncated"):
            _plan(chinese_audio_duration=10.0)


class ProductionGateTests(unittest.TestCase):
    def artifacts(self):
        return {
            key: {"path": f"qa/{key}.json", "sha256": f"{index:x}" * 64}
            for index, key in enumerate(PRODUCTION_ARTIFACT_KEYS, start=1)
        }

    def gate(self, **overrides):
        values = {
            "artifacts": self.artifacts(),
            "ad_edit_gate_status": "pass",
            "translation_gate_status": "pass",
            "working_master_and_overlay_hashes_match": True,
            "translation_hash_matches": True,
            "voice_mapping_locked": True,
            "authorization_bound": True,
            "automatic_dry_run_recorded": True,
            "tts_speeds": [1.0, 1.0],
            "offline_rates": [1.0],
            "chinese_overlap_count": 0,
            "subtitle_timeline_rebuilt": True,
        }
        values.update(overrides)
        return build_production_gate_report(_plan(), **values)

    def test_complete_report_passes_and_matches_gate_schema_fields(self) -> None:
        report = self.gate()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["tts_native_speed"], 1.0)
        self.assertEqual(report["offline_rate"], 1.0)
        self.assertFalse(report["audio_time_stretch"])
        self.assertEqual(report["forbidden_audio_processors"], [])
        self.assertTrue(report["working_master_frames_preserved"])
        self.assertTrue(report["coverage_complete"])
        self.assertRegex(report["video_retime_plan"]["sha256"], r"^[0-9a-f]{64}$")

    def test_non_native_tts_or_missing_artifact_is_a_hard_failure(self) -> None:
        artifacts = self.artifacts()
        del artifacts["voice_mapping"]
        report = self.gate(artifacts=artifacts, tts_speeds=[1.0, 1.05])
        self.assertEqual(report["status"], "fail")
        self.assertIn("all_tts_speed_1_0", report["failure_codes"])
        self.assertIn("artifact_invalid:voice_mapping", report["failure_codes"])


class MachineQATests(unittest.TestCase):
    def expected(self):
        return {
            "duration": 11.0,
            "width": 1920,
            "height": 1080,
            "frame_rate": "30000/1001",
            "subtitle_count": 2,
            "subtitle_language": "zho",
            "require_strict_video_dts": True,
        }

    def test_command_plan_uses_required_probe_and_strict_decode_options(self) -> None:
        commands = build_machine_qa_commands("final.mp4")
        self.assertIn("-show_packets", commands["video_dts"])
        self.assertEqual(commands["decode"][:4], ["ffmpeg", "-v", "error", "-xerror"])
        self.assertIn("0:v:0", commands["decode"])
        self.assertIn("0:a:0", commands["decode"])

    def test_evaluate_accepts_three_streams_and_strict_dts(self) -> None:
        report = evaluate_machine_qa(_probe_payload(), self.expected())
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["checks"]["video_dts_strictly_increasing"])
        self.assertEqual(report["observed"]["subtitle_stream_count"], 1)

    def test_missing_subtitle_non_monotonic_dts_and_decode_error_fail(self) -> None:
        report = evaluate_machine_qa(
            _probe_payload(subtitle=False, dts=(0.0, 0.033, 0.02)),
            self.expected(),
            decode_returncode=1,
            decode_stderr="corrupt frame",
        )
        self.assertEqual(report["status"], "fail")
        self.assertIn("subtitle_stream_present", report["failure_codes"])
        self.assertIn("video_dts_strictly_increasing", report["failure_codes"])
        self.assertIn("full_video_audio_decode", report["failure_codes"])
        self.assertEqual(report["decode_error"], "corrupt frame")

    def test_run_machine_qa_calls_two_probes_and_decode_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "final.mp4"
            media.write_bytes(b"small-fixture")
            calls: list[list[str]] = []

            def fake_runner(command, **kwargs):
                del kwargs
                calls.append(list(command))
                if "-show_packets" in command:
                    payload = {"packets": _probe_payload()["packets"]}
                elif command[0] == "ffprobe":
                    payload = _probe_payload()
                    payload.pop("packets")
                else:
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            report = run_machine_qa(media, self.expected(), command_runner=fake_runner)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(report["sha256"]), 64)
            self.assertTrue(report["qa_commands"]["ffmpeg_error_xerror_decode"])

    def test_run_machine_qa_refuses_missing_file(self) -> None:
        with self.assertRaisesRegex(MachineQAError, "does not exist"):
            run_machine_qa("missing.mp4", self.expected())


if __name__ == "__main__":
    unittest.main()
