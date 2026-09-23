from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.workbench.database import WorkbenchDatabase
from backend.workbench.media import select_formats, select_subtitle_track
from backend.workbench.runner import LocalTaskRunner, UncertainPaidRequest


class MediaSelectionTests(unittest.TestCase):
    def test_prefers_resolution_then_compatible_video_and_original_language(self) -> None:
        info = {
            "formats": [
                {"format_id": "v720", "vcodec": "avc1.4d", "acodec": "none", "ext": "mp4", "height": 720, "fps": 30, "tbr": 1200},
                {"format_id": "v1080-vp9", "vcodec": "vp9", "acodec": "none", "ext": "webm", "height": 1080, "fps": 30, "tbr": 2200},
                {"format_id": "v1080-h264", "vcodec": "avc1.640", "acodec": "none", "ext": "mp4", "height": 1080, "fps": 30, "tbr": 1800},
                {"format_id": "a-en", "vcodec": "none", "acodec": "opus", "ext": "webm", "language": "en", "abr": 160},
                {"format_id": "a-es", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "language": "es", "is_original": True, "abr": 128},
            ]
        }
        selected = select_formats(info, "es")
        self.assertEqual(selected["video"]["format_id"], "v1080-h264")
        self.assertEqual(selected["audio"]["format_id"], "a-es")
        self.assertTrue(selected["source_language_verified"])
        self.assertFalse(selected["manual_approval_required"])

    def test_subtitle_selection_prefers_manual_source_language_and_never_keeps_url(self) -> None:
        selected = select_subtitle_track(
            {
                "language": "es",
                "subtitles": {
                    "es": [
                        {
                            "ext": "vtt",
                            "url": "https://temporary.invalid/signed?token=secret",
                        },
                        {"ext": "srt", "url": "https://temporary.invalid/other"},
                    ]
                },
                "automatic_captions": {"es": [{"ext": "vtt", "url": "https://auto"}]},
            },
            "es",
        )
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["kind"], "manual")
        self.assertEqual(selected["language"], "es")
        self.assertEqual(selected["available_formats"], ["vtt", "srt"])
        self.assertNotIn("url", str(selected).lower())

    def test_ambiguous_or_missing_source_language_requires_import_or_device_asr(self) -> None:
        result = select_subtitle_track(
            {
                "subtitles": {"en": [{"ext": "vtt"}], "es": [{"ext": "vtt"}]},
                "automatic_captions": {},
            },
            None,
        )
        self.assertEqual(result["status"], "needs_import_or_device_asr")
        self.assertIn("ambiguous", result["reason"])


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = WorkbenchDatabase(Path(self.temp.name) / "test.sqlite3")
        self.database.initialize()
        self.database.seed_workflow_preset(
            preset_id="fixture",
            name="Fixture",
            version=1,
            description="fixture",
            locked=True,
            prompt_pack={},
            prompt_pack_sha256="0" * 64,
        )
        self.project = self.database.create_project(
            {
                "title": "Fixture",
                "source_kind": "video_url",
                "source": "https://example.test/video",
                "library_path": "fixture_run",
            }
        )
        self.run = self.database.create_run(
            project_id=self.project["id"],
            preset_id="fixture",
            stages=[{"key": "01", "title": "Fixture"}],
            provider_lock={},
            prompt_lock={},
        )

    def task(self, kind: str, payload: dict | None = None) -> dict:
        return self.database.create_task(
            run_id=self.run["id"],
            stage_key="01",
            kind=kind,
            detail="queued",
            input_payload=payload or {},
        )

    def test_exact_progress_is_committed_before_completion(self) -> None:
        runner = LocalTaskRunner(self.database, max_workers=1)

        def handler(payload, progress, token):
            self.assertEqual(payload["count"], 2)
            progress.exact(1, 2, "items", "one")
            token.checkpoint()
            progress.exact(2, 2, "items", "two")
            return {"detail": "done", "verified": True}

        runner.register("fixture", handler)
        task = self.task("fixture", {"count": 2})
        runner.run_inline(task["id"])
        result = self.database.get_task(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress_current"], 2)
        self.assertTrue(result["result"]["verified"])

    def test_uncertain_paid_request_is_never_turned_into_retry(self) -> None:
        runner = LocalTaskRunner(self.database, max_workers=1)

        def handler(_payload, _progress, _token):
            raise UncertainPaidRequest("response unknown", request_id="trace-safe")

        runner.register("provider.speech.synthesize", handler)
        task = self.task("provider.speech.synthesize")
        runner.run_inline(task["id"])
        result = self.database.get_task(task["id"])
        self.assertEqual(result["status"], "blocked_uncertain")
        self.assertFalse(result["error"]["automatic_retry"])
        self.assertEqual(result["error"]["request_id"], "trace-safe")

    def test_restart_marks_local_and_paid_tasks_differently(self) -> None:
        local = self.task("media.download")
        paid = self.task("provider.llm.generate")
        self.database.update_task(local["id"], status="running")
        self.database.update_task(paid["id"], status="running")
        self.database.reserve_provider_request(
            task_id=paid["id"],
            profile_id=None,
            provider_id="fixture-provider",
            requested_model="fixture-model",
            idempotency_key="fixture-interrupted-request",
            input_sha256="a" * 64,
        )
        runner = LocalTaskRunner(self.database, max_workers=1)
        self.assertEqual(runner.recover_interrupted(), 2)
        self.assertEqual(self.database.get_task(local["id"])["status"], "repair_required")
        self.assertEqual(self.database.get_task(paid["id"])["status"], "blocked_uncertain")

    def test_restart_allows_paid_task_recovery_when_ledger_has_no_unresolved_request(self) -> None:
        paid = self.task("provider.llm.generate")
        self.database.update_task(paid["id"], status="running")
        request = self.database.reserve_provider_request(
            task_id=paid["id"],
            profile_id=None,
            provider_id="fixture-provider",
            requested_model="fixture-model",
            idempotency_key="fixture-completed-request",
            input_sha256="b" * 64,
        )
        self.database.update_provider_request(
            request["id"],
            status="completed",
            billing_state="settled",
            result={},
        )
        runner = LocalTaskRunner(self.database, max_workers=1)
        self.assertEqual(runner.recover_interrupted(), 1)
        recovered = self.database.get_task(paid["id"])
        self.assertEqual(recovered["status"], "repair_required")
        self.assertEqual(recovered["error_code"], "interrupted_recoverable_task")

    def test_second_worker_waits_until_run_lease_is_released(self) -> None:
        first = self.database.acquire_lease(self.run["id"], "worker-a", ttl_seconds=30)
        with self.assertRaises(RuntimeError):
            self.database.acquire_lease(self.run["id"], "worker-b", ttl_seconds=30)
        self.assertTrue(self.database.release_lease(self.run["id"], first["lease_token"]))
        second = self.database.acquire_lease(self.run["id"], "worker-b", ttl_seconds=30)
        self.assertEqual(second["worker_id"], "worker-b")

    def test_same_worker_id_cannot_replace_an_active_lease_token(self) -> None:
        first = self.database.acquire_lease(self.run["id"], "worker-a", ttl_seconds=30)
        with self.assertRaises(RuntimeError):
            self.database.acquire_lease(self.run["id"], "worker-a", ttl_seconds=30)
        self.assertTrue(self.database.release_lease(self.run["id"], first["lease_token"]))

    def test_missing_handler_does_not_take_run_lease(self) -> None:
        runner = LocalTaskRunner(self.database, max_workers=1, worker_id="worker-a")
        task = self.task("missing-handler")
        runner.run_inline(task["id"])
        result = self.database.get_task(task["id"])
        self.assertEqual(result["status"], "repair_required")
        lease = self.database.acquire_lease(self.run["id"], "worker-b", ttl_seconds=30)
        self.assertEqual(lease["worker_id"], "worker-b")


if __name__ == "__main__":
    unittest.main()
