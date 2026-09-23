"""Built-in provider registry used by the workflow runtime."""

from __future__ import annotations

from collections.abc import Callable

from .base import LLMProvider, SpeechProvider
from .errors import ProviderUnsupportedError
from .minimax_llm import MiniMaxLLMProvider
from .minimax_speech import MiniMaxSpeechProvider
from .models import ProviderProfile
from .openai_compatible import OpenAICompatibleLLMProvider
from .openai_speech import OpenAICompatibleSpeechProvider
from .transport import HttpTransport

ProviderInstance = LLMProvider | SpeechProvider
ProviderFactory = Callable[
    [ProviderProfile, str, HttpTransport | None],
    ProviderInstance,
]


def _openai_factory(
    profile: ProviderProfile,
    api_key: str,
    transport: HttpTransport | None,
) -> ProviderInstance:
    return OpenAICompatibleLLMProvider(profile, api_key, transport=transport)


def _minimax_llm_factory(
    profile: ProviderProfile,
    api_key: str,
    transport: HttpTransport | None,
) -> ProviderInstance:
    return MiniMaxLLMProvider(profile, api_key, transport=transport)


def _minimax_speech_factory(
    profile: ProviderProfile,
    api_key: str,
    transport: HttpTransport | None,
) -> ProviderInstance:
    return MiniMaxSpeechProvider(profile, api_key, transport=transport)


def _openai_speech_factory(
    profile: ProviderProfile,
    api_key: str,
    transport: HttpTransport | None,
) -> ProviderInstance:
    return OpenAICompatibleSpeechProvider(profile, api_key, transport=transport)


class ProviderRegistry:
    """Explicit registry; arbitrary executable plugins are not auto-loaded."""

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], ProviderFactory] = {}

    def register(
        self,
        provider_id: str,
        kind: str,
        factory: ProviderFactory,
    ) -> None:
        key = (provider_id.strip(), kind.strip())
        if not all(key) or kind not in {"llm", "speech"}:
            raise ValueError("Provider 注册信息无效")
        if key in self._factories:
            raise ValueError(f"Provider 已注册：{provider_id}/{kind}")
        self._factories[key] = factory

    def create(
        self,
        profile: ProviderProfile,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
    ) -> ProviderInstance:
        factory = self._factories.get((profile.provider_id, profile.kind))
        if factory is None:
            raise ProviderUnsupportedError(
                "尚未安装此 Provider 适配器。",
                provider_id=profile.provider_id,
            )
        return factory(profile, api_key, transport)

    def registered(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._factories))


def builtin_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("openai-compatible", "llm", _openai_factory)
    registry.register("minimax-llm", "llm", _minimax_llm_factory)
    registry.register("minimax-speech", "speech", _minimax_speech_factory)
    registry.register("openai-compatible-speech", "speech", _openai_speech_factory)
    return registry
