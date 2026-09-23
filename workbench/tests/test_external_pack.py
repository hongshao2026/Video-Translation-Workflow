from __future__ import annotations

import io
import json
import math
import shutil
import struct
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.timeline_audio import prepare_natural_timing, timing_summary

SAMPLE_URL = f"https://www.youtube.com/watch?v={app_module.PROJECT_ID}"
SEGMENT_COUNT = 2


def fixture_manifest(_translation_path, role_names=None):
    names = role_names or {}
    return {
        "schema_version": 1,
        "project_id": app_module.PROJECT_ID,
        "clip_start_seconds": 0,
        "clip_end_seconds": 2,
        "clip_duration_seconds": 2,
        "audio_naming": "segment_0001.wav",
        "accepted_audio": ["wav"],
        "segments": [
            {
                "segment_id": "segment_0001",
                "source_id": "fixture.1",
                "filename": "segment_0001.wav",
                "role_id": "host",
                "role_name": names.get("host", "Host"),
                "start_time": "00:00:00.000",
                "end_time": "00:00:01.000",
                "start_seconds": 0,
                "end_seconds": 1,
                "slot_seconds": 1,
                "text": "测试一。",
                "subtitle_text": "测试一。",
                "tts_text_mode": "approved_translation",
            },
            {
                "segment_id": "segment_0002",
                "source_id": "fixture.2",
                "filename": "segment_0002.wav",
                "role_id": "guest",
                "role_name": names.get("guest", "Guest"),
                "start_time": "00:00:01.000",
                "end_time": "00:00:02.000",
                "start_seconds": 1,
                "end_seconds": 2,
                "slot_seconds": 1,
                "text": "测试二。",
                "subtitle_text": "测试二。",
                "tts_text_mode": "approved_translation",
            },
        ],
    }


def fixture_normalize(source: Path, destination: Path) -> float:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return 0.18


def tone_wav(seconds: float = 0.18, frequency: float = 220.0) -> bytes:
    sample_rate = 24_000
    frames = bytearray()
    for index in range(round(sample_rate * seconds)):
        value = round(math.sin(index / sample_rate * frequency * math.tau) * 8_000)
        frames.extend(struct.pack("<h", value))
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return output.getvalue()


class ExternalPackApiTests(unittest.TestCase):
    def test_natural_timing_trims_only_tail_and_never_stretches_audio(self) -> None:
        sample_rate = 24_000
        voiced = np.sin(
            np.arange(sample_rate * 3, dtype=np.float32) / sample_rate * 220 * math.tau
        ) * 0.25
        source = np.concatenate([voiced, np.zeros(round(sample_rate * 0.3), dtype=np.float32)])
        timing = prepare_natural_timing(
            source,
            sample_rate,
            window_seconds=3.18,
            available_until_next_seconds=3.24,
        )
        self.assertEqual(timing.raw_seconds, 3.3)
        self.assertLessEqual(timing.rendered_seconds, 3.18)
        self.assertEqual(timing.overflow_seconds, 0)
        self.assertEqual(timing.overlap_seconds, 0)
        np.testing.assert_array_equal(timing.audio[: len(voiced)], voiced)
        summary = timing_summary(
            [{"overflow_seconds": timing.overflow_seconds, "overlap_seconds": 0}]
        )
        self.assertEqual(summary["timing_policy"], "natural_no_stretch")
        self.assertEqual(summary["max_stretch_rate"], 1.0)

    def test_template_and_complete_audio_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_module, "EXTERNAL_PACK_DIR", Path(temp_dir)
        ), patch.object(app_module, "build_external_manifest", side_effect=fixture_manifest), patch.object(
            app_module, "normalize_audio", side_effect=fixture_normalize
        ):
            client = TestClient(app_module.app)
            template = client.post(
                "/api/external-packs/template",
                json={
                    "url": SAMPLE_URL,
                    "role_names": {"host": "主持人", "guest": "嘉宾"},
                },
            )
            self.assertEqual(template.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(template.content)) as archive:
                self.assertIn("manifest.csv", archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(len(manifest["segments"]), SEGMENT_COUNT)
                self.assertEqual(manifest["segments"][0]["filename"], "segment_0001.wav")
                expected_lines = [
                    ("fixture.1", "host", "测试一。"),
                    ("fixture.2", "guest", "测试二。"),
                ]
                for source_id, role_id, approved_text in expected_lines:
                    line = next(row for row in manifest["segments"] if row["source_id"] == source_id)
                    self.assertEqual(line["role_id"], role_id)
                    self.assertEqual(line["text"], approved_text)

            files = [
                (
                    "files",
                    (
                        f"segment_{index:04d}.wav",
                        tone_wav(15.0 if index == 1 else 0.18),
                        "audio/wav",
                    ),
                )
                for index in range(1, SEGMENT_COUNT + 1)
            ]
            uploaded = client.post(
                "/api/external-packs",
                data={
                    "url": SAMPLE_URL,
                    "role_names": json.dumps(
                        {"host": "主持人", "guest": "嘉宾"}, ensure_ascii=False
                    ),
                },
                files=files,
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            report = uploaded.json()
            self.assertTrue(report["ready"])
            self.assertEqual(report["ready_count"], SEGMENT_COUNT)
            self.assertEqual(report["missing"], [])
            self.assertEqual(report["warning_count"], 1)
            self.assertEqual(report["segments"][0]["status"], "long")
            self.assertTrue(all(row["audio_seconds"] for row in report["segments"]))

    def test_partial_pack_reports_missing_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_module, "EXTERNAL_PACK_DIR", Path(temp_dir)
        ), patch.object(app_module, "build_external_manifest", side_effect=fixture_manifest), patch.object(
            app_module, "normalize_audio", side_effect=fixture_normalize
        ):
            client = TestClient(app_module.app)
            uploaded = client.post(
                "/api/external-packs",
                data={"url": SAMPLE_URL, "role_names": "{}"},
                files=[("files", ("segment_0001.wav", tone_wav(), "audio/wav"))],
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            report = uploaded.json()
            self.assertFalse(report["ready"])
            self.assertEqual(report["ready_count"], 1)
            self.assertEqual(len(report["missing"]), SEGMENT_COUNT - 1)

            supplemented = client.post(
                "/api/external-packs",
                data={
                    "url": SAMPLE_URL,
                    "role_names": "{}",
                    "pack_id": report["pack_id"],
                },
                files=[
                    (
                        "files",
                        (f"segment_{index:04d}.wav", tone_wav(), "audio/wav"),
                    )
                    for index in range(2, SEGMENT_COUNT + 1)
                ],
            )
            self.assertEqual(supplemented.status_code, 200, supplemented.text)
            completed = supplemented.json()
            self.assertTrue(completed["ready"])
            self.assertEqual(completed["ready_count"], SEGMENT_COUNT)


if __name__ == "__main__":
    unittest.main()
