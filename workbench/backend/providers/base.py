"""Abstract contracts implemented by every provider plugin."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    LLMRequest,
    LLMResult,
    ProbeResult,
    ProviderCapabilities,
    ProviderModel,
    ProviderProfile,
    SpeechEstimate,
    SpeechRequest,
    SpeechResult,
    Voice,
)


class _Provider(ABC):
    def __init__(
        self,
        profile: ProviderProfile,
        capabilities: ProviderCapabilities,
    ) -> None:
        if not profile.enabled:
            raise ValueError("Provider profile 已停用")
        if profile.provider_id != capabilities.provider_id:
            raise ValueError("Profile 与 capabilities 的 provider_id 不一致")
        if profile.kind != capabilities.kind:
            raise ValueError("Profile 与 capabilities 的 kind 不一致")
        self.profile = profile
        self.capabilities = capabilities

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(profile_id={self.profile.profile_id!r}, "
            f"provider_id={self.profile.provider_id!r})"
        )

    @abstractmethod
    def probe(self) -> ProbeResult:
        """Verify authentication using a non-generating endpoint."""


class LLMProvider(_Provider, ABC):
    @abstractmethod
    def list_models(self) -> list[ProviderModel]:
        """Return models visible to this credential."""

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResult:
        """Generate one non-streaming completion."""


class SpeechProvider(_Provider, ABC):
    @abstractmethod
    def list_voices(self) -> list[Voice]:
        """Return voices visible to this credential."""

    @abstractmethod
    def estimate(self, request: SpeechRequest) -> SpeechEstimate:
        """Estimate billable units without sending a generation request."""

    @abstractmethod
    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        """Generate one audio payload at native speed 1.0."""
