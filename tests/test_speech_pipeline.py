from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.providers import ProviderProfile, SpeechEstimate, SpeechResult
from backend.workbench.runner import UncertainPaidRequest
from backend.workbench.speech import (
    SpeechManifestError,
    SpeechPipeline,
    authorization_for_segments,
)


class FakeSpeech:
    def __init__(self, *, uncertain: bool = False) -> None:
        self.profile = ProviderProfile(
            profile_id="speech-fixture",
            provider_id="fake-speech",
            kind="speech",
            base_url="http://127.0.0.1:9999",
            model="speech-fixture-v1",
        )
        self.calls = []
        self.uncertain = uncertain

    def estimate(self, request):
        self.assert_native(request)
        return SpeechEstimate(
            provider_id="fake-speech",
            model=request.model,
            billable_units=len(request.text),
            unit_name="characters",
            estimated_cost=len(request.text) / 1000,
            currency="CNY",
        )

    def synthesize(self, request):
        self.assert_native(request)
        self.calls.append(request)
        if self.uncertain:
            error = RuntimeError("timeout")
            error.uncertain_completion = True
            error.code = "timeout_uncertain"
            error.trace_id = "trace-fixture"
            raise error
        return SpeechResult(
            audio=b"ID3" + request.text.encode("utf-8"),
            audio_format="mp3",
            provider_id="fake-speech",
            model=request.model,
            voice_id=request.voice_id,
            trace_id=f"trace-{len(self.calls)}",
            usage_units=len(request.text),
        )

    @staticmethod
    def assert_native(request):
        if request.speed != 1.0:
            raise AssertionError("speech speed changed")


class ReplaySpeech(FakeSpeech):
    def __init__(self) -> None:
        super().__init__()
        self.cached_result = None
        self.replay_calls = 0

    def synthesize(self, request):
        result = super().synthesize(request)
        self.cached_result = result
        return result

    def replay(self, request):
        self.assert_native(request)
        self.replay_calls += 1
        if self.cached_result is None:
            raise AssertionError("missing cached result")
        return self.cached_result


class SpeechPipelineTests(unittest.TestCase):
    def segments(self):
        return [
            {"segment_id": "S001", "text": "你好", "voice_id": "voice-a"},
            {"segment_id": "S002", "text": "欢迎", "voice_id": "voice-b"},
        ]

    def test_dry_run_and_cache_reuse_never_change_speed(self) -> None:
        provider = FakeSpeech()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = SpeechPipeline(provider, Path(temp_dir))
            dry_run = pipeline.dry_run(self.segments())
            self.assertEqual(dry_run["status"], "pass")
            self.assertEqual(dry_run["speed"], 1.0)
            authorization = authorization_for_segments(provider, self.segments())
            manifest = pipeline.synthesize(self.segments(), authorization=authorization)
            self.assertEqual(manifest["status"], "ready")
            self.assertEqual(len(provider.calls), 2)
            pipeline.synthesize(self.segments(), authorization=authorization)
            self.assertEqual(len(provider.calls), 2)
            self.assertTrue((Path(temp_dir) / "raw" / "S001.mp3").is_file())

    def test_authorization_must_bind_current_segments(self) -> None:
        provider = FakeSpeech()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = SpeechPipeline(provider, Path(temp_dir))
            authorization = authorization_for_segments(provider, self.segments())
            changed = [*self.segments()]
            changed[0] = {**changed[0], "text": "变更后的文本"}
            with self.assertRaisesRegex(SpeechManifestError, "授权"):
                pipeline.synthesize(changed, authorization=authorization)
        self.assertEqual(provider.calls, [])

    def test_non_native_speed_is_rejected_before_provider_call(self) -> None:
        provider = FakeSpeech()
        segments = [{**self.segments()[0], "speed": 1.1}]
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = SpeechPipeline(provider, Path(temp_dir))
            with self.assertRaisesRegex(SpeechManifestError, "speed=1.0"):
                pipeline.dry_run(segments)

    def test_uncertain_request_locks_manifest_against_retry(self) -> None:
        provider = FakeSpeech(uncertain=True)
        segments = [self.segments()[0]]
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = SpeechPipeline(provider, Path(temp_dir))
            authorization = authorization_for_segments(provider, segments)
            with self.assertRaises(UncertainPaidRequest):
                pipeline.synthesize(segments, authorization=authorization)
            manifest = json.loads((Path(temp_dir) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["segments"]["S001"]["status"], "uncertain")
            with self.assertRaises(UncertainPaidRequest):
                pipeline.synthesize(segments, authorization=authorization)
            self.assertEqual(len(provider.calls), 1)

    def test_audition_scope_is_separate_and_uncertain_request_never_retries(self) -> None:
        provider = FakeSpeech(uncertain=True)
        segments = [self.segments()[0]]
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = SpeechPipeline(provider, Path(temp_dir))
            authorization = authorization_for_segments(
                provider,
                segments,
                scope="audition_current_inputs",
            )
            with self.assertRaisesRegex(SpeechManifestError, "授权范围"):
                pipeline.synthesize(segments, authorization=authorization)
            with self.assertRaises(UncertainPaidRequest):
                pipeline.synthesize(
                    segments,
                    authorization=authorization,
                    authorization_scope="audition_current_inputs",
                )
            with self.assertRaises(UncertainPaidRequest):
                pipeline.synthesize(
                    segments,
                    authorization=authorization,
                    authorization_scope="audition_current_inputs",
                )
            self.assertEqual(len(provider.calls), 1)
            manifest = json.loads(
                (Path(temp_dir) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["locks"]["authorization_scope"],
                "audition_current_inputs",
            )

    def test_sent_manifest_recovers_from_response_cache_without_rebilling(self) -> None:
        provider = ReplaySpeech()
        segments = [self.segments()[0]]
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = SpeechPipeline(provider, Path(temp_dir))
            authorization = authorization_for_segments(provider, segments)
            with (
                patch(
                    "backend.workbench.speech._atomic_bytes",
                    side_effect=OSError("fixture disk interruption"),
                ),
                self.assertRaisesRegex(OSError, "disk interruption"),
            ):
                pipeline.synthesize(segments, authorization=authorization)
            manifest = pipeline.synthesize(segments, authorization=authorization)
            self.assertEqual(manifest["status"], "ready")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(provider.replay_calls, 1)


if __name__ == "__main__":
    unittest.main()
