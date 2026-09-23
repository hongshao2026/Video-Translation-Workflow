"""Small injectable HTTP transport used by provider adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests


class TransportTimeout(TimeoutError):
    """The remote outcome may be unknown for billable requests."""


class TransportFailure(ConnectionError):
    """The request could not be completed at the HTTP transport layer."""


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return self.body


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any] | None,
        timeout: tuple[float, float],
    ) -> HttpResponse:
        """Perform one request without logging its headers or body."""


class RequestsTransport:
    """Production transport. Tests should inject a deterministic fake."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any] | None,
        timeout: tuple[float, float],
    ) -> HttpResponse:
        try:
            response = self._session.request(
                method,
                url,
                headers=dict(headers),
                json=dict(json) if json is not None else None,
                timeout=timeout,
            )
        except requests.Timeout:
            # Do not retain the requests exception: it may hold a prepared
            # request object containing the Authorization header.
            raise TransportTimeout("provider request timed out") from None
        except requests.RequestException:
            raise TransportFailure("provider request failed") from None
        try:
            body = response.json()
        except ValueError:
            # Speech endpoints commonly return the encoded audio bytes rather
            # than JSON.  Keep the opaque payload without decoding or logging it.
            body = response.content
        return HttpResponse(
            status_code=response.status_code,
            body=body,
            headers=dict(response.headers),
        )
