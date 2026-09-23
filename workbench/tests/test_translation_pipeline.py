from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.providers import LLMResult, LLMUsage
from backend.workbench.translation import (
    TranslationPipeline,
    TranslationValidationError,
)

PROMPT_PACK = {
    "common_policy": ["保留稳定 ID。"],
    "roles": {
        "T": {"purpose": "翻译"},
        "A": {"purpose": "中文审核"},
        "B": {"purpose": "忠实审核"},
        "C": {"purpose": "裁决"},
    },
}


class FakeLLM:
    def __init__(self, role: str, *, invalid_ids: bool = False) -> None:
        self.role = role
        self.invalid_ids = invalid_ids
        self.calls = []

    def generate(self, request):
        payload = json.loads(request.messages[-1].content)
        self.calls.append(payload)
        if self.role == "T":
            items = [
                {"id": row["id"], "subtitle_zh": f"译文-{row['id']}", "uncertainty": None}
                for row in payload["items"]
            ]
            if self.invalid_ids:
                items[0]["id"] = "changed"
            structured = {"items": items}
        elif self.role == "A":
            structured = {
                "covered_ids": [row["id"] for row in payload["items"]],
                "issues": (
                    [
                        {
                            "id": payload["items"][0]["id"],
                            "severity": "medium",
                            "category": "fluency",
                            "suggestion": "更自然的译文",
                            "rationale": "口语更顺",
                        }
                    ]
                    if not self.calls[:-1]
                    else []
                ),
            }
        elif self.role == "B":
            structured = {
                "covered_ids": [row["id"] for row in payload["items"]],
                "issues": [],
            }
        else:
            structured = {
                "decisions": [
                    {
                        "id": issue["id"],
                        "action": "replace",
                        "final_text": issue["suggestion"],
                        "rationale": "采纳审核意见",
                    }
                    for issue in payload["issues"]
                ]
            }
        return LLMResult(
            text=json.dumps(structured, ensure_ascii=False),
            structured=structured,
            provider_id=f"fake-{self.role.lower()}",
            model="fixture-model",
            request_id=f"req-{self.role}-{len(self.calls)}",
            finish_reason="stop",
            usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class FailingLLM(FakeLLM):
    def generate(self, request):
        payload = json.loads(request.messages[-1].content)
        self.calls.append(payload)
        raise RuntimeError("fixture review failure")


class TranslationPipelineTests(unittest.TestCase):
    def providers(self, **replacements):
        values = {role: FakeLLM(role) for role in ("T", "A", "B", "C")}
        values.update(replacements)
        return values

    def test_roles_are_isolated_and_full_coverage_is_written(self) -> None:
        providers = self.providers()
        rows = [
            {"id": "S001", "source_text": "Hello", "speaker": "host"},
            {"id": "S002", "source_text": "World", "speaker": "guest"},
            {"id": "S003", "source_text": "Again", "speaker": "host"},
        ]
        progress = []
        with tempfile.TemporaryDirectory() as temp_dir:
            result = TranslationPipeline(providers, PROMPT_PACK, batch_size=2).run(
                rows,
                glossary={"World": "世界"},
                output_dir=Path(temp_dir),
                progress=lambda *values: progress.append(values),
            )
            self.assertEqual(result["stable_ids"], ["S001", "S002", "S003"])
            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["translation"][0]["subtitle_zh"], "更自然的译文")
            self.assertEqual(result["reviewers"]["A"]["candidate_sha256"], result["reviewers"]["B"]["candidate_sha256"])
            self.assertFalse(any("issues" in call for call in providers["A"].calls))
            self.assertFalse(any("issues" in call for call in providers["B"].calls))
            self.assertTrue((Path(temp_dir) / "work" / "translation_final_v1.json").is_file())
            self.assertTrue((Path(temp_dir) / "qa" / "translation_regression_v1.json").is_file())
        self.assertTrue(any(row[0] == "T" and row[1] == 3 for row in progress))
        self.assertTrue(any(row[0] == "A" and row[1] == 3 for row in progress))
        self.assertTrue(any(row[0] == "B" and row[1] == 3 for row in progress))

    def test_translator_cannot_change_stable_ids(self) -> None:
        pipeline = TranslationPipeline(
            self.providers(T=FakeLLM("T", invalid_ids=True)),
            PROMPT_PACK,
        )
        with self.assertRaisesRegex(TranslationValidationError, "稳定 ID"):
            pipeline.run([{"id": "S001", "source_text": "Hello"}])

    def test_duplicate_source_ids_fail_before_any_provider_call(self) -> None:
        providers = self.providers()
        pipeline = TranslationPipeline(providers, PROMPT_PACK)
        with self.assertRaisesRegex(TranslationValidationError, "稳定 ID"):
            pipeline.run(
                [
                    {"id": "S001", "source_text": "Hello"},
                    {"id": "S001", "source_text": "Again"},
                ]
            )
        self.assertEqual(providers["T"].calls, [])

    def test_validated_batches_resume_without_repeating_billable_calls(self) -> None:
        rows = [
            {"id": "S001", "source_text": "One"},
            {"id": "S002", "source_text": "Two"},
            {"id": "S003", "source_text": "Three"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            first = self.providers(A=FailingLLM("A"))
            with self.assertRaisesRegex(RuntimeError, "fixture review failure"):
                TranslationPipeline(first, PROMPT_PACK, batch_size=2).run(
                    rows, output_dir=output, version=3
                )
            self.assertEqual(len(first["T"].calls), 2)
            # B is independent and completes while A fails; both its batches
            # and all T batches are durable before the failed turn exits.
            self.assertEqual(len(first["B"].calls), 2)

            resumed = self.providers()
            result = TranslationPipeline(resumed, PROMPT_PACK, batch_size=2).run(
                rows, output_dir=output, version=3
            )
            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(resumed["T"].calls, [])
            self.assertEqual(resumed["B"].calls, [])
            self.assertEqual(len(resumed["A"].calls), 2)
            self.assertEqual(len(resumed["C"].calls), 1)

    def test_checkpoint_is_bound_to_prompt_and_cannot_be_silently_reused(self) -> None:
        rows = [{"id": "S001", "source_text": "One"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            TranslationPipeline(self.providers(), PROMPT_PACK).run(
                rows, output_dir=output, version=7
            )
            changed_pack = {
                **PROMPT_PACK,
                "common_policy": ["这是另一份已冻结提示词。"],
            }
            providers = self.providers()
            with self.assertRaisesRegex(TranslationValidationError, "冻结输入不匹配"):
                TranslationPipeline(providers, changed_pack).run(
                    rows, output_dir=output, version=7
                )
            self.assertEqual(providers["T"].calls, [])


if __name__ == "__main__":
    unittest.main()
