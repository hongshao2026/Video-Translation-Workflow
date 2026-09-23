"""Unified speech-provider wrapper around the proven MiniMax client."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import requests

from backend.minimax_client import (
    MiniMaxClient,
    MiniMaxError,
    SpeechConfig,
    billable_characters,
    estimate_cost,
)

from .base import SpeechProvider
from .errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
)
from .models import (
    ProbeResult,
    ProviderCapabilities,
    ProviderProfile,
    SpeechEstimate,
    SpeechRequest,
    SpeechResult,
    Voice,
)
from .transport import (
    HttpResponse,
    HttpTransport,
    RequestsTransport,
    TransportFailure,
    TransportTimeout,
)


class _LegacySessionAdapter:
    """Expose the small ``requests.Session.post`` surface MiniMaxClient uses."""

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: tuple[float, float],
    ) -> HttpResponse:
        try:
            return self._transport.request(
                "POST",
                url,
                headers=headers,
                json=json,
                timeout=timeout,
            )
        except TransportTimeout as exc:
            raise requests.Timeout() from exc
        except TransportFailure as exc:
            raise requests.ConnectionError() from exc


class MiniMaxSpeechProvider(SpeechProvider):
    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        if profile.kind != "speech":
            raise ValueError("MiniMax speech profile 必须是 speech")
        key = api_key.strip()
        if not key:
            raise ValueError("API Key 不能为空")
        capabilities = ProviderCapabilities(
            provider_id=profile.provider_id,
            kind="speech",
            voice_listing=True,
            speech_estimates=True,
            native_speed_one=True,
            audio_formats=("mp3", "wav", "flac"),
        )
        super().__init__(profile, capabilities)
        self._api_key = key
        session = _LegacySessionAdapter(transport or RequestsTransport())
        self._client = MiniMaxClient(
            key,
            base_url=profile.base_url,
            session=session,  # type: ignore[arg-type]
        )

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        voices = self.list_voices()
        return ProbeResult(
            ok=True,
            provider_id=self.profile.provider_id,
            profile_id=self.profile.profile_id,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            capabilities=self.capabilities,
            message="连接、凭证与音色目录验证成功",
            voice_count=len(voices),
        )

    def list_voices(self) -> list[Voice]:
        try:
            rows = self._client.list_voices()
        except MiniMaxError as exc:
            raise self._convert_error(exc, billable=False) from exc
        return [
            Voice(
                voice_id=str(row["voice_id"]),
                name=str(row.get("voice_name") or row["voice_id"]),
                description=str(row.get("description") or ""),
                category=str(row.get("category") or "system"),
            )
            for row in rows
            if row.get("voice_id")
        ]

    def estimate(self, request: SpeechRequest) -> SpeechEstimate:
        model = self._model(request)
        cost = estimate_cost(request.text, model)
        return SpeechEstimate(
            provider_id=self.profile.provider_id,
            model=model,
            billable_units=int(cost["billable_characters"]),
            unit_name="characters",
            estimated_cost=float(cost["estimated_cny"]),
            currency="CNY",
        )

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        model = self._model(request)
        config_values = {
            **dict(request.extra),
            "model": model,
            "speed": request.speed,
            "sample_rate": request.sample_rate,
            "bitrate": request.bitrate,
            "format": request.audio_format,
            "channel": request.channel,
            "emotion": request.emotion,
            "language_boost": request.language_boost,
        }
        try:
            config = SpeechConfig.from_mapping(config_values)
            result = self._client.synthesize(
                request.text,
                request.voice_id,
                config,
                subtitle_type=request.subtitle_type,
            )
        except ValueError:
            raise
        except MiniMaxError as exc:
            raise self._convert_error(exc, billable=True) from exc
        usage = result.extra_info.get("usage_characters")
        try:
            usage_units = int(usage) if usage is not None else None
        except (TypeError, ValueError):
            usage_units = None
        return SpeechResult(
            audio=result.audio,
            audio_format=result.audio_format,
            provider_id=self.profile.provider_id,
            model=model,
            voice_id=request.voice_id,
            trace_id=result.trace_id,
            usage_units=usage_units or billable_characters(request.text),
            subtitle_file=result.subtitle_file,
        )

    def _model(self, request: SpeechRequest) -> str:
        model = (request.model or self.profile.model or "").strip()
        if not model:
            raise ValueError("必须在 Provider profile 或语音请求中指定模型")
        return model

    def _convert_error(self, exc: MiniMaxError, *, billable: bool) -> ProviderError:
        kwargs = {
            "provider_id": self.profile.provider_id,
            "status_code": exc.status_code,
            "trace_id": exc.trace_id,
            "uncertain_completion": exc.uncertain_completion,
        }
        if exc.status_code == 1004:
            return ProviderAuthenticationError(str(exc), **kwargs)
        if exc.status_code in {1002, 1039}:
            return ProviderRateLimitError(str(exc), retryable=True, **kwargs)
        if exc.status_code in {1042, 2013}:
            return ProviderInvalidRequestError(str(exc), **kwargs)
        if exc.uncertain_completion:
            return ProviderTransportError(str(exc), retryable=False, **kwargs)
        if exc.status_code is None:
            return ProviderTransportError(
                str(exc), retryable=not billable, **kwargs
            )
        return ProviderResponseError(str(exc), **kwargs)
