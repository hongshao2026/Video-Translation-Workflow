from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.workbench.gates import (
    ApprovalBindingError,
    ChapterReadingError,
    bind_translation_approval,
    build_chapter_reading,
    is_explicit_downstream_command,
    sha256_file,
    validate_chapter_reading,
    validate_gate,
    validate_translation_approval,
)


class DeterministicGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def artifact(self, relative: str, content: bytes | None = None) -> dict[str, str]:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if content is not None else relative.encode("utf-8"))
        return {"path": relative, "sha256": sha256_file(path)}

    def test_minimal_workflow_ad_production_and_publication_gates_pass(self) -> None:
        workflow = {
            "schema_version": 3,
            "status": "pass",
            "required_documents": [self.artifact("docs/workflow.md")],
            "current_stage": "01",
            "next_gate": "media_format_selection",
            "execution_mode": "local_runner_event_driven",
            "model_progress_polling": "forbidden",
            "document_loading": "first_load_then_hash_check_in_retained_context",
            "agent_handoff": "minimal_frozen_role_packet",
            "media_format_selection": "automatic_after_probe",
            "ad_policy": "detect_then_apply_evidence_based",
            "translation_mode": "provider_agent_direct_quality_first",
            "translation_review": "two_independent_agents_full_coverage",
            "chapter_reading_review": "required_before_translation_gate",
            "chapter_reading_layout": "sentence_aligned_verbatim",
            "translation_approval": "explicit_downstream_command_binds_current_version",
            "chinese_tts_speed": 1.0,
            "sync_strategy": "video_retime_only",
            "preserve_all_formal_working_master_frames": True,
            "audition_authorization": "voice_selection_implies_audition",
            "full_tts_authorization": "user_generate_full_command",
            "publication_package": "required_after_final_machine_qa",
            "publication_text_format": "utf8_txt_only",
            "cover_variants": "16x9_and_4x3",
        }
        self.assertTrue(validate_gate("workflow_lock", workflow, artifact_root=self.root))

        ad_gate = {
            "schema_version": 1,
            "status": "pass",
            "decision": "no_ads_detected",
            "source_master": self.artifact("source/master.mp4"),
            "working_master": self.artifact("work/master.mp4"),
            "timeline_mapping": self.artifact("qa/source_to_edit_timeline.json"),
            "removed_segments": [],
            "overlay_regions": [],
            "subtitle_layer_above_overlays": True,
            "unresolved_count": 0,
        }
        self.assertTrue(validate_gate("ad_edit_gate", ad_gate, artifact_root=self.root))

        production_gate = {
            "schema_version": 1,
            "status": "pass",
            "tts_native_speed": 1.0,
            "offline_rate": 1.0,
            "audio_time_stretch": False,
            "forbidden_audio_processors": [],
            "sync_strategy": "video_retime_only",
            "working_master_frames_preserved": True,
            "video_retime_plan": self.artifact("work/video_retime_plan.json"),
            "coverage_complete": True,
        }
        self.assertTrue(validate_gate("production_gate", production_gate, artifact_root=self.root))

        publication_gate = {
            "schema_version": 1,
            "status": "pass",
            "final_video": self.artifact("deliverables/final.mp4"),
            "final_machine_qa": self.artifact("qa/final_qa.json"),
            "chapter_mapping": self.artifact("qa/chapter_mapping.json"),
            "publication_text": {
                **self.artifact("deliverables/publication.txt"),
                "format": "utf8_txt",
                "ads_removed": True,
            },
            "cover_16x9": {
                **self.artifact("deliverables/cover_16x9.png"),
                "width": 1920,
                "height": 1080,
            },
            "cover_4x3": {
                **self.artifact("deliverables/cover_4x3.png"),
                "width": 1440,
                "height": 1080,
            },
        }
        self.assertTrue(validate_gate("publication_package_gate", publication_gate, artifact_root=self.root))

    def test_artifact_hash_mismatch_closes_gate(self) -> None:
        plan = self.artifact("work/video_retime_plan.json", b"v1")
        gate = {
            "schema_version": 1,
            "status": "pass",
            "tts_native_speed": 1.0,
            "offline_rate": 1.0,
            "audio_time_stretch": False,
            "forbidden_audio_processors": [],
            "sync_strategy": "video_retime_only",
            "working_master_frames_preserved": True,
            "video_retime_plan": plan,
            "coverage_complete": True,
        }
        (self.root / plan["path"]).write_bytes(b"v2")
        result = validate_gate("production_gate", gate, artifact_root=self.root)
        self.assertFalse(result.valid)
        self.assertTrue(any("哈希不匹配" in error for error in result.errors))

    def test_translation_gate_checks_role_and_approval_bindings(self) -> None:
        final = self.artifact("work/translation_final_v1.json")
        reading = self.artifact("deliverables/reading_v1.md")
        validation = self.artifact(
            "qa/chapter_reading_validation_v1.json",
            json.dumps(
                {
                    "status": "pass",
                    "input_translation_sha256": final["sha256"],
                    "output_sha256": reading["sha256"],
                }
            ).encode(),
        )
        approval_artifact = self.artifact(
            "qa/translation_approval_v1.json",
            json.dumps(
                {
                    "status": "pass",
                    "approved": True,
                    "binding": {
                        "formal_translation": final,
                        "chapter_reading": reading,
                    },
                }
            ).encode(),
        )
        gate = {
            "schema_version": 1,
            "status": "pass",
            "mode": "provider_agent_direct_quality_first",
            "translator": {
                "id": "agent-t",
                "missing": 0,
                "coverage": "S001-S003",
                "report": self.artifact("qa/t.json"),
            },
            "reviewers": [
                {
                    "id": "agent-a",
                    "missing": 0,
                    "coverage": "S001-S003",
                    "report": self.artifact("qa/a.json"),
                },
                {
                    "id": "agent-b",
                    "missing": 0,
                    "coverage": "S001-S003",
                    "report": self.artifact("qa/b.json"),
                },
            ],
            "orchestrator_id": "orchestrator",
            "roles_are_distinct": True,
            "unresolved_high": 0,
            "unresolved_total": 0,
            "regression_status": "pass",
            "final_translation": final,
            "chapter_reading": {
                **reading,
                "validation": validation,
                "status": "pass",
                "missing_slots": 0,
                "duplicate_slots": 0,
                "subtitle_text_used_verbatim": True,
                "subtitle_text_reconstructable_per_stable_id": True,
                "layout": "sentence_aligned_verbatim",
            },
            "user_approval": {
                "artifact": approval_artifact,
                "approved": True,
                "capture_mode": "explicit_downstream_command",
                "command": "开始选音色",
                "reading_sha256": reading["sha256"],
                "final_translation_sha256": final["sha256"],
            },
        }
        self.assertTrue(validate_gate("translation_gate", gate, artifact_root=self.root))
        gate["user_approval"]["reading_sha256"] = "0" * 64
        result = validate_gate("translation_gate", gate, artifact_root=self.root)
        self.assertFalse(result.valid)
        self.assertTrue(any("阅读稿不匹配" in error for error in result.errors))


class ChapterReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()
        (self.root / "deliverables").mkdir()
        (self.root / "qa").mkdir()
        self.translation = self.root / "work" / "translation_final_v1.json"
        self.translation.write_text(
            json.dumps(
                {
                    "slots": [
                        {"id": "S001", "subtitle_zh": "你好，", "recording_zh": "你好"},
                        {"id": "S002", "subtitle_zh": "这是同一句。"},
                        {"id": "S003", "subtitle_zh": "下一句！"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.reading = self.root / "deliverables" / "video_中文阅读版_按章节_v1.md"
        self.validation = self.root / "qa" / "chapter_reading_validation_v1.json"
        self.chapters = [
            {
                "id": "C01",
                "title": "开场",
                "working_start": "00:00:00",
                "source_start": "00:00:00",
                "slot_ids": ["S001", "S002", "S003"],
            }
        ]

    def build(self) -> dict:
        return build_chapter_reading(
            self.translation,
            self.chapters,
            self.reading,
            self.validation,
            video_id="fixture",
            chapter_source="source",
            run_root=self.root,
        )

    def test_sentence_aligned_reading_is_verbatim_and_reconstructable(self) -> None:
        report = self.build()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["slot_count"], 3)
        self.assertTrue(report["subtitle_text_used_verbatim"])
        self.assertTrue(report["subtitle_text_reconstructable_per_stable_id"])
        rendered = self.reading.read_text(encoding="utf-8")
        s1_end = rendered.index("<!--dub-eid:")
        s2_start = rendered.index("<!--dub-sid:", s1_end)
        self.assertNotIn("\n", rendered[s1_end:s2_start])
        self.assertNotIn("recording_zh", rendered)

    def test_missing_and_duplicate_chapter_assignment_is_rejected(self) -> None:
        missing = [{"id": "C01", "title": "开场", "slot_ids": ["S001", "S003"]}]
        with self.assertRaisesRegex(ChapterReadingError, "缺失"):
            build_chapter_reading(
                self.translation,
                missing,
                self.reading,
                self.validation,
                run_root=self.root,
            )
        duplicate = [
            {"id": "C01", "title": "开场", "slot_ids": ["S001", "S002"]},
            {"id": "C02", "title": "重复", "slot_ids": ["S002", "S003"]},
        ]
        with self.assertRaisesRegex(ChapterReadingError, "重复"):
            build_chapter_reading(
                self.translation,
                duplicate,
                self.reading,
                self.validation,
                run_root=self.root,
            )

    def test_validator_reports_missing_and_duplicate_markers(self) -> None:
        self.build()
        original = self.reading.read_text(encoding="utf-8")
        slot_matches = list(
            __import__("re").finditer(
                r"<!--dub-sid:[A-Za-z0-9_-]+-->.*?<!--dub-eid:[A-Za-z0-9_-]+-->",
                original,
                __import__("re").DOTALL,
            )
        )
        without_second = original[: slot_matches[1].start()] + original[slot_matches[1].end() :]
        self.reading.write_text(without_second, encoding="utf-8")
        missing = validate_chapter_reading(self.translation, self.reading, run_root=self.root)
        self.assertEqual(missing["status"], "fail")
        self.assertEqual(missing["missing_slot_ids"], ["S002"])

        duplicated = original[: slot_matches[1].end()] + slot_matches[1].group(0) + original[slot_matches[1].end() :]
        self.reading.write_text(duplicated, encoding="utf-8")
        duplicate = validate_chapter_reading(self.translation, self.reading, run_root=self.root)
        self.assertEqual(duplicate["status"], "fail")
        self.assertEqual(duplicate["duplicate_slot_ids"], ["S002"])

    def test_newline_only_revision_invalidates_old_approval(self) -> None:
        self.build()
        approval_path = self.root / "qa" / "translation_approval_v1.json"
        approval = bind_translation_approval(
            "开始选音色",
            translation_path=self.translation,
            reading_path=self.reading,
            validation_path=self.validation,
            approval_path=approval_path,
            run_root=self.root,
            approved_at_utc="2026-09-23T00:00:00+00:00",
        )
        self.assertTrue(
            validate_translation_approval(
                approval,
                translation_path=self.translation,
                reading_path=self.reading,
                validation_path=self.validation,
            )
        )
        before = sha256_file(self.reading)
        text = self.reading.read_text(encoding="utf-8")
        self.reading.write_text(text.replace("\n\n<!--dub-sid:", "\n\n\n<!--dub-sid:", 1), encoding="utf-8")
        self.assertNotEqual(before, sha256_file(self.reading))
        revised = validate_chapter_reading(self.translation, self.reading, run_root=self.root)
        self.assertEqual(revised["status"], "pass")
        stale = validate_translation_approval(
            approval,
            translation_path=self.translation,
            reading_path=self.reading,
            validation_path=self.validation,
        )
        self.assertFalse(stale.valid)
        self.assertTrue(any("旧批准已失效" in error for error in stale.errors))

    def test_generic_continue_cannot_bind_approval(self) -> None:
        self.build()
        self.assertFalse(is_explicit_downstream_command("继续"))
        with self.assertRaises(ApprovalBindingError):
            bind_translation_approval(
                "继续",
                translation_path=self.translation,
                reading_path=self.reading,
                validation_path=self.validation,
                run_root=self.root,
            )


if __name__ == "__main__":
    unittest.main()
