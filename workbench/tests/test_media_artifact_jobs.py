from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.workbench.api import create_workbench_router
from backend.workbench.credentials import CredentialBroker, MemoryCredentialStore
from backend.workbench.media_artifacts import build_working_master_command
from backend.workbench.settings import WorkbenchSettings


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _png_header(width: int = 640, height: int = 360) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


class MediaArtifactJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        settings = WorkbenchSettings(
            state_dir=base / "state",
            library_dir=base / "library",
            database_path=base / "state" / "workbench.sqlite3",
            ffmpeg="fixture-ffmpeg",
            ffprobe="fixture-ffprobe",
            yt_dlp="fixture-yt-dlp",
            worker_id="media-artifact-tests",
        )
        router, self.context = create_workbench_router(
            settings, CredentialBroker(MemoryCredentialStore())
        )
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        project_response = self.client.post(
            "/api/library/projects",
            json={
                "source_kind": "video_url",
                "source": "https://www.youtube.com/watch?v=media-artifact",
                "title": "Media artifact fixture",
            },
        )
        self.assertEqual(project_response.status_code, 201, project_response.text)
        self.project = project_response.json()
        run_response = self.client.post(
            f"/api/library/projects/{self.project['id']}/runs", json={}
        )
        self.assertEqual(run_response.status_code, 201, run_response.text)
        self.run = run_response.json()
        self.root = self.context.library.path_for(self.project)
        self.source = self.root / "source" / "original_master.mp4"
        self.source.write_bytes(b"independent-original-master")

    def _post_without_start(self, endpoint: str, payload: dict) -> dict:
        with mock.patch.object(self.context.runner, "enqueue"):
            response = self.client.post(
                f"/api/workflows/runs/{self.run['id']}/{endpoint}", json=payload
            )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def _fake_media_process(self, argv, *, cwd, **_kwargs):
        args = [str(value) for value in argv]
        root = Path(cwd)
        if Path(args[0]).name == "fixture-ffprobe":
            payload = {
                "format": {
                    "duration": "10.0",
                    "size": "1000",
                    "format_name": "mov,mp4",
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 640,
                        "height": 360,
                        "r_frame_rate": "30/1",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                    },
                ],
            }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        if "-c:s" in args:
            output = root / args[-1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello world\n",
                encoding="utf-8",
            )
        elif "image2" in args:
            output = root / args[-1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(_png_header())
        elif "-f" in args and args[args.index("-f") + 1] == "null":
            pass
        elif "-c" in args or "-filter_complex" in args:
            output = root / args[-1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"generated-working-master")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _frozen_source(self) -> None:
        _write_json(
            self.root / "work" / "frozen_source_v1.json",
            {
                "schema_version": 1,
                "status": "pass",
                "kind": "frozen_source_transcript",
                "source_format": "srt",
                "source_file": {"path": "work/source.srt", "sha256": "0" * 64},
                "source_language": "en",
                "stable_id_scheme": "S{ordinal:06d}",
                "slot_count": 1,
                "stable_ids": ["S000001"],
                "slots": [
                    {
                        "id": "S000001",
                        "source_text": "Hello world",
                        "start": 1.0,
                        "end": 2.0,
                        "position": 0,
                    }
                ],
            },
        )

    @staticmethod
    def _no_ads_payload() -> dict:
        return {
            "source_master_path": "source/original_master.mp4",
            "working_master_path": "work/working_master_v1.mp4",
            "source_duration": 10,
            "frame_width": 640,
            "frame_height": 360,
            "frozen_source_path": "work/frozen_source_v1.json",
            "version": 1,
            "analysis": {
                "status": "pass",
                "decision": "no_ads_detected",
                "content_scan_complete": True,
                "visual_scan_complete": True,
                "semantic_analysis_required": False,
                "semantic_analysis_complete": False,
                "analysis_method": "deterministic_full_scan",
                "decision_rule_version": "ad-evidence-v1",
                "candidates": [],
            },
        }

    def test_embedded_subtitle_api_runs_persistent_task_and_reuses_exact_artifacts(self) -> None:
        payload = {
            "source_path": "source/original_master.mp4",
            "output_path": "work/embedded_source_v1.srt",
            "stream_index": 2,
            "language": "en",
            "version": 1,
        }
        task = self._post_without_start("ingest/embedded-subtitle", payload)
        self.assertEqual(task["kind"], "media.extract_embedded_subtitle")
        with mock.patch(
            "backend.workbench.media_artifacts.subprocess.run",
            side_effect=self._fake_media_process,
        ):
            self.context.runner.run_inline(task["id"])
        completed = self.context.database.get_task(task["id"])
        self.assertEqual(completed["status"], "completed", completed)
        result = completed["result"]
        self.assertEqual(result["slot_count"], 1)
        self.assertTrue((self.root / result["subtitle"]["path"]).is_file())
        self.assertTrue((self.root / result["frozen_source"]["path"]).is_file())
        self.assertTrue((self.root / result["text_projection"]["path"]).is_file())
        self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))

        retry = self._post_without_start("ingest/embedded-subtitle", payload)
        with mock.patch(
            "backend.workbench.media_artifacts.subprocess.run",
            side_effect=AssertionError("idempotent retry must not call FFmpeg"),
        ):
            self.context.runner.run_inline(retry["id"])
        retried = self.context.database.get_task(retry["id"])
        self.assertEqual(retried["status"], "completed", retried)
        self.assertTrue(retried["result"]["reused"])

    def test_ad_edit_no_ads_remuxes_to_independent_master_and_passes_gate(self) -> None:
        self._frozen_source()
        task = self._post_without_start("ad-edit", self._no_ads_payload())
        calls: list[list[str]] = []

        def fake(argv, **kwargs):
            calls.append([str(value) for value in argv])
            return self._fake_media_process(argv, **kwargs)

        with mock.patch(
            "backend.workbench.media_artifacts.subprocess.run", side_effect=fake
        ):
            self.context.runner.run_inline(task["id"])
        completed = self.context.database.get_task(task["id"])
        self.assertEqual(completed["status"], "completed", completed)
        result = completed["result"]
        working = self.root / result["working_master"]["path"]
        self.assertTrue(working.is_file())
        self.assertNotEqual(working.resolve(), self.source.resolve())
        self.assertFalse(self.source.samefile(working))
        remux = next(call for call in calls if "-c" in call and call[-1].endswith(".mp4"))
        self.assertEqual(remux[remux.index("-c") + 1], "copy")
        self.assertTrue((self.root / "qa" / "ad_edit_gate.json").is_file())
        self.assertTrue((self.root / "qa" / "source_to_edit_timeline.json").is_file())
        mapped = json.loads(
            (self.root / "work" / "frozen_source_working_v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(mapped["based_on_working_master"])

    def test_ad_edit_ffmpeg_failure_is_persisted_and_leaves_no_false_gate(self) -> None:
        self._frozen_source()
        task = self._post_without_start("ad-edit", self._no_ads_payload())

        def fail(argv, *, cwd, **_kwargs):
            args = [str(value) for value in argv]
            if Path(args[0]).name == "fixture-ffmpeg" and args[-1].endswith(".mp4"):
                (Path(cwd) / args[-1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(args, 9, stdout="", stderr="private path")
            return self._fake_media_process(argv, cwd=cwd)

        with mock.patch(
            "backend.workbench.media_artifacts.subprocess.run", side_effect=fail
        ):
            self.context.runner.run_inline(task["id"])
        failed = self.context.database.get_task(task["id"])
        self.assertEqual(failed["status"], "repair_required", failed)
        self.assertIn("非零退出码 9", failed["error"]["message"])
        self.assertNotIn("private path", json.dumps(failed, ensure_ascii=False))
        self.assertFalse((self.root / "work" / "working_master_v1.mp4").exists())
        self.assertFalse((self.root / "qa" / "ad_edit_gate.json").exists())

    def test_cover_source_is_extracted_only_from_gate_bound_working_master(self) -> None:
        self._frozen_source()
        ad_task = self._post_without_start("ad-edit", self._no_ads_payload())
        with mock.patch(
            "backend.workbench.media_artifacts.subprocess.run",
            side_effect=self._fake_media_process,
        ):
            self.context.runner.run_inline(ad_task["id"])
        self.assertEqual(
            self.context.database.get_task(ad_task["id"])["status"], "completed"
        )

        cover_task = self._post_without_start(
            "cover-source",
            {
                "working_master_path": "work/working_master_v1.mp4",
                "at_seconds": 3.25,
                "output_path": "work/cover_source_v1.png",
                "version": 1,
            },
        )
        with mock.patch(
            "backend.workbench.media_artifacts.subprocess.run",
            side_effect=self._fake_media_process,
        ):
            self.context.runner.run_inline(cover_task["id"])
        completed = self.context.database.get_task(cover_task["id"])
        self.assertEqual(completed["status"], "completed", completed)
        cover = completed["result"]["cover_source"]
        self.assertEqual((cover["width"], cover["height"]), (640, 360))
        self.assertTrue((self.root / cover["path"]).is_file())

        wrong = self.root / "work" / "unbound.mp4"
        wrong.write_bytes(b"not-the-formal-working-master")
        rejected = self._post_without_start(
            "cover-source",
            {
                "working_master_path": "work/unbound.mp4",
                "at_seconds": 1,
                "version": 2,
            },
        )
        self.context.runner.run_inline(rejected["id"])
        rejected_task = self.context.database.get_task(rejected["id"])
        self.assertEqual(rejected_task["status"], "repair_required")
        self.assertFalse((self.root / "work" / "cover_source_v2.png").exists())

    def test_project_relative_path_escape_fails_in_worker_without_writing_outside(self) -> None:
        outside = Path(self.temporary.name) / "escaped.srt"
        task = self._post_without_start(
            "ingest/embedded-subtitle",
            {
                "source_path": "source/original_master.mp4",
                "output_path": "../../escaped.srt",
                "stream_index": 2,
                "version": 1,
            },
        )
        self.context.runner.run_inline(task["id"])
        failed = self.context.database.get_task(task["id"])
        self.assertEqual(failed["status"], "repair_required")
        self.assertFalse(outside.exists())

    def test_remove_command_uses_synchronized_video_audio_trim_and_concat(self) -> None:
        command = build_working_master_command(
            "source/original.mp4",
            "work/working.mp4",
            {
                "identity": False,
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
                        "source_end": 4,
                        "working_anchor": 2,
                    },
                    {
                        "kind": "keep",
                        "source_start": 4,
                        "source_end": 10,
                        "working_start": 2,
                        "working_end": 8,
                    },
                ],
            },
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("trim=start=0.000000:end=2.000000", graph)
        self.assertIn("atrim=start=4.000000:end=10.000000", graph)
        self.assertIn("concat=n=2:v=1:a=1", graph)
        self.assertNotIn("atempo", graph)


if __name__ == "__main__":
    unittest.main()
