"""Provider-neutral request, result, profile, and capability models."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlparse

ProviderKind = Literal["llm", "speech"]
JsonMapping = Mapping[str, Any]
_SECRET_OPTION_NAMES = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "credential",
    "credentials",
    "custom_headers",
    "headers",
    "password",
    "secret",
    "token",
}


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _reject_secret_options(value: Mapping[str, Any], *, field_name: str) -> None:
    for key, nested in value.items():
        normalized = str(key).lower().replace("-", "_")
        if (
            normalized in _SECRET_OPTION_NAMES
            or normalized.endswith(("_api_key", "_token", "_secret"))
        ):
            raise ValueError(
                f"{field_name} 不能包含密钥；请通过运行时凭证解析器提供"
            )
        if isinstance(nested, Mapping):
            _reject_secret_options(nested, field_name=field_name)
        elif isinstance(nested, (list, tuple)):
            for item in nested:
                if isinstance(item, Mapping):
                    _reject_secret_options(item, field_name=field_name)


@dataclass(frozen=True, repr=False)
class ProviderProfile:
    """Serializable provider configuration with no credential material."""

    profile_id: str
    provider_id: str
    kind: ProviderKind
    base_url: str
    model: str | None = None
    credential_ref: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        profile_id = self.profile_id.strip()
        provider_id = self.provider_id.strip()
        base_url = self.base_url.strip().rstrip("/")
        model = self.model.strip() if self.model else None
        credential_ref = self.credential_ref.strip() if self.credential_ref else None
        if not profile_id or not provider_id:
            raise ValueError("Provider profile_id 和 provider_id 不能为空")
        if self.kind not in {"llm", "speech"}:
            raise ValueError("Provider kind 只能是 llm 或 speech")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provider base_url 必须是有效的 HTTP(S) 地址")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("远程 Provider 必须使用 HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provider base_url 不能包含凭证、查询参数或片段")
        options = dict(self.options)
        _reject_secret_options(options, field_name="Provider options")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "credential_ref", credential_ref)
        object.__setattr__(self, "options", _frozen_mapping(options))

    def public_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "kind": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "credential_ref": self.credential_ref,
            "options": dict(self.options),
            "enabled": self.enabled,
        }

    def __repr__(self) -> str:
        return f"ProviderProfile({self.public_dict()!r})"


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    kind: ProviderKind
    model_listing: bool = False
    structured_outputs: bool = False
    json_mode: bool = False
    tool_calls: bool = False
    multimodal_input: bool = False
    voice_listing: bool = False
    speech_estimates: bool = False
    async_operations: bool = False
    native_speed_one: bool = False
    audio_formats: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["audio_formats"] = list(self.audio_formats)
        return payload


@dataclass(frozen=True)
class ProviderModel:
    model_id: str
    label: str | None = None
    owned_by: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    provider_id: str
    profile_id: str
    latency_ms: int
    capabilities: ProviderCapabilities
    message: str
    model_count: int | None = None
    voice_count: int | None = None


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: Any
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("不支持的消息角色")
        if self.content is None or self.content == "":
            raise ValueError("消息内容不能为空")

    def request_dict(self) -> dict[str, Any]:
        row = {"role": self.role, "content": self.content}
        if self.name:
            row["name"] = self.name
        return row


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    response_schema: Mapping[str, Any] | None = None
    schema_name: str = "workflow_output"
    idempotency_key: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("LLM 请求至少需要一条消息")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature 必须在 0–2 之间")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须大于 0")
        if not self.schema_name.strip():
            raise ValueError("schema_name 不能为空")
        schema = dict(self.response_schema) if self.response_schema else None
        extra = dict(self.extra)
        _reject_secret_options(extra, field_name="LLM extra")
        if schema is not None:
            json.dumps(schema)
        object.__setattr__(self, "response_schema", _frozen_mapping(schema) if schema else None)
        object.__setattr__(self, "extra", _frozen_mapping(extra))

    @classmethod
    def from_messages(
        cls,
        messages: Sequence[LLMMessage | Mapping[str, Any]],
        **kwargs: Any,
    ) -> LLMRequest:
        normalized = tuple(
            message
            if isinstance(message, LLMMessage)
            else LLMMessage(
                role=str(message["role"]),  # type: ignore[arg-type]
                content=message["content"],
                name=str(message["name"]) if message.get("name") else None,
            )
            for message in messages
        )
        return cls(messages=normalized, **kwargs)


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider_id: str
    model: str
    request_id: str | None
    finish_reason: str | None
    usage: LLMUsage
    structured: Any = None


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    description: str = ""
    category: str = "system"
    language: str | None = None


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: str
    model: str | None = None
    audio_format: str = "mp3"
    sample_rate: int = 32000
    bitrate: int = 128000
    channel: int = 1
    speed: float = 1.0
    emotion: str | None = None
    language_boost: str = "Chinese"
    subtitle_type: str | None = None
    idempotency_key: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        text = self.text.strip()
        voice_id = self.voice_id.strip()
        if not text:
            raise ValueError("语音文本不能为空")
        if not voice_id:
            raise ValueError("voice_id 不能为空")
        if self.speed != 1.0:
            raise ValueError("正式中文 TTS 必须使用模型原生 speed=1.0")
        extra = dict(self.extra)
        _reject_secret_options(extra, field_name="Speech extra")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "voice_id", voice_id)
        object.__setattr__(self, "audio_format", self.audio_format.lower())
        object.__setattr__(self, "extra", _frozen_mapping(extra))


@dataclass(frozen=True)
class SpeechEstimate:
    provider_id: str
    model: str
    billable_units: int
    unit_name: str
    estimated_cost: float | None = None
    currency: str | None = None


@dataclass(frozen=True)
class SpeechResult:
    audio: bytes
    audio_format: str
    provider_id: str
    model: str
    voice_id: str
    request_id: str | None = None
    trace_id: str | None = None
    usage_units: int | None = None
    subtitle_file: str | None = None
