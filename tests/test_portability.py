from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.portability.environment import run_diagnostics, write_rebind_receipt
from backend.portability.manifest import (
    MANIFEST_NAME,
    PortabilityError,
    export_project,
    import_project,
    verify_archive,
)


class ProjectTransferTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "demo_run"
        (project / "qa").mkdir(parents=True)
        (project / "source").mkdir()
        (project / "runtime").mkdir()
        (project / "credentials").mkdir()
        (project / "PROJECT.md").write_text("# Demo\n", encoding="utf-8")
        (project / "qa" / "translation_gate.json").write_text(
            '{"status":"pass"}\n', encoding="utf-8"
        )
        (project / "source" / "video.mp4").write_bytes(b"fake-video")
        (project / "source" / "analysis.npy").write_bytes(b"fake-array")
        (project / "runtime" / "active-job.json").write_text("{}", encoding="utf-8")
        (project / "credentials" / "account.json").write_text(
            '{"value":"hidden"}', encoding="utf-8"
        )
        (project / "minimax_api.txt").write_text(
            "must-never-be-opened-or-exported", encoding="utf-8"
        )
        (project / ".env").write_text("MINIMAX_API_KEY=hidden", encoding="utf-8")
        (project / "workbench_config_v1.json").write_text("{}", encoding="utf-8")
        return project

    def test_default_export_is_relative_verified_and_excludes_sensitive_or_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            archive = root / "project.zip"

            result = export_project(project, archive)
            self.assertEqual(result["file_count"], 2)
            self.assertFalse(result["media_included"])
            self.assertEqual(result["excluded_counts"]["secret_or_credential"], 3)
            self.assertEqual(result["excluded_counts"]["device_binding"], 1)
            self.assertEqual(result["excluded_counts"]["media"], 2)

            verified = verify_archive(archive)
            self.assertEqual(verified["file_count"], 2)
            with zipfile.ZipFile(archive) as package:
                manifest = json.loads(package.read(MANIFEST_NAME))
                paths = [entry["path"] for entry in manifest["files"]]
                self.assertEqual(paths, ["PROJECT.md", "qa/translation_gate.json"])
                self.assertTrue(all(not Path(path).is_absolute() for path in paths))
                member_names = package.namelist()
                self.assertNotIn("project/source/video.mp4", member_names)
                self.assertNotIn("project/source/analysis.npy", member_names)
                self.assertFalse(any("key" in name.casefold() for name in member_names))
                self.assertFalse(any(".env" in name.casefold() for name in member_names))

    def test_media_is_opt_in_but_credentials_remain_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            archive = root / "project-with-media.zip"
            result = export_project(project, archive, include_media=True)
            self.assertTrue(result["media_included"])
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                self.assertIn("project/source/video.mp4", names)
                self.assertIn("project/source/analysis.npy", names)
                self.assertFalse(any("api_key" in name.casefold() for name in names))

    def test_import_round_trip_is_atomic_and_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            archive = root / "project.zip"
            destination = root / "restored"
            export_project(project, archive)

            result = import_project(archive, destination)
            self.assertTrue(result["rebind_required"])
            self.assertEqual((destination / "PROJECT.md").read_text(encoding="utf-8"), "# Demo\n")
            self.assertFalse((destination / "source" / "video.mp4").exists())
            receipt = json.loads(
                (destination / ".dub-workbench" / "import-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(receipt["verified"])
            self.assertTrue(receipt["rebind_required"])
            self.assertNotIn(str(project), json.dumps(receipt))

    def test_tampered_member_is_rejected_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "project.zip"
            tampered = root / "tampered.zip"
            export_project(self._project(root), archive)
            with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(
                tampered, "w", compression=zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    content = source.read(info)
                    if info.filename == "project/PROJECT.md":
                        content = b"tampered"
                    target.writestr(info.filename, content)

            destination = root / "must-not-exist"
            with self.assertRaisesRegex(PortabilityError, "SHA-256"):
                import_project(tampered, destination)
            self.assertFalse(destination.exists())

    def test_traversal_path_is_rejected(self) -> None:
        manifest = {
            "schema_version": "dub-workbench-project-transfer/v1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "project_name": "unsafe",
            "options": {"media_included": False},
            "summary": {"file_count": 1, "total_bytes": 0, "excluded_counts": {}},
            "files": [
                {
                    "path": "../outside.txt",
                    "size": 0,
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "media": False,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(MANIFEST_NAME, json.dumps(manifest))
                package.writestr("project/../outside.txt", b"")
            with self.assertRaises(PortabilityError):
                verify_archive(archive)

    def test_nonempty_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "project.zip"
            destination = root / "existing"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            export_project(self._project(root), archive)
            with self.assertRaisesRegex(PortabilityError, "absent or empty"):
                import_project(archive, destination)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_symlink_cannot_pull_an_external_file_into_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            external = root / "outside.txt"
            external.write_text("outside", encoding="utf-8")
            link = project / "linked-outside.txt"
            try:
                link.symlink_to(external)
            except OSError:
                self.skipTest("This Windows account cannot create symbolic links.")
            archive = root / "project.zip"
            result = export_project(project, archive)
            self.assertEqual(result["excluded_counts"]["symlink_or_reparse_point"], 1)
            with zipfile.ZipFile(archive) as package:
                self.assertNotIn("project/linked-outside.txt", package.namelist())


class EnvironmentDiagnosticTests(unittest.TestCase):
    def test_report_never_contains_key_values_or_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_file = root / "artifact.json"
            project_file.write_text("{}", encoding="utf-8")
            config = root / "project-config.json"
            config.write_text(
                json.dumps(
                    {
                        "source_video": str(project_file),
                        "background_audio": str(project_file),
                        "translation": str(project_file),
                        "translation_gate": str(project_file),
                        "audition_items": str(project_file),
                    }
                ),
                encoding="utf-8",
            )
            hidden_key = "unit-test-key-that-must-stay-redacted"
            with patch.dict(
                os.environ,
                {
                    "DUB_PROJECT_CONFIG": str(config),
                    "MINIMAX_API_KEY": hidden_key,
                },
                clear=False,
            ):
                report = run_diagnostics(
                    root, require_project=True, required_provider="minimax"
                )
            serialized = json.dumps(report)
            self.assertNotIn(hidden_key, serialized)
            self.assertNotIn(str(root), serialized)
            provider = next(
                row for row in report["checks"] if row["id"] == "provider.minimax"
            )
            self.assertEqual(provider["status"], "pass")
            self.assertFalse(report["credential_values_included"])
            self.assertFalse(report["filesystem_paths_included"])

    def test_rebind_receipt_is_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            report = {
                "schema_version": "dub-workbench-environment-diagnostic/v1",
                "status": "warn",
                "required_failures": 0,
                "warnings": 2,
            }
            receipt = write_rebind_receipt(project, report)
            serialized = json.dumps(receipt)
            self.assertEqual(receipt["status"], "pass")
            self.assertNotIn(str(project), serialized)
            self.assertTrue(
                (project / ".dub-workbench" / "rebind-receipt.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
