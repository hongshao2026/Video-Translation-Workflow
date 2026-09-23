from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.workbench.credentials import CredentialBroker, MemoryCredentialStore
from backend.workbench.database import WorkbenchDatabase
from backend.workbench.library import ProjectLibrary
from backend.workbench.production_jobs import ProductionJobError, ProductionJobs
from backend.workbench.settings import WorkbenchSettings
from backend.workbench.workflows import WorkflowService


class WorkflowFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = WorkbenchSettings(
            state_dir=root / "state",
            library_dir=root / "library",
            database_path=root / "state" / "workbench.sqlite3",
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            yt_dlp="yt-dlp",
            worker_id="fixture",
        )
        self.settings.ensure_directories()
        self.database = WorkbenchDatabase(self.settings.database_path)
        self.database.initialize()
        self.library = ProjectLibrary(self.database, self.settings)
        self.credentials = CredentialBroker(MemoryCredentialStore())
        self.workflows = WorkflowService(
            self.database,
            self.library,
            Path(__file__).resolve().parents[1],
        )
        self.workflows.seed()
        self.project = self.library.create(
            source_kind="video_url",
            source="https://example.test/video",
            title="Fixture",
        )
        reference = self.credentials.save("llm-freeze", "fixture-secret")
        self.profile = self.database.upsert_provider_profile(
            {
                "id": "llm-freeze",
                "service_kind": "llm",
                "provider_id": "openai-compatible",
                "display_name": "Fixture LLM",
                "base_url": "https://example.test/v1",
                "model": "fixture-model",
                "credential_ref": reference,
                "config": {"structured_output_mode": "prompt_json"},
                "capability": {"structured_outputs": True},
            }
        )

    def test_run_freezes_prompt_artifact_and_provider_options(self) -> None:
        run = self.workflows.create_run(
            self.project["id"],
            role_bindings={role: self.profile["id"] for role in "TABC"},
        )
        root = self.library.path_for(self.project)
        artifact = run["prompt_lock"]["artifact"]
        prompt_path = root / artifact["path"]
        self.assertTrue(prompt_path.is_file())

        jobs = ProductionJobs(self.database, self.library, self.credentials)
        provider = jobs._provider(run, self.profile["id"], "llm")
        self.assertEqual(
            dict(provider.profile.options), {"structured_output_mode": "prompt_json"}
        )

        changed = {
            **self.profile,
            "config": {"structured_output_mode": "none", "chat_path": "/other"},
            "capability": self.profile["capability"],
        }
        self.database.upsert_provider_profile(changed)
        with self.assertRaisesRegex(ProductionJobError, "参数已变化"):
            jobs._provider(run, self.profile["id"], "llm")

        changed["config"] = self.profile["config"]
        changed["capability"] = {"structured_outputs": False}
        self.database.upsert_provider_profile(changed)
        with self.assertRaisesRegex(ProductionJobError, "能力记录已变化"):
            jobs._provider(run, self.profile["id"], "llm")

    def test_frozen_prompt_artifact_tampering_is_detected(self) -> None:
        run = self.workflows.create_run(self.project["id"])
        root = self.library.path_for(self.project)
        artifact = run["prompt_lock"]["artifact"]
        path = root / artifact["path"]
        path.write_text("{}", encoding="utf-8")
        jobs = ProductionJobs(self.database, self.library, self.credentials)
        with self.assertRaisesRegex(ProductionJobError, "哈希"):
            jobs._bound_file(root, artifact, "冻结提示词包")


if __name__ == "__main__":
    unittest.main()
