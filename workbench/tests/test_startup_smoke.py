from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CoreStartupSmokeTests(unittest.TestCase):
    def test_backend_app_imports_without_optional_media_packages(self) -> None:
        code = r'''
import importlib.abc
import sys

class BlockOptionalMedia(importlib.abc.MetaPathFinder):
    blocked = {"librosa", "numpy", "soundfile"}
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in self.blocked:
            raise ModuleNotFoundError(f"blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, BlockOptionalMedia())
import backend.app
assert backend.app.app is not None
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment.update(
                {
                    "DUB_CREDENTIAL_BACKEND": "memory",
                    "DUB_WORKBENCH_STATE_DIR": str(root / "state"),
                    "DUB_WORKBENCH_LIBRARY_DIR": str(root / "library"),
                    "DUB_WORKBENCH_DB": str(root / "state" / "workbench.sqlite3"),
                }
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
