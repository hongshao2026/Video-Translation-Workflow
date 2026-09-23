"""Stable, user-safe errors shared by every provider adapter.

Provider exceptions intentionally do not retain request headers, request bodies,
or raw response bodies.  This keeps credentials and source material out of
tracebacks, logs, and serialized job state.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def redact_secrets(value: object, secrets: Iterable[str] = ()) -> str:
    """Return a bounded string with known secret values removed."""

    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:500]


class ProviderError(RuntimeError):
    """Base failure type that is safe to expose through the local API."""

    default_code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        code: str | None = None,
        status_code: int | None = None,
        trace_id: str | None = None,
        retryable: bool = False,
        uncertain_completion: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.code = code or self.default_code
        self.status_code = status_code
        self.trace_id = trace_id
        self.retryable = retryable
        self.uncertain_completion = uncertain_completion

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "code": self.code,
            "message": str(self),
            "status_code": self.status_code,
            "trace_id": self.trace_id,
            "retryable": self.retryable,
            "uncertain_completion": self.uncertain_completion,
        }


class ProviderAuthenticationError(ProviderError):
    default_code = "authentication_failed"


class ProviderRateLimitError(ProviderError):
    default_code = "rate_limited"


class ProviderInvalidRequestError(ProviderError):
    default_code = "invalid_request"


class ProviderUnsupportedError(ProviderError):
    default_code = "unsupported_capability"


class ProviderTransportError(ProviderError):
    default_code = "transport_error"


class ProviderResponseError(ProviderError):
    default_code = "invalid_response"
