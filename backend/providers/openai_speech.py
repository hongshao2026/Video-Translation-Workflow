"""OpenAI-compatible synchronous text-to-speech provider."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .base import SpeechProvider
from .errors import ProviderResponseError
from .http_support import HttpProviderSupport
from .models import (
    ProbeResult,
    ProviderCapabilities,
    ProviderProfile,
    SpeechEstimate,
    SpeechRequest,
    SpeechResult,
    Voice,
)
from .transport import HttpTransport, RequestsTransport


class OpenAICompatibleSpeechProvider(HttpProviderSupport, SpeechProvider):
    """Adapter for the conservative ``POST /audio/speech`` contract.

    Voice choices are configuration data because many compatible services do
    not expose a voice-list endpoint.  Probe uses the non-generating model list.
    """

    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        if profile.kind != "speech":
            raise ValueError("OpenAI-compatible speech profile 必须是 speech")
        key = api_key.strip()
        if not key:
            raise ValueError("API Key 不能为空")
        formats = tuple(
            str(value).lower()
            for value in profile.options.get(
                "audio_formats", ["mp3", "wav", "flac", "opus", "aac", "pcm"]
            )
        )
        capabilities = ProviderCapabilities(
            provider_id=profile.provider_id,
            kind="speech",
            model_listing=bool(profile.options.get("model_listing", True)),
            voice_listing=bool(profile.options.get("voices")),
            speech_estimates=True,
            native_speed_one=True,
            audio_formats=formats,
        )
        SpeechProvider.__init__(self, profile, capabilities)
        self._api_key = key
        self._transport = transport or RequestsTransport()

    @property
    def _models_path(self) -> str:
        return str(self.profile.options.get("models_path", "/models"))

    @property
    def _speech_path(self) -> str:
        return str(self.profile.options.get("speech_path", "/audio/speech"))

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        model_count: int | None = None
        if self.capabilities.model_listing:
            response = self._request(
                "GET", self._models_path, billable=False, timeout=(8, 30)
            )
            body = self._body_mapping(response)
            rows = body.get("data")
            if not isinstance(rows, list):
                raise ProviderResponseError(
                    "模型列表响应缺少 data 数组。",
                    provider_id=self.profile.provider_id,
                    trace_id=self._trace_id(response),
                )
            model_count = len([row for row in rows if isinstance(row, Mapping)])
        return ProbeResult(
            ok=True,
            provider_id=self.profile.provider_id,
            profile_id=self.profile.profile_id,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            capabilities=self.capabilities,
            message="连接与凭证验证成功",
            model_count=model_count,
            voice_count=len(self.list_voices()) if self.capabilities.voice_listing else None,
        )

    def list_voices(self) -> list[Voice]:
        values = self.profile.options.get("voices", [])
        if not isinstance(values, (list, tuple)):
            raise TypeError("voices 配置必须是数组")
        voices: list[Voice] = []
        for value in values:
            if isinstance(value, str):
                voices.append(Voice(value, value))
            elif isinstance(value, Mapping) and value.get("voice_id"):
                voices.append(
                    Voice(
                        str(value["voice_id"]),
                        str(value.get("name") or value["voice_id"]),
                        str(value.get("description") or ""),
                        str(value.get("category") or "configured"),
                        str(value["language"]) if value.get("language") else None,
                    )
                )
        return voices

    def estimate(self, request: SpeechRequest) -> SpeechEstimate:
        model = self._model(request)
        units = len(request.text)
        price = self.profile.options.get("price_per_million_characters")
        estimated = None if price is None else units * float(price) / 1_000_000
        return SpeechEstimate(
            provider_id=self.profile.provider_id,
            model=model,
            billable_units=units,
            unit_name="characters",
            estimated_cost=estimated,
            currency=(str(self.profile.options.get("currency") or "USD") if price is not None else None),
        )

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        model = self._model(request)
        if request.audio_format not in self.capabilities.audio_formats:
            raise ValueError("当前 Provider 配置不支持请求的音频格式")
        payload: dict[str, Any] = {
            "model": model,
            "input": request.text,
            "voice": request.voice_id,
            "response_format": request.audio_format,
            "speed": 1.0,
        }
        protected = set(payload)
        overlaps = protected.intersection(request.extra)
        if overlaps:
            raise ValueError(f"Speech extra 不能覆盖核心字段：{', '.join(sorted(overlaps))}")
        payload.update(request.extra)
        response = self._request(
            "POST",
            self._speech_path,
            payload=payload,
            billable=True,
            timeout=(10, 180),
            extra_headers=(
                {"Idempotency-Key": request.idempotency_key}
                if request.idempotency_key
                else None
            ),
        )
        if not isinstance(response.body, (bytes, bytearray)) or not response.body:
            raise ProviderResponseError(
                "语音接口没有返回音频字节。",
                provider_id=self.profile.provider_id,
                trace_id=self._trace_id(response),
            )
        return SpeechResult(
            audio=bytes(response.body),
            audio_format=request.audio_format,
            provider_id=self.profile.provider_id,
            model=model,
            voice_id=request.voice_id,
            request_id=self._trace_id(response),
            usage_units=len(request.text),
        )

    def _model(self, request: SpeechRequest) -> str:
        model = (request.model or self.profile.model or "").strip()
        if not model:
            raise ValueError("必须在 Provider profile 或语音请求中指定模型")
        return model
