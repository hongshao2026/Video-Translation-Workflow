"""Replaceable LLM and speech providers for the dubbing workbench."""

from .base import LLMProvider, SpeechProvider
from .errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
    ProviderUnsupportedError,
)
from .minimax_llm import MiniMaxLLMProvider
from .minimax_speech import MiniMaxSpeechProvider
from .models import (
    LLMMessage,
    LLMRequest,
    LLMResult,
    LLMUsage,
    ProbeResult,
    ProviderCapabilities,
    ProviderModel,
    ProviderProfile,
    SpeechEstimate,
    SpeechRequest,
    SpeechResult,
    Voice,
)
from .openai_compatible import OpenAICompatibleLLMProvider
from .openai_speech import OpenAICompatibleSpeechProvider
from .registry import ProviderRegistry, builtin_registry
from .transport import (
    HttpResponse,
    HttpTransport,
    RequestsTransport,
    TransportFailure,
    TransportTimeout,
)

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "LLMUsage",
    "MiniMaxLLMProvider",
    "MiniMaxSpeechProvider",
    "OpenAICompatibleLLMProvider",
    "OpenAICompatibleSpeechProvider",
    "ProbeResult",
    "ProviderAuthenticationError",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderInvalidRequestError",
    "ProviderModel",
    "ProviderProfile",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderResponseError",
    "ProviderTransportError",
    "ProviderUnsupportedError",
    "RequestsTransport",
    "SpeechEstimate",
    "SpeechProvider",
    "SpeechRequest",
    "SpeechResult",
    "TransportFailure",
    "TransportTimeout",
    "Voice",
    "builtin_registry",
]
