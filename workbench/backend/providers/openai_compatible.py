"""OpenAI Chat Completions compatible text provider."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from .base import LLMProvider
from .errors import ProviderResponseError, ProviderUnsupportedError
from .http_support import HttpProviderSupport
from .models import (
    LLMRequest,
    LLMResult,
    LLMUsage,
    ProbeResult,
    ProviderCapabilities,
    ProviderModel,
    ProviderProfile,
)
from .transport import HttpTransport, RequestsTransport


class OpenAICompatibleLLMProvider(HttpProviderSupport, LLMProvider):
    """Adapter for the conservative common subset of Chat Completions APIs.

    Capability flags come from the profile instead of being guessed.  A server
    that does not explicitly declare structured output support will never
    silently receive an ignored ``response_format`` parameter.
    """

    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        if profile.kind != "llm":
            raise ValueError("OpenAI-compatible profile 必须是 llm")
        key = api_key.strip()
        if not key:
            raise ValueError("API Key 不能为空")
        structured_mode = str(
            profile.options.get("structured_output_mode", "none")
        )
        if structured_mode not in {"none", "prompt_json", "json_object", "json_schema"}:
            raise ValueError("structured_output_mode 配置无效")
        capabilities = ProviderCapabilities(
            provider_id=profile.provider_id,
            kind="llm",
            model_listing=bool(profile.options.get("model_listing", True)),
            structured_outputs=structured_mode in {"prompt_json", "json_object", "json_schema"},
            json_mode=structured_mode in {"json_object", "json_schema"},
            tool_calls=bool(profile.options.get("tool_calls", False)),
            multimodal_input=bool(profile.options.get("multimodal_input", False)),
        )
        LLMProvider.__init__(self, profile, capabilities)
        self._api_key = key
        self._transport = transport or RequestsTransport()
        self._structured_mode = structured_mode

    @property
    def _models_path(self) -> str:
        return str(self.profile.options.get("models_path", "/models"))

    @property
    def _chat_path(self) -> str:
        return str(self.profile.options.get("chat_path", "/chat/completions"))

    def probe(self) -> ProbeResult:
        if not self.capabilities.model_listing:
            raise ProviderUnsupportedError(
                "此 Provider 未配置非计费的模型列表探测端点。",
                provider_id=self.profile.provider_id,
            )
        started = time.perf_counter()
        models = self.list_models()
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        return ProbeResult(
            ok=True,
            provider_id=self.profile.provider_id,
            profile_id=self.profile.profile_id,
            latency_ms=latency_ms,
            capabilities=self.capabilities,
            message="连接与凭证验证成功",
            model_count=len(models),
        )

    def list_models(self) -> list[ProviderModel]:
        if not self.capabilities.model_listing:
            raise ProviderUnsupportedError(
                "此 Provider 不支持模型列表。",
                provider_id=self.profile.provider_id,
            )
        response = self._request(
            "GET",
            self._models_path,
            billable=False,
            timeout=(8, 30),
        )
        body = self._body_mapping(response)
        values = body.get("data")
        if not isinstance(values, list):
            raise ProviderResponseError(
                "模型列表响应缺少 data 数组。",
                provider_id=self.profile.provider_id,
                trace_id=self._trace_id(response),
            )
        models: list[ProviderModel] = []
        for value in values:
            if not isinstance(value, Mapping) or not value.get("id"):
                continue
            models.append(
                ProviderModel(
                    model_id=str(value["id"]),
                    label=str(value.get("name")) if value.get("name") else None,
                    owned_by=(
                        str(value.get("owned_by")) if value.get("owned_by") else None
                    ),
                )
            )
        return models

    def generate(self, request: LLMRequest) -> LLMResult:
        model = (request.model or self.profile.model or "").strip()
        if not model:
            raise ValueError("必须在 Provider profile 或请求中指定模型")
        messages = [message.request_dict() for message in request.messages]
        if request.response_schema is not None and self._structured_mode == "prompt_json":
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "Return only one JSON value that conforms exactly to this JSON Schema: "
                        + json.dumps(
                            dict(request.response_schema),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                },
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            max_field = str(self.profile.options.get("max_tokens_field", "max_tokens"))
            if max_field not in {"max_tokens", "max_completion_tokens"}:
                raise ValueError("max_tokens_field 配置无效")
            payload[max_field] = request.max_output_tokens
        if request.response_schema is not None:
            if self._structured_mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "strict": True,
                        "schema": dict(request.response_schema),
                    },
                }
            elif self._structured_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            elif self._structured_mode != "prompt_json":
                raise ProviderUnsupportedError(
                    "此 Provider 未声明结构化输出能力，不能用于正式翻译门禁。",
                    provider_id=self.profile.provider_id,
                )
        protected = {"model", "messages", "stream", "response_format"}
        overlaps = protected.intersection(request.extra)
        if overlaps:
            raise ValueError(f"LLM extra 不能覆盖核心字段：{', '.join(sorted(overlaps))}")
        payload.update(request.extra)
        headers = (
            {"Idempotency-Key": request.idempotency_key}
            if request.idempotency_key
            else None
        )
        response = self._request(
            "POST",
            self._chat_path,
            payload=payload,
            billable=True,
            timeout=(10, 180),
            extra_headers=headers,
        )
        body = self._body_mapping(response)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError(
                "文本生成响应缺少 choices。",
                provider_id=self.profile.provider_id,
                trace_id=self._trace_id(response),
            )
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderResponseError(
                "文本生成响应的 choice 结构不正确。",
                provider_id=self.profile.provider_id,
                trace_id=self._trace_id(response),
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        text = self._content_text(content)
        if not text:
            raise ProviderResponseError(
                "供应商没有返回文本内容。",
                provider_id=self.profile.provider_id,
                trace_id=self._trace_id(response),
            )
        structured: Any = None
        if request.response_schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderResponseError(
                    "结构化输出不是有效 JSON，结果不能进入正式门禁。",
                    provider_id=self.profile.provider_id,
                    trace_id=self._trace_id(response),
                ) from exc
        usage_value = body.get("usage")
        usage = usage_value if isinstance(usage_value, Mapping) else {}
        input_tokens = self._integer(usage.get("prompt_tokens") or usage.get("input_tokens"))
        output_tokens = self._integer(
            usage.get("completion_tokens") or usage.get("output_tokens")
        )
        return LLMResult(
            text=text,
            provider_id=self.profile.provider_id,
            model=str(body.get("model") or model),
            request_id=str(body.get("id") or "") or None,
            finish_reason=str(choice.get("finish_reason") or "") or None,
            usage=LLMUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=self._integer(usage.get("total_tokens"))
                or input_tokens + output_tokens,
            ),
            structured=structured,
        )

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}:
                    value = item.get("text")
                    if isinstance(value, str):
                        parts.append(value)
            return "".join(parts).strip()
        return ""
