from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from backend.providers import (
    HttpResponse,
    LLMMessage,
    LLMRequest,
    MiniMaxLLMProvider,
    MiniMaxSpeechProvider,
    OpenAICompatibleLLMProvider,
    OpenAICompatibleSpeechProvider,
    ProviderAuthenticationError,
    ProviderProfile,
    ProviderTransportError,
    ProviderUnsupportedError,
    SpeechRequest,
    TransportTimeout,
    builtin_registry,
)


class FakeTransport:
    def __init__(self, *responses: HttpResponse | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any] | None,
        timeout: tuple[float, float],
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json": dict(json) if json is not None else None,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("FakeTransport received an unexpected request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ProviderModelTests(unittest.TestCase):
    def test_profile_cannot_store_credentials_or_use_remote_plain_http(self) -> None:
        with self.assertRaises(ValueError):
            ProviderProfile(
                profile_id="bad",
                provider_id="bad",
                kind="llm",
                base_url="https://example.test/v1",
                options={"nested": {"api_key": "must-not-live-here"}},
            )
        with self.assertRaises(ValueError):
            ProviderProfile(
                profile_id="bad-http",
                provider_id="bad",
                kind="llm",
                base_url="http://example.test/v1",
            )
        local = ProviderProfile(
            profile_id="local",
            provider_id="local",
            kind="llm",
            base_url="http://127.0.0.1:11434/v1",
        )
        self.assertEqual(local.base_url, "http://127.0.0.1:11434/v1")

    def test_production_speech_rejects_non_neutral_speed(self) -> None:
        with self.assertRaisesRegex(ValueError, "speed=1.0"):
            SpeechRequest(text="你好", voice_id="voice", speed=1.1)

    def test_builtin_registry_selects_only_explicit_adapters(self) -> None:
        registry = builtin_registry()
        profile = ProviderProfile(
            profile_id="translation",
            provider_id="openai-compatible",
            kind="llm",
            base_url="https://llm.example.test/v1",
            model="model-a",
        )
        provider = registry.create(
            profile,
            "unit-test-credential",
            transport=FakeTransport(),
        )
        self.assertIsInstance(provider, OpenAICompatibleLLMProvider)
        unknown = ProviderProfile(
            profile_id="unknown",
            provider_id="uninstalled-provider",
            kind="llm",
            base_url="https://unknown.example.test/v1",
            model="model-a",
        )
        with self.assertRaises(ProviderUnsupportedError):
            registry.create(unknown, "unit-test-credential", transport=FakeTransport())


class OpenAICompatibleProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = ProviderProfile(
            profile_id="translator-primary",
            provider_id="openai-compatible",
            kind="llm",
            base_url="https://llm.example.test/v1",
            model="model-a",
            credential_ref="os-keychain:translator-primary",
            options={"structured_output_mode": "json_schema"},
        )

    def test_probe_and_structured_generation_use_injected_transport(self) -> None:
        transport = FakeTransport(
            HttpResponse(
                200,
                {"data": [{"id": "model-a", "owned_by": "vendor"}]},
                {"x-request-id": "models-request"},
            ),
            HttpResponse(
                200,
                {
                    "id": "completion-1",
                    "model": "model-a",
                    "choices": [
                        {
                            "message": {"content": '{"translation":"你好"}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 5,
                        "total_tokens": 17,
                    },
                },
            ),
        )
        credential = "unit-test-credential"
        provider = OpenAICompatibleLLMProvider(
            self.profile,
            credential,
            transport=transport,
        )
        probe = provider.probe()
        result = provider.generate(
            LLMRequest(
                messages=(LLMMessage("user", "Translate hello"),),
                temperature=0,
                max_output_tokens=100,
                response_schema={
                    "type": "object",
                    "properties": {"translation": {"type": "string"}},
                    "required": ["translation"],
                    "additionalProperties": False,
                },
                idempotency_key="job-1-slot-1",
            )
        )

        self.assertTrue(probe.ok)
        self.assertEqual(probe.model_count, 1)
        self.assertEqual(result.structured, {"translation": "你好"})
        self.assertEqual(result.usage.total_tokens, 17)
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[0]["url"], "https://llm.example.test/v1/models")
        generation = transport.calls[1]
        self.assertEqual(generation["url"], "https://llm.example.test/v1/chat/completions")
        self.assertEqual(generation["headers"]["Authorization"], f"Bearer {credential}")
        self.assertEqual(generation["headers"]["Idempotency-Key"], "job-1-slot-1")
        self.assertNotIn(credential, repr(generation["json"]))
        self.assertNotIn(credential, repr(provider))
        self.assertNotIn(credential, repr(self.profile))
        self.assertEqual(
            generation["json"]["response_format"]["json_schema"]["strict"],
            True,
        )

    def test_schema_is_never_silently_ignored(self) -> None:
        profile = ProviderProfile(
            profile_id="plain",
            provider_id="plain",
            kind="llm",
            base_url="https://plain.example.test/v1",
            model="plain-model",
        )
        provider = OpenAICompatibleLLMProvider(
            profile,
            "unit-test-credential",
            transport=FakeTransport(),
        )
        with self.assertRaises(ProviderUnsupportedError):
            provider.generate(
                LLMRequest(
                    messages=(LLMMessage("user", "hello"),),
                    response_schema={"type": "object"},
                )
            )

    def test_prompt_json_mode_sends_schema_and_parses_strict_json(self) -> None:
        profile = ProviderProfile(
            profile_id="prompt-json",
            provider_id="openai-compatible",
            kind="llm",
            base_url="https://plain.example.test/v1",
            model="plain-model",
            options={"structured_output_mode": "prompt_json"},
        )
        transport = FakeTransport(
            HttpResponse(
                200,
                {
                    "id": "completion-prompt-json",
                    "choices": [{"message": {"content": '{"value":"ok"}'}, "finish_reason": "stop"}],
                },
            )
        )
        provider = OpenAICompatibleLLMProvider(
            profile, "unit-test-credential", transport=transport
        )
        result = provider.generate(
            LLMRequest(
                messages=(LLMMessage("user", "produce value"),),
                response_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        )
        self.assertEqual(result.structured, {"value": "ok"})
        self.assertIn("JSON Schema", transport.calls[0]["json"]["messages"][0]["content"])
        self.assertNotIn("response_format", transport.calls[0]["json"])

    def test_billable_timeout_is_uncertain_and_not_retried(self) -> None:
        transport = FakeTransport(TransportTimeout("late"))
        provider = OpenAICompatibleLLMProvider(
            self.profile,
            "unit-test-credential",
            transport=transport,
        )
        with self.assertRaises(ProviderTransportError) as raised:
            provider.generate(
                LLMRequest(messages=(LLMMessage("user", "hello"),))
            )
        self.assertTrue(raised.exception.uncertain_completion)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_authentication_error_never_echoes_credential(self) -> None:
        credential = "unit-test-credential"
        transport = FakeTransport(
            HttpResponse(
                401,
                {"error": {"message": f"bad token {credential}"}},
            )
        )
        provider = OpenAICompatibleLLMProvider(
            self.profile,
            credential,
            transport=transport,
        )
        with self.assertRaises(ProviderAuthenticationError) as raised:
            provider.list_models()
        self.assertNotIn(credential, str(raised.exception))
        self.assertNotIn(credential, repr(raised.exception.public_dict()))


class OpenAICompatibleSpeechTests(unittest.TestCase):
    def test_probe_estimate_and_synthesize_binary_audio(self) -> None:
        profile = ProviderProfile(
            profile_id="speech-compatible",
            provider_id="openai-compatible-speech",
            kind="speech",
            base_url="https://speech.example.test/v1",
            model="voice-model",
            options={"voices": ["voice-a"], "price_per_million_characters": 10},
        )
        transport = FakeTransport(
            HttpResponse(200, {"data": [{"id": "voice-model"}]}),
            HttpResponse(200, b"ID3-compatible", {"x-request-id": "speech-request"}),
        )
        provider = OpenAICompatibleSpeechProvider(
            profile, "unit-test-credential", transport=transport
        )
        probe = provider.probe()
        request = SpeechRequest(
            text="你好",
            voice_id="voice-a",
            idempotency_key="speech-idempotency",
        )
        estimate = provider.estimate(request)
        result = provider.synthesize(request)
        self.assertTrue(probe.ok)
        self.assertEqual(probe.voice_count, 1)
        self.assertEqual(estimate.billable_units, 2)
        self.assertEqual(result.audio, b"ID3-compatible")
        call = transport.calls[1]
        self.assertEqual(call["url"], "https://speech.example.test/v1/audio/speech")
        self.assertEqual(call["json"]["speed"], 1.0)
        self.assertEqual(call["headers"]["Idempotency-Key"], "speech-idempotency")


class MiniMaxProviderTests(unittest.TestCase):
    def test_minimax_llm_uses_vendor_route_without_real_request(self) -> None:
        profile = ProviderProfile(
            profile_id="minimax-translation",
            provider_id="minimax-llm",
            kind="llm",
            base_url="https://api.minimax.cn/v1",
            model="MiniMax-M3",
        )
        transport = FakeTransport(
            HttpResponse(
                200,
                {"data": [{"id": "MiniMax-M3", "owned_by": "MiniMax"}]},
            ),
            HttpResponse(
                200,
                {
                    "id": "mm-text-1",
                    "model": "MiniMax-M3",
                    "choices": [
                        {"message": {"content": "你好"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            )
        )
        provider = MiniMaxLLMProvider(
            profile,
            "unit-test-credential",
            transport=transport,
        )
        probe = provider.probe()
        result = provider.generate(
            LLMRequest(
                messages=(LLMMessage("user", "hello"),),
                max_output_tokens=16,
            )
        )
        self.assertTrue(probe.ok)
        self.assertEqual(probe.model_count, 1)
        self.assertEqual(
            transport.calls[0]["url"],
            "https://api.minimax.cn/v1/models",
        )
        call = transport.calls[1]
        self.assertEqual(
            call["url"],
            "https://api.minimax.cn/v1/chat/completions",
        )
        self.assertEqual(call["json"]["max_completion_tokens"], 16)
        self.assertEqual(result.text, "你好")

    def test_minimax_speech_probe_estimate_and_synthesize(self) -> None:
        profile = ProviderProfile(
            profile_id="minimax-speech",
            provider_id="minimax-speech",
            kind="speech",
            base_url="https://api.minimax.cn",
            model="speech-2.8-hd",
        )
        transport = FakeTransport(
            HttpResponse(
                200,
                {
                    "system_voice": [
                        {
                            "voice_id": "voice-a",
                            "voice_name": "测试音色",
                            "description": ["普通话", "女声"],
                        }
                    ],
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                },
            ),
            HttpResponse(
                200,
                {
                    "data": {"audio": b"ID3demo".hex(), "status": 2},
                    "extra_info": {
                        "audio_format": "mp3",
                        "usage_characters": 4,
                    },
                    "trace_id": "speech-trace-1",
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                },
            ),
        )
        credential = "unit-test-credential"
        provider = MiniMaxSpeechProvider(
            profile,
            credential,
            transport=transport,
        )
        probe = provider.probe()
        request = SpeechRequest(text="你好", voice_id="voice-a")
        estimate = provider.estimate(request)
        result = provider.synthesize(request)

        self.assertEqual(probe.voice_count, 1)
        self.assertEqual(estimate.billable_units, 4)
        self.assertEqual(result.audio, b"ID3demo")
        self.assertEqual(result.trace_id, "speech-trace-1")
        self.assertEqual(result.usage_units, 4)
        self.assertEqual(transport.calls[0]["url"], "https://api.minimax.cn/v1/get_voice")
        synthesis = transport.calls[1]
        self.assertEqual(synthesis["url"], "https://api.minimax.cn/v1/t2a_v2")
        self.assertEqual(synthesis["json"]["voice_setting"]["speed"], 1.0)
        self.assertNotIn(credential, repr(synthesis["json"]))
        self.assertNotIn(credential, repr(provider))

    def test_minimax_billable_timeout_preserves_uncertain_state(self) -> None:
        profile = ProviderProfile(
            profile_id="minimax-speech",
            provider_id="minimax-speech",
            kind="speech",
            base_url="https://api.minimax.cn",
            model="speech-2.8-hd",
        )
        transport = FakeTransport(TransportTimeout("late"))
        provider = MiniMaxSpeechProvider(
            profile,
            "unit-test-credential",
            transport=transport,
        )
        with self.assertRaises(ProviderTransportError) as raised:
            provider.synthesize(SpeechRequest(text="你好", voice_id="voice-a"))
        self.assertTrue(raised.exception.uncertain_completion)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
