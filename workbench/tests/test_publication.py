from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.workbench import publication
from backend.workbench.gates import validate_gate
from backend.workbench.publication import (
    TimelineMappingError,
    build_final_chapter_timeline,
    build_publication_package_gate,
    create_cover_art,
    create_publication_materials,
    map_timestamp,
    sha256_file,
    validate_publication_content,
    write_final_chapter_timeline,
)


def _json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class PublicationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "video_run"
        (self.root / "deliverables").mkdir(parents=True)
        (self.root / "qa").mkdir(parents=True)
        self.video = self.root / "deliverables" / "final_v1.mp4"
        self.video.write_bytes(b"deterministic-final-video")
        self.video_sha = sha256_file(self.video)
        self.machine_qa = self.root / "qa" / "final_machine_qa_v1.json"
        _json(
            self.machine_qa,
            {
                "schema_version": 1,
                "status": "pass",
                "sha256": self.video_sha,
                "observed": {"duration": 99.0},
            },
        )
        self.source_map = self.root / "qa" / "source_to_edit_timeline.json"
        _json(
            self.source_map,
            {
                "segments": [
                    {"source_start": 0, "source_end": 40, "target_start": 0, "target_end": 40},
                    {"source_start": 50, "source_end": 100, "target_start": 40, "target_end": 90},
                ]
            },
        )
        self.retime_map = self.root / "qa" / "working_to_final_v1.json"
        _json(
            self.retime_map,
            {
                "segments": [
                    {"working_start": 0, "working_end": 40, "output_start": 0, "output_end": 44},
                    {"working_start": 40, "working_end": 90, "output_start": 44, "output_end": 99},
                ]
            },
        )
        self.chapters = [
            {"id": "C01", "title": "开场", "source_seconds": 0},
            {"id": "C02", "title": "核心讨论", "source_seconds": 50},
            {"id": "C03", "title": "总结", "source_seconds": 90},
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_timeline(self, version: int = 1) -> Path:
        write_final_chapter_timeline(
            self.root,
            "demo",
            version,
            final_video_path=self.video,
            final_machine_qa_path=self.machine_qa,
            source_chapters=self.chapters,
            source_to_working_path=self.source_map,
            working_to_final_path=self.retime_map,
        )
        return self.root / "qa" / f"chapter_timeline_v{version}.json"

    def write_materials(self, timeline: Path, version: int = 1) -> Path:
        report = create_publication_materials(
            self.root,
            "demo",
            version,
            final_video_path=self.video,
            final_machine_qa_path=self.machine_qa,
            chapter_timeline_path=timeline,
            titles=["深度对话一", "深度对话二", "深度对话三"],
            description="本期讨论策略、决策与实践。\n原视频：https://youtu.be/example",
            books=["思考，快与慢"],
            original_video_url="https://youtu.be/example",
            foreign_names_verified=True,
            removed_promotion_categories=["discount", "subscription"],
        )
        self.assertEqual(report["status"], "pass")
        return self.root / "qa" / f"publication_materials_v{version}.json"

    def write_source_frame(self) -> Path:
        if not publication.pillow_capability()["available"]:
            self.skipTest("Pillow is not installed")
        source = self.root / "source" / "frame.png"
        source.parent.mkdir(parents=True)
        image = publication.Image.new("RGB", (640, 360), "#8C5B45")
        image.save(source, format="PNG")
        return source

    def write_covers(self, version: int = 1) -> Path:
        report = create_cover_art(
            self.root,
            "demo",
            version,
            source_image_path=self.write_source_frame(),
            title="如何做出更好的决策",
            source_authorized=True,
            source_clean_verified=True,
            identity_verified=True,
            text_verified=True,
        )
        self.assertEqual(report["status"], "pass")
        return self.root / "qa" / f"cover_art_v{version}.json"


class TimelineTests(PublicationFixture):
    def test_two_stage_mapping_and_strict_final_chapters(self) -> None:
        source = json.loads(self.source_map.read_text(encoding="utf-8"))
        retime = json.loads(self.retime_map.read_text(encoding="utf-8"))
        payload = build_final_chapter_timeline(
            self.chapters,
            source,
            retime,
            final_duration=99,
            final_video_sha256="a" * 64,
            final_machine_qa_sha256="b" * 64,
        )
        self.assertEqual([row["final_seconds"] for row in payload["chapters"]], [0.0, 44.0, 88.0])
        self.assertEqual([row["timestamp"] for row in payload["chapters"]], ["0:00", "0:44", "1:28"])
        self.assertTrue(payload["checks"]["source_to_output_mapping_valid"])

    def test_removed_gap_and_same_whole_second_are_rejected(self) -> None:
        mapping = json.loads(self.source_map.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(TimelineMappingError, "outside the retained timeline"):
            map_timestamp(45, mapping)
        close_chapters = [
            {"id": "a", "title": "A", "start": 0},
            {"id": "b", "title": "B", "start": 0.5},
        ]
        identity = {"segments": [{"source_start": 0, "source_end": 10, "target_start": 0, "target_end": 10}]}
        with self.assertRaisesRegex(TimelineMappingError, "whole-second"):
            build_final_chapter_timeline(
                close_chapters,
                identity,
                identity,
                final_duration=10,
                final_video_sha256="a" * 64,
                final_machine_qa_sha256="b" * 64,
            )

    def test_accepts_existing_edit_blocks_and_retained_range_formats(self) -> None:
        edit_segments = {
            "segments": [
                {"source_start": 0, "source_end": 10, "edit_start": 0, "edit_end": 10}
            ]
        }
        self.assertEqual(map_timestamp(6, edit_segments), 6)
        retained_ranges = {
            "retained_ranges": [
                {"source_seconds": [0, 10], "working_seconds": [0, 8]}
            ]
        }
        self.assertEqual(map_timestamp(5, retained_ranges), 4)
        block_timeline = {
            "blocks": [
                {"source_start": 0, "source_end": 8, "target_start": 0, "target_end": 12}
            ]
        }
        self.assertEqual(map_timestamp(4, block_timeline), 6)

    def test_writer_binds_machine_qa_and_never_overwrites(self) -> None:
        path = self.write_timeline()
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["final_video_sha256"], self.video_sha)
        self.assertEqual(payload["source_to_working_timeline"]["sha256"], sha256_file(self.source_map))
        with self.assertRaises(FileExistsError):
            self.write_timeline()
        self.video.write_bytes(b"changed-after-qa")
        with self.assertRaisesRegex(Exception, "different video"):
            self.write_timeline(version=2)


class PublicationTextTests(PublicationFixture):
    def test_original_link_is_preserved_but_other_links_and_promotions_fail(self) -> None:
        clean = validate_publication_content(
            titles=["A", "B", "C"],
            description="简介 https://youtu.be/source",
            chapter_titles=["开场", "讨论"],
            books=["一本书"],
            original_video_url="https://youtu.be/source",
        )
        self.assertTrue(clean.valid)
        bad = validate_publication_content(
            titles=["A", "B", "C"],
            description=(
                "简介 https://youtu.be/source 扫码订阅，优惠码见 "
                "https://sales.example.com"
            ),
            chapter_titles=["开场"],
            books=[],
            original_video_url="https://youtu.be/source",
        )
        self.assertFalse(bad.valid)
        categories = {issue["category"] for issue in bad.issues}
        self.assertTrue({"qr_code", "subscription", "discount", "non_source_url"} <= categories)

    def test_writes_three_utf8_txt_files_and_failure_writes_no_formal_text(self) -> None:
        timeline = self.write_timeline()
        report_path = self.write_materials(timeline)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["advertising_or_promotional_links_remaining"], 0)
        for artifact in report["artifacts"].values():
            path = self.root / artifact["path"]
            self.assertEqual(path.suffix, ".txt")
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            raw.decode("utf-8")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact["sha256"])
        with self.assertRaises(FileExistsError):
            self.write_materials(timeline)

        failed = create_publication_materials(
            self.root,
            "demo",
            2,
            final_video_path=self.video,
            final_machine_qa_path=self.machine_qa,
            chapter_timeline_path=timeline,
            titles=["点击订阅", "B", "C"],
            description="优惠详情 https://youtu.be/example",
            books=[],
            original_video_url="https://youtu.be/example",
            foreign_names_verified=True,
        )
        self.assertEqual(failed["status"], "fail")
        self.assertFalse((self.root / "deliverables" / "demo_发布材料_v2.txt").exists())
        self.assertTrue((self.root / "qa" / "publication_materials_v2.json").is_file())


class CoverTests(PublicationFixture):
    def test_pillow_generates_two_decodable_relayouts_at_exact_dimensions(self) -> None:
        report_path = self.write_covers()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["checks"]["four_by_three_relayout_not_crop"])
        for key, expected in (("cover_16x9", (1920, 1080)), ("cover_4x3", (1440, 1080))):
            artifact = report["covers"][key]
            path = self.root / artifact["path"]
            with publication.Image.open(path) as opened:
                self.assertEqual(opened.format, "PNG")
                self.assertEqual(opened.size, expected)
            self.assertEqual(sha256_file(path), artifact["sha256"])
        with self.assertRaises(FileExistsError):
            create_cover_art(
                self.root,
                "demo",
                1,
                source_image_path=self.root / "source" / "frame.png",
                title="新标题",
                source_authorized=True,
                source_clean_verified=True,
                identity_verified=True,
                text_verified=True,
            )

    def test_unavailable_pillow_writes_spec_not_fake_images(self) -> None:
        source = self.root / "source" / "frame.bin"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"authorized-frame-fixture")
        with mock.patch.object(publication, "Image", None), mock.patch.object(
            publication, "_PILLOW_IMPORT_ERROR", "not installed"
        ):
            report = create_cover_art(
                self.root,
                "demo",
                2,
                source_image_path=source,
                title="安全标题",
                source_authorized=True,
                source_clean_verified=True,
                identity_verified=True,
                text_verified=True,
            )
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["capability"]["install_command"][:3], ["python", "-m", "pip"])
        self.assertFalse((self.root / "deliverables" / "covers" / "demo_中文封面_16x9_v2.png").exists())


class PublicationGateTests(PublicationFixture):
    def _prepare(self) -> tuple[Path, Path, Path, Path]:
        timeline = self.write_timeline()
        materials = self.write_materials(timeline)
        covers = self.write_covers()
        ad_gate = self.root / "qa" / "ad_edit_gate.json"
        _json(ad_gate, {"schema_version": 1, "status": "pass"})
        return timeline, materials, covers, ad_gate

    def test_gate_passes_and_is_ready_only_for_human_review(self) -> None:
        timeline, materials, covers, ad_gate = self._prepare()
        gate = build_publication_package_gate(
            self.root,
            1,
            final_video_path=self.video,
            ad_edit_gate_path=ad_gate,
            final_machine_qa_path=self.machine_qa,
            chapter_timeline_path=timeline,
            publication_materials_path=materials,
            cover_art_path=covers,
        )
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["delivery_state"], "ready_for_human_review")
        self.assertTrue(gate["checks"]["all_artifact_hashes_match"])
        self.assertEqual((gate["cover_16x9"]["width"], gate["cover_16x9"]["height"]), (1920, 1080))
        self.assertEqual((gate["cover_4x3"]["width"], gate["cover_4x3"]["height"]), (1440, 1080))
        self.assertTrue(validate_gate("publication_package_gate", gate, artifact_root=self.root))

    def test_tampered_cover_closes_gate_and_cannot_be_called_deliverable(self) -> None:
        timeline, materials, covers, ad_gate = self._prepare()
        cover_payload = json.loads(covers.read_text(encoding="utf-8"))
        cover_path = self.root / cover_payload["covers"]["cover_16x9"]["path"]
        cover_path.write_bytes(cover_path.read_bytes() + b"tampered")
        gate = build_publication_package_gate(
            self.root,
            1,
            final_video_path=self.video,
            ad_edit_gate_path=ad_gate,
            final_machine_qa_path=self.machine_qa,
            chapter_timeline_path=timeline,
            publication_materials_path=materials,
            cover_art_path=covers,
        )
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["delivery_state"], "not_deliverable")
        self.assertIn("all_artifact_hashes_match", gate["failure_codes"])


if __name__ == "__main__":
    unittest.main()
