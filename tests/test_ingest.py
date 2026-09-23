from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.workbench.gates import validate_gate
from backend.workbench.ingest import (
    AdEvidenceError,
    PathSafetyError,
    TranscriptError,
    build_ad_edit_artifacts,
    build_embedded_subtitle_plan,
    build_ffprobe_plan,
    build_source_to_edit_timeline,
    import_source_transcript,
    map_source_time,
    materialize_ad_edit_artifacts,
    parse_ffprobe_payload,
    validate_ad_analysis,
    write_frozen_source,
)


class IngestFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for relative in ("source", "work", "qa"):
            (self.root / relative).mkdir()
        self.original = self.root / "source" / "original.mp4"
        self.original.write_bytes(b"original-master-never-overwrite")
        self.working = self.root / "source" / "working_v1.mp4"
        self.working.write_bytes(b"independent-working-master")

    @staticmethod
    def completed_report(candidates: list[dict] | None = None, **values) -> dict:
        report = {
            "status": "pass",
            "content_scan_complete": True,
            "visual_scan_complete": True,
            "semantic_analysis_required": True,
            "semantic_analysis_complete": True,
            "analysis_method": "model_reviewed_evidence",
            "decision_rule_version": "ad-evidence-v1",
            "candidates": candidates or [],
        }
        report.update(values)
        return report


class MediaPlanTests(IngestFixture):
    def test_ffprobe_plan_is_shell_free_and_uses_project_relative_input(self) -> None:
        plan = build_ffprobe_plan(self.original, project_root=self.root)
        payload = plan.to_dict()
        self.assertEqual(payload["kind"], "media.ffprobe")
        self.assertFalse(payload["shell"])
        self.assertEqual(payload["input_path"], "source/original.mp4")
        self.assertEqual(payload["argv"][-1], "source/original.mp4")

    def test_media_report_exposes_embedded_subtitle_stream(self) -> None:
        report = parse_ffprobe_payload(
            {
                "format": {"duration": "12.5", "size": "1234", "format_name": "mov,mp4"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "r_frame_rate": "30000/1001",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                        "tags": {"language": "es"},
                    },
                    {
                        "index": 3,
                        "codec_type": "subtitle",
                        "codec_name": "mov_text",
                        "tags": {"language": "es", "title": "Español"},
                    },
                ],
            }
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["video"]["width"], 1920)
        self.assertAlmostEqual(report["video"]["frame_rate"], 29.97003)
        self.assertEqual(report["subtitle_streams"][0]["stream_index"], 3)

    def test_embedded_subtitle_plan_never_overwrites_and_rejects_escape(self) -> None:
        plan = build_embedded_subtitle_plan(
            self.original,
            "work/embedded_es.vtt",
            stream_index=3,
            project_root=self.root,
        )
        self.assertIn("-n", plan.argv)
        self.assertNotIn("-y", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("-map") + 1], "0:3")
        with self.assertRaises(PathSafetyError):
            build_embedded_subtitle_plan(
                self.original,
                "../escaped.srt",
                stream_index=3,
                project_root=self.root,
            )


class TranscriptImportTests(IngestFixture):
    def test_srt_import_normalizes_continuous_stable_ids_and_can_freeze_once(self) -> None:
        source = self.root / "work" / "source.srt"
        source.write_text(
            "1\n00:00:01,000 --> 00:00:02,250\nHello\nworld\n\n"
            "7\n00:00:03,000 --> 00:00:04,000\nAgain\n",
            encoding="utf-8",
        )
        document = import_source_transcript(source, project_root=self.root, language="en")
        self.assertEqual(document["stable_ids"], ["S000001", "S000002"])
        self.assertEqual(document["slots"][0]["source_text"], "Hello world")
        self.assertEqual(document["slots"][0]["imported_id"], "1")
        self.assertEqual(document["slots"][1]["position"], 1)

        artifact = write_frozen_source(
            document,
            "work/frozen_source_v1.json",
            project_root=self.root,
        )
        self.assertEqual(len(artifact["sha256"]), 64)
        with self.assertRaises(FileExistsError):
            write_frozen_source(
                document,
                "work/frozen_source_v1.json",
                project_root=self.root,
            )

    def test_vtt_and_json_import_use_the_same_stable_id_scheme(self) -> None:
        vtt = self.root / "work" / "source.vtt"
        vtt.write_text(
            "WEBVTT\n\nalpha\n00:01.000 --> 00:02.000\nOne\n\n"
            "beta\n00:03.000 --> 00:04.500\nTwo\n",
            encoding="utf-8",
        )
        json_source = self.root / "work" / "source.json"
        json_source.write_text(
            json.dumps(
                {
                    "segments": [
                        {"id": "alpha", "start_ms": 1000, "end_ms": 2000, "text": "One"},
                        {"id": "beta", "start": 3, "end": 4.5, "source_text": "Two"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        vtt_document = import_source_transcript(vtt, project_root=self.root)
        json_document = import_source_transcript(json_source, project_root=self.root)
        self.assertEqual(vtt_document["stable_ids"], json_document["stable_ids"])
        self.assertEqual(
            [(row["start"], row["end"], row["source_text"]) for row in vtt_document["slots"]],
            [(row["start"], row["end"], row["source_text"]) for row in json_document["slots"]],
        )

    def test_non_monotonic_source_is_rejected(self) -> None:
        source = self.root / "work" / "bad.json"
        source.write_text(
            json.dumps(
                [
                    {"start": 5, "end": 6, "text": "later"},
                    {"start": 1, "end": 2, "text": "earlier"},
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(TranscriptError):
            import_source_transcript(source, project_root=self.root)


class AdvertisementGateTests(IngestFixture):
    def test_model_dependent_scan_cannot_fabricate_no_ads(self) -> None:
        result = validate_ad_analysis(
            {
                "status": "analysis_required",
                "content_scan_complete": True,
                "visual_scan_complete": True,
                "semantic_analysis_required": True,
                "semantic_analysis_complete": False,
                "candidates": [],
            },
            source_duration=30,
            frame_width=1920,
            frame_height=1080,
        )
        self.assertEqual(result.status, "analysis_required")
        self.assertIn("semantic_analysis_required", result.blockers)
        bundle = build_ad_edit_artifacts(
            project_root=self.root,
            source_master=self.original,
            working_master=self.working,
            analysis=result.normalized or {},
            source_duration=30,
            frame_width=1920,
            frame_height=1080,
        )
        self.assertEqual(bundle["status"], "analysis_required")
        self.assertIsNone(bundle["ad_edit_gate"])

    def test_remove_requires_semantics_safe_boundary_and_ten_second_context(self) -> None:
        candidate = {
            "id": "AD001",
            "kind": "content",
            "start": 10,
            "end": 20,
            "action": "remove",
            "confidence": 0.95,
            "rationale": "sponsor segment",
            "boundary_safe": True,
            "context_before_seconds": 4,
            "context_after_seconds": 10,
            "evidence": [
                {"kind": "asr_semantic", "summary": "explicit sponsor wording"},
                {"kind": "pause", "summary": "clean return boundary"},
            ],
        }
        result = validate_ad_analysis(
            self.completed_report([candidate]), source_duration=60
        )
        self.assertEqual(result.status, "fail")
        self.assertTrue(any("至少 10 秒" in error for error in result.errors))

        candidate["context_before_seconds"] = 10
        passed = validate_ad_analysis(
            self.completed_report([candidate]), source_duration=60
        )
        self.assertTrue(passed.ok)
        timeline = build_source_to_edit_timeline(
            60, [row for row in passed.normalized["candidates"] if row["action"] == "remove"]
        )
        self.assertEqual(timeline["working_duration_seconds"], 50)
        self.assertEqual(map_source_time(timeline, 15), 10)
        self.assertEqual(map_source_time(timeline, 25), 15)

    def test_mask_requires_consistent_in_bounds_pixel_and_normalized_coordinates(self) -> None:
        candidate = {
            "id": "AD-MASK",
            "kind": "visual",
            "start": 5,
            "end": 12,
            "action": "mask",
            "confidence": 0.9,
            "rationale": "promotional QR code",
            "program_content_clear": True,
            "evidence": [
                {"kind": "ocr", "summary": "discount URL and QR detected"},
                {"kind": "screenshot", "summary": "corner evidence frame"},
            ],
            "region": {
                "frame_width": 1920,
                "frame_height": 1080,
                "normalized": {"x": 0.8, "y": 0.8, "width": 0.15, "height": 0.15},
                "pixels": {"x": 1536, "y": 864, "width": 400, "height": 162},
                "safe_margin_pixels": 8,
                "mode": "blur",
            },
        }
        failed = validate_ad_analysis(
            self.completed_report([candidate]),
            source_duration=30,
            frame_width=1920,
            frame_height=1080,
        )
        self.assertEqual(failed.status, "fail")
        self.assertTrue(any("越出画面" in error or "不一致" in error for error in failed.errors))

        candidate["region"]["pixels"]["width"] = 288
        passed = validate_ad_analysis(
            self.completed_report([candidate]),
            source_duration=30,
            frame_width=1920,
            frame_height=1080,
        )
        self.assertTrue(passed.ok)
        self.assertEqual(passed.normalized["decision"], "ads_overlaid")

    def test_no_ads_completed_scan_auto_passes_identity_gate_and_preserves_source(self) -> None:
        source_before = self.original.read_bytes()
        analysis = self.completed_report([], decision="no_ads_detected")
        bundle = build_ad_edit_artifacts(
            project_root=self.root,
            source_master=self.original,
            working_master=self.working,
            analysis=analysis,
            source_duration=30,
            frame_width=1920,
            frame_height=1080,
        )
        gate = bundle["ad_edit_gate"]
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["decision"], "no_ads_detected")
        self.assertTrue(gate["original_master_preserved"])
        self.assertEqual(gate["removed_segments"], [])
        self.assertEqual(gate["overlay_regions"], [])
        self.assertTrue(bundle["documents"]["qa/source_to_edit_timeline.json"]["identity"])

        written = materialize_ad_edit_artifacts(bundle, project_root=self.root)
        self.assertEqual(len(written), 5)
        self.assertEqual(self.original.read_bytes(), source_before)
        stored_gate = json.loads((self.root / "qa" / "ad_edit_gate.json").read_text(encoding="utf-8"))
        timeline_bytes = (self.root / "qa" / "source_to_edit_timeline.json").read_bytes()
        import hashlib

        self.assertEqual(
            stored_gate["timeline_mapping"]["sha256"],
            hashlib.sha256(timeline_bytes).hexdigest(),
        )
        gate_validation = validate_gate(
            "ad_edit_gate", stored_gate, artifact_root=self.root
        )
        self.assertTrue(gate_validation.valid, gate_validation.errors)

    def test_no_ads_deterministic_scan_does_not_claim_model_analysis(self) -> None:
        analysis = self.completed_report(
            [],
            decision="no_ads_detected",
            semantic_analysis_required=False,
            semantic_analysis_complete=False,
            analysis_method="deterministic_full_scan",
        )
        bundle = build_ad_edit_artifacts(
            project_root=self.root,
            source_master=self.original,
            working_master=self.working,
            analysis=analysis,
            source_duration=30,
            frame_width=1920,
            frame_height=1080,
        )
        gate = bundle["ad_edit_gate"]
        self.assertEqual(gate["status"], "pass")
        self.assertFalse(gate["semantic_analysis_required"])
        self.assertFalse(gate["semantic_analysis_complete"])

    def test_source_and_working_master_cannot_alias(self) -> None:
        with self.assertRaises(PathSafetyError):
            build_ad_edit_artifacts(
                project_root=self.root,
                source_master=self.original,
                working_master=self.original,
                analysis=self.completed_report([]),
                source_duration=30,
            )

    def test_overlapping_remove_intervals_are_rejected(self) -> None:
        with self.assertRaises(AdEvidenceError):
            build_source_to_edit_timeline(
                30,
                [
                    {"id": "A", "start": 2, "end": 8},
                    {"id": "B", "start": 7, "end": 10},
                ],
            )


if __name__ == "__main__":
    unittest.main()
