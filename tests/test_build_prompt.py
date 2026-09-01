from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_prompt import extract_video_id, validate_cookie_file


REPO_ROOT = Path(__file__).resolve().parents[1]


class PromptBuilderTests(unittest.TestCase):
    def test_extracts_supported_youtube_url_shapes(self) -> None:
        cases = {
            "https://www.youtube.com/watch?v=abcDEF_1234&list=private-list": "abcDEF_1234",
            "https://youtu.be/abcDEF_1234?t=12": "abcDEF_1234",
            "https://www.youtube.com/shorts/abcDEF_1234": "abcDEF_1234",
            "https://www.youtube.com/embed/abcDEF_1234": "abcDEF_1234",
            "https://www.youtube.com/live/abcDEF_1234": "abcDEF_1234",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_video_id(url), expected)

    def test_rejects_non_youtube_url(self) -> None:
        with self.assertRaises(ValueError):
            extract_video_id("https://example.com/watch?v=abcDEF_1234")

    def test_validates_netscape_header_and_youtube_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cookie_path = Path(temporary) / "netscape-input.txt"
            cookie_path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tTEST_NAME\tTEST_VALUE\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_cookie_file(cookie_path), cookie_path.resolve())

    def test_cli_builds_ignored_local_prompt_without_cookie_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            cookie_path = workspace / "netscape-input.txt"
            cookie_path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tTEST_NAME\tTEST_VALUE\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "build_prompt.py"),
                    "--url",
                    "https://www.youtube.com/watch?v=abcDEF_1234&list=tracking-value",
                    "--cookie-file",
                    str(cookie_path),
                    "--source-language",
                    "en",
                    "--workspace",
                    str(workspace),
                ],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            prompt_path = workspace / "abcDEF_1234_run" / "CODEX_PROMPT.md"
            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("https://www.youtube.com/watch?v=abcDEF_1234", prompt)
            self.assertNotIn("tracking-value", prompt)
            self.assertIn(str(cookie_path.resolve()), prompt)
            self.assertNotIn("TEST_NAME", prompt)
            self.assertNotIn("TEST_VALUE", prompt)
            self.assertNotIn("TEST_VALUE", result.stdout)
            self.assertIn("media_format_selection_vN.json", prompt)
            self.assertIn("不请求格式组合确认", prompt)
            self.assertIn("explicit_downstream_command", prompt)
            self.assertIn("不要求用户复制 SHA", prompt)

    def test_format_selection_is_automatic_not_a_manual_gate(self) -> None:
        definition = json.loads(
            (REPO_ROOT / "docs" / "workflow.definition.json").read_text(encoding="utf-8")
        )
        manual_gate_ids = {gate["id"] for gate in definition["manual_gates"]}
        self.assertNotIn("format", manual_gate_ids)

        acquire = next(stage for stage in definition["stages"] if stage["id"] == "acquire")
        self.assertEqual(
            acquire["automatic_format_gate"]["artifact"],
            "qa/media_format_selection_vN.json",
        )
        self.assertIn(
            "manual_approval_required=false",
            acquire["automatic_format_gate"]["conditions"],
        )

    def test_downstream_voice_command_binds_translation_approval(self) -> None:
        definition = json.loads(
            (REPO_ROOT / "docs" / "workflow.definition.json").read_text(encoding="utf-8")
        )
        self.assertEqual(definition["schema_version"], 9)

        audit = next(stage for stage in definition["stages"] if stage["id"] == "audit")
        conditions = set(audit["required_gate"]["conditions"])
        self.assertIn("approval_capture_mode=explicit_downstream_command", conditions)
        self.assertIn("manual_hash_repetition_required=false", conditions)
        self.assertIn("不追加批准问答", "".join(audit["tasks"]))


if __name__ == "__main__":
    unittest.main()
