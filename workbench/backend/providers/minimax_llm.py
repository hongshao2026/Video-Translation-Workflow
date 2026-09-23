"""MiniMax text adapter using its Chat Completions compatible endpoint."""

from __future__ import annotations

from .models import ProviderProfile
from .openai_compatible import OpenAICompatibleLLMProvider
from .transport import HttpTransport


class MiniMaxLLMProvider(OpenAICompatibleLLMProvider):
    """MiniMax-specific route defaults without hard-coding a model revision."""

    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        options = dict(profile.options)
        # MiniMax's current OpenAI-compatible API uses the standard Chat
        # Completions route. Keep the option overridable for compatible
        # international or self-hosted endpoints.
        options.setdefault("chat_path", "/chat/completions")
        options.setdefault("models_path", "/models")
        options.setdefault("model_listing", True)
        options.setdefault("max_tokens_field", "max_completion_tokens")
        # Default to prompt-constrained JSON plus strict local parsing.  A
        # profile may opt into a vendor-native response format after testing.
        options.setdefault("structured_output_mode", "prompt_json")
        normalized = ProviderProfile(
            profile_id=profile.profile_id,
            provider_id=profile.provider_id,
            kind=profile.kind,
            base_url=profile.base_url,
            model=profile.model,
            credential_ref=profile.credential_ref,
            options=options,
            enabled=profile.enabled,
        )
        super().__init__(normalized, api_key, transport=transport)
