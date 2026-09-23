from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.providers import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMUsage,
    ProbeResult,
    ProviderCapabilities,
    ProviderProfile,
    SpeechEstimate,
    SpeechProvider,
    SpeechRequest,
    SpeechResult,
)
from backend.workbench.database import WorkbenchDatabase
from backend.workbench.provider_ledger import (
    LedgeredLLMProvider,
    LedgeredSpeechProvider,
    ProviderRequestReplayBlocked,
)
from backend.workbench.runner import UncertainPaidRequest


class FakeLLM(LLMProvider):
    def __init__(self, *, uncertain: bool = False) -> None:
        profile = ProviderProfile("llm-profile", "fake-llm", "llm", "https://example.test", "fake-model")
        super().__init__(profile, ProviderCapabilities("fake-llm", "llm"))
        self.uncertain = uncertain
        self.calls = 0

    def probe(self):
        return ProbeResult(True, "fake-llm", "llm-profile", 1, self.capabilities, "ok")

    def list_models(self):
        return []

    def generate(self, request):
        self.calls += 1
        if self.uncertain:
            error = RuntimeError("network result hidden")
            error.uncertain_completion = True
            error.trace_id = "trace-uncertain"
            raise error
        return LLMResult("ok", "fake-llm", "fake-model", "req-1", "stop", LLMUsage(1, 1, 2))


class FakeSpeech(SpeechProvider):
    def __init__(self) -> None:
        profile = ProviderProfile("speech-profile", "fake-speech", "speech", "https://example.test", "speech-model")
        super().__init__(profile, ProviderCapabilities("fake-speech", "speech", native_speed_one=True))
        self.calls = 0

    def probe(self):
        return ProbeResult(True, "fake-speech", "speech-profile", 1, self.capabilities, "ok")

    def list_voices(self):
        return []

    def estimate(self, request):
        return SpeechEstimate("fake-speech", "speech-model", len(request.text), "characters")

    def synthesize(self, request):
        self.calls += 1
        return SpeechResult(b"audio", "mp3", "fake-speech", "speech-model", request.voice_id, request_id="speech-1", usage_units=2)


class ProviderLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = WorkbenchDatabase(Path(self.temp.name) / "ledger.sqlite3")
        self.database.initialize()
        for profile_id, kind, provider_id, model in (
            ("llm-profile", "llm", "fake-llm", "fake-model"),
            ("speech-profile", "speech", "fake-speech", "speech-model"),
        ):
            self.database.upsert_provider_profile(
                {
                    "id": profile_id,
                    "service_kind": kind,
                    "provider_id": provider_id,
                    "display_name": profile_id,
                    "model": model,
                    "config": {},
                    "capability": {},
                }
            )

    def test_completed_llm_request_cannot_be_billed_twice(self) -> None:
        delegate = FakeLLM()
        provider = LedgeredLLMProvider(delegate, self.database, None)
        request = LLMRequest.from_messages(
            [LLMMessage("user", "reply ok")],
            idempotency_key="stable-request-1",
        )
        self.assertEqual(provider.generate(request).text, "ok")
        self.assertEqual(provider.generate(request).text, "ok")
        self.assertEqual(delegate.calls, 1)
        row = self.database.list_provider_requests()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["billing_state"], "settled")
        self.assertEqual(row["usage"]["total_tokens"], 2)

    def test_uncertain_request_is_recorded_and_never_resent(self) -> None:
        delegate = FakeLLM(uncertain=True)
        provider = LedgeredLLMProvider(delegate, self.database, None)
        request = LLMRequest.from_messages(
            [LLMMessage("user", "reply ok")],
            idempotency_key="stable-request-2",
        )
        with self.assertRaises(RuntimeError):
            provider.generate(request)
        with self.assertRaises(UncertainPaidRequest):
            provider.generate(request)
        self.assertEqual(delegate.calls, 1)
        row = self.database.list_provider_requests()[0]
        self.assertEqual(row["status"], "uncertain")
        self.assertFalse(row["error"]["automatic_retry"])

    def test_completed_request_with_tampered_cache_fails_closed(self) -> None:
        delegate = FakeLLM()
        provider = LedgeredLLMProvider(delegate, self.database, None)
        request = LLMRequest.from_messages(
            [LLMMessage("user", "reply ok")],
            idempotency_key="stable-tampered-cache",
        )
        provider.generate(request)
        row = self.database.list_provider_requests()[0]
        cache_path = self.database.path.parent / row["result"]["cache"]["path"]
        cache_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ProviderRequestReplayBlocked):
            provider.generate(request)
        self.assertEqual(delegate.calls, 1)

    def test_speech_ledger_records_only_metadata_not_audio_or_text(self) -> None:
        delegate = FakeSpeech()
        provider = LedgeredSpeechProvider(delegate, self.database, None)
        request = SpeechRequest(
            text="不可写入数据库的正文",
            voice_id="voice-a",
            idempotency_key="speech-stable-1",
        )
        self.assertEqual(provider.synthesize(request).audio, b"audio")
        row = self.database.list_provider_requests()[0]
        self.assertEqual(row["usage"], {"units": 2})
        database_bytes = self.database.path.read_bytes()
        self.assertNotIn(request.text.encode("utf-8"), database_bytes)
        self.assertNotIn(b"audio", database_bytes)
        self.assertEqual(provider.synthesize(request).audio, b"audio")
        self.assertEqual(delegate.calls, 1)

    def test_terminal_provider_request_cannot_be_rewritten(self) -> None:
        provider = LedgeredLLMProvider(FakeLLM(), self.database, None)
        request = LLMRequest.from_messages(
            [LLMMessage("user", "reply ok")],
            idempotency_key="stable-terminal-request",
        )
        provider.generate(request)
        row = self.database.list_provider_requests()[0]
        with self.assertRaisesRegex(ValueError, "终态"):
            self.database.update_provider_request(
                row["id"], status="uncertain", billing_state="uncertain"
            )
        self.assertEqual(self.database.get_provider_request(row["id"])["status"], "completed")

    def test_successful_remote_call_with_ledger_commit_failure_is_uncertain(self) -> None:
        delegate = FakeLLM()
        provider = LedgeredLLMProvider(delegate, self.database, None)
        request = LLMRequest.from_messages(
            [LLMMessage("user", "reply ok")],
            idempotency_key="stable-ledger-commit-failure",
        )
        original = self.database.update_provider_request

        def fail_completion(request_id, **values):
            if values.get("status") == "completed":
                raise OSError("disk unavailable")
            return original(request_id, **values)

        with (
            patch.object(self.database, "update_provider_request", side_effect=fail_completion),
            self.assertRaises(UncertainPaidRequest),
        ):
            provider.generate(request)
        self.assertEqual(delegate.calls, 1)
        self.assertEqual(provider.generate(request).text, "ok")
        self.assertEqual(delegate.calls, 1)
        self.assertEqual(self.database.list_provider_requests()[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
