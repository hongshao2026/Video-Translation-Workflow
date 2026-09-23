from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.portability.manifest import export_project, import_project
from backend.workbench.api import create_workbench_router
from backend.workbench.credentials import CredentialBroker, MemoryCredentialStore
from backend.workbench.database import WorkbenchDatabase
from backend.workbench.library import ProjectLibrary
from backend.workbench.settings import WorkbenchSettings
from backend.workbench.workflows import WorkflowService


class ColdMigrationRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app_root = Path(__file__).resolve().parents[1]

    def _settings(self, name: str) -> WorkbenchSettings:
        root = self.root / name
        return WorkbenchSettings(
            state_dir=root / "state",
            library_dir=root / "library",
            database_path=root / "state" / "workbench.sqlite3",
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            yt_dlp="yt-dlp",
            worker_id=f"worker-{name}",
        )

    def _services(self, name: str) -> tuple[WorkbenchDatabase, ProjectLibrary, WorkflowService]:
        settings = self._settings(name)
        settings.ensure_directories()
        database = WorkbenchDatabase(settings.database_path)
        database.initialize()
        library = ProjectLibrary(database, settings)
        workflows = WorkflowService(database, library, self.app_root)
        workflows.seed()
        return database, library, workflows

    def _frozen_url_project(self) -> tuple[WorkbenchDatabase, ProjectLibrary, dict, dict]:
        database, library, workflows = self._services("source")
        project = library.create(
            source_kind="video_url",
            source="https://www.youtube.com/watch?v=portable123&list=public",
            title="Portable fixture",
        )
        database.upsert_provider_profile(
            {
                "id": "provider-minimax-portable",
                "service_kind": "llm",
                "provider_id": "minimax-llm",
                "display_name": "MiniMax portable",
                "base_url": "https://api.minimax.cn/v1",
                "model": "MiniMax-M2.5",
                "credential_ref": "keyring://old-device-only",
                "config": {"structured_output_mode": "prompt_json"},
                "capability": {},
            }
        )
        run = workflows.create_run(
            project["id"],
            role_bindings={
                "T": "provider-minimax-portable",
                "A": "provider-minimax-portable",
                "B": "provider-minimax-portable",
                "C": "provider-minimax-portable",
            },
        )
        return database, library, project, run

    def _transfer(self, library: ProjectLibrary, project: dict, target_name: str) -> Path:
        archive = self.root / f"{target_name}.zip"
        export_project(library.path_for(project), archive)
        destination = self._settings("target").library_dir / target_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        import_project(archive, destination)
        return destination

    def test_public_url_snapshot_restores_same_run_without_live_state_or_credentials(self) -> None:
        _, source_library, project, source_run = self._frozen_url_project()
        source_root = source_library.path_for(project)
        manifest_text = (source_root / "project.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["source_reference"]["mode"], "public_url")
        self.assertEqual(manifest["workflow"]["run_id"], source_run["id"])
        self.assertNotIn("old-device-only", manifest_text)

        destination = self._transfer(source_library, project, "restored_run")
        target_database, target_library, _ = self._services("target")
        result = target_library.restore_existing(destination)
        restored = result["project"]
        restored_run = restored["runs"][0]

        self.assertEqual(restored["id"], project["id"])
        self.assertEqual(restored_run["id"], source_run["id"])
        self.assertEqual(restored_run["provider_lock"], source_run["provider_lock"])
        self.assertEqual(restored_run["prompt_lock"], source_run["prompt_lock"])
        self.assertEqual(len(target_database.get_run(source_run["id"])["stages"]), 8)
        self.assertEqual(target_database.list_tasks(), [])
        self.assertEqual(target_database.list_provider_requests(), [])
        self.assertEqual(target_database.fetch_all("SELECT * FROM worker_leases"), [])
        restored_profile = target_database.get_provider_profile("provider-minimax-portable")
        self.assertIsNotNone(restored_profile)
        self.assertIsNone(restored_profile["credential_ref"])
        self.assertEqual(
            result["restore"]["credential_rebind_profile_ids"],
            ["provider-minimax-portable"],
        )
        self.assertFalse(result["restore"]["active_tasks_restored"])
        self.assertFalse(result["restore"]["provider_request_ledger_restored"])

    def test_local_sources_are_relative_or_explicitly_require_rebind(self) -> None:
        _database, library, workflows = self._services("source")
        external = self.root / "private-device" / "camera.mp4"
        external.parent.mkdir()
        external.write_bytes(b"portable-media")

        copied = library.create(
            source_kind="local_file",
            source=str(external),
            copy_local_file=True,
        )
        workflows.create_run(copied["id"])
        copied_manifest_text = (
            library.path_for(copied) / "project.json"
        ).read_text(encoding="utf-8")
        copied_manifest = json.loads(copied_manifest_text)
        self.assertEqual(
            copied_manifest["source_reference"]["path"], "source/original.mp4"
        )
        self.assertNotIn(str(external), copied_manifest_text)

        unbound = library.create(
            source_kind="local_file",
            source=str(external),
            copy_local_file=False,
        )
        workflows.create_run(unbound["id"])
        unbound_text = (library.path_for(unbound) / "project.json").read_text(
            encoding="utf-8"
        )
        unbound_manifest = json.loads(unbound_text)
        self.assertEqual(unbound_manifest["source_reference"]["mode"], "rebind_required")
        self.assertNotIn(str(external), unbound_text)

        # Default export intentionally omits the copied media.  Attachment is
        # still safe and records a mandatory source rebind rather than inventing
        # a source or trusting the old absolute path.
        destination = self._transfer(library, copied, "copied_without_media")
        target_database, target_library, _ = self._services("target")
        result = target_library.restore_existing(destination)
        restored = target_database.get_project(copied["id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored["source"], "")
        self.assertTrue(result["restore"]["source_rebind_required"])

    def test_temporary_or_credentialed_video_url_is_never_written_to_project_json(self) -> None:
        _, library, _ = self._services("source")
        unsafe = (
            "https://r1---sn.example.googlevideo.com/videoplayback?"
            "expire=999999&signature=TOP-SECRET"
        )
        with self.assertRaisesRegex(ValueError, "临时媒体直链|签名"):
            library.create(source_kind="video_url", source=unsafe)
        self.assertEqual(library.database.list_projects(), [])

    def test_attach_rejects_outside_duplicate_link_and_tampered_lock(self) -> None:
        _, source_library, project, _ = self._frozen_url_project()
        destination = self._transfer(source_library, project, "tamper_me")
        target_database, target_library, _ = self._services("target")

        with self.assertRaisesRegex(ValueError, "资料库根目录"):
            target_library.restore_existing(source_library.path_for(project))

        lock_path = destination / "qa" / "workflow_lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["next_gate"] = "tampered"
        lock_path.write_text(json.dumps(lock, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "哈希绑定"):
            target_library.restore_existing(destination)
        self.assertIsNone(target_database.get_project(project["id"]))

        # Restore the bound file and attach once; a second registration cannot
        # silently alias either the id or directory.
        source_lock = source_library.path_for(project) / "qa" / "workflow_lock.json"
        lock_path.write_bytes(source_lock.read_bytes())
        target_library.restore_existing(destination)
        with self.assertRaisesRegex(ValueError, "已在资料库"):
            target_library.restore_existing(destination)

        link = self._settings("target").library_dir / "linked-project"
        try:
            link.symlink_to(destination, target_is_directory=True)
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, "链接|联接"):
            target_library.restore_existing(link)

    def test_api_attach_and_source_rebind_make_import_visible(self) -> None:
        _, source_library, project, source_run = self._frozen_url_project()
        destination = self._transfer(source_library, project, "api_import")
        settings = self._settings("target")
        router, context = create_workbench_router(
            settings, CredentialBroker(MemoryCredentialStore())
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        attached = client.post(
            "/api/library/projects/attach", json={"library_path": destination.name}
        )
        self.assertEqual(attached.status_code, 201, attached.text)
        listed = client.get("/api/library/projects").json()
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["projects"][0]["id"], project["id"])
        self.assertEqual(
            context.database.list_runs(project["id"])[0]["id"], source_run["id"]
        )

        rebound = client.post(
            f"/api/library/projects/{project['id']}/source-binding",
            json={
                "source_kind": "video_url",
                "source": "https://www.youtube.com/watch?v=rebound456",
            },
        )
        self.assertEqual(rebound.status_code, 200, rebound.text)
        self.assertTrue(rebound.json()["metadata"]["source_bound"])

    def test_project_relative_media_hash_mismatch_fails_closed(self) -> None:
        _, source_library, workflows = self._services("source")
        external = self.root / "input.mp4"
        external.write_bytes(b"original")
        project = source_library.create(
            source_kind="local_file", source=str(external), copy_local_file=True
        )
        workflows.create_run(project["id"])
        archive = self.root / "with-media.zip"
        export_project(source_library.path_for(project), archive, include_media=True)
        destination = self._settings("target").library_dir / "hash-mismatch"
        destination.parent.mkdir(parents=True, exist_ok=True)
        import_project(archive, destination)
        media = destination / "source" / "original.mp4"
        media.write_bytes(b"modified")
        _, target_library, _ = self._services("target")
        with self.assertRaisesRegex(ValueError, "哈希绑定"):
            target_library.restore_existing(destination)


if __name__ == "__main__":
    unittest.main()
