"""Internal helpers for HTTP-backed provider implementations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
    redact_secrets,
)
from .transport import HttpResponse, HttpTransport, TransportFailure, TransportTimeout


class HttpProviderSupport:
    profile: Any
    _api_key: str
    _transport: HttpTransport

    def _url(self, path: str) -> str:
        return f"{self.profile.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        billable: bool,
        timeout: tuple[float, float] = (10, 120),
        extra_headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **dict(extra_headers or {}),
        }
        try:
            response = self._transport.request(
                method,
                self._url(path),
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except TransportTimeout as exc:
            raise ProviderTransportError(
                "服务请求超时；生成是否完成目前未知，请先核对供应商记录再重试。"
                if billable
                else "连接服务超时，请稍后重试。",
                provider_id=self.profile.provider_id,
                code="timeout",
                retryable=not billable,
                uncertain_completion=billable,
            ) from exc
        except TransportFailure as exc:
            raise ProviderTransportError(
                "无法连接服务，请检查网络和接口地址。",
                provider_id=self.profile.provider_id,
                retryable=not billable,
                uncertain_completion=billable,
            ) from exc

        if response.status_code < 400:
            return response
        trace_id = self._trace_id(response)
        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError(
                "API 凭证验证失败，请检查密钥和账号权限。",
                provider_id=self.profile.provider_id,
                status_code=response.status_code,
                trace_id=trace_id,
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "请求过于频繁或额度不足；本任务不会自动切换模型。",
                provider_id=self.profile.provider_id,
                status_code=response.status_code,
                trace_id=trace_id,
                retryable=True,
            )
        if response.status_code in {400, 404, 409, 422}:
            raise ProviderInvalidRequestError(
                self._safe_service_message(response)
                or "供应商拒绝了请求，请检查模型、接口和参数。",
                provider_id=self.profile.provider_id,
                status_code=response.status_code,
                trace_id=trace_id,
            )
        raise ProviderError(
            "供应商服务暂时不可用。",
            provider_id=self.profile.provider_id,
            code="service_error",
            status_code=response.status_code,
            trace_id=trace_id,
            retryable=response.status_code >= 500 and not billable,
            uncertain_completion=billable and response.status_code >= 500,
        )

    def _body_mapping(self, response: HttpResponse) -> Mapping[str, Any]:
        if not isinstance(response.body, Mapping):
            raise ProviderResponseError(
                "供应商返回了无法解析的数据。",
                provider_id=self.profile.provider_id,
                status_code=response.status_code,
                trace_id=self._trace_id(response),
            )
        return response.body

    def _safe_service_message(self, response: HttpResponse) -> str:
        body = response.body
        if not isinstance(body, Mapping):
            return ""
        error = body.get("error")
        if isinstance(error, Mapping):
            value = error.get("message")
        else:
            base_resp = body.get("base_resp")
            value = base_resp.get("status_msg") if isinstance(base_resp, Mapping) else ""
        return redact_secrets(value or "", (self._api_key,))

    @staticmethod
    def _trace_id(response: HttpResponse) -> str | None:
        body = response.body
        body_trace = body.get("trace_id") if isinstance(body, Mapping) else None
        header_trace = next(
            (
                value
                for key, value in response.headers.items()
                if key.lower() in {"trace-id", "x-request-id", "request-id"}
            ),
            None,
        )
        return str(body_trace or header_trace or "") or None
