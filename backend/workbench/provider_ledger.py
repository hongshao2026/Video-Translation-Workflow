"""Durable, secret-free ledger wrappers for billable provider calls."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from backend.providers import (
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMUsage,
    ProviderModel,
    SpeechEstimate,
    SpeechProvider,
    SpeechRequest,
    SpeechResult,
    Voice,
)

from .database import WorkbenchDatabase
from .runner import UncertainPaidRequest


class ProviderRequestReplayBlocked(RuntimeError):
    """Raised when a previous call must be reconciled instead of resent."""


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_once(path: Path, payload: bytes) -> None:
    """Publish an immutable cache file, accepting only an identical retry."""

    path.parent.mkdir(parents=True, exist_ok=True)
    expected = _sha256_bytes(payload)
    if path.exists():
        if path.is_file() and _sha256_bytes(path.read_bytes()) == expected:
            return
        raise OSError(f"Provider 响应缓存已存在且内容不同：{path.name}")
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, delete=False, prefix=f".{path.name}.", suffix=".tmp"
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected:
                raise OSError(f"Provider 响应缓存并发冲突：{path.name}")
        except OSError:
            try:
                with temporary.open("rb") as source, path.open("xb") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
            except FileExistsError:
                if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected:
                    raise OSError(f"Provider 响应缓存并发冲突：{path.name}")
            except Exception:
                path.unlink(missing_ok=True)
                raise
    finally:
        temporary.unlink(missing_ok=True)


def _llm_input(provider: LLMProvider, request: LLMRequest) -> dict[str, Any]:
    return {
        "profile_id": provider.profile.profile_id,
        "provider_id": provider.profile.provider_id,
        "model": request.model or provider.profile.model,
        "messages": [message.request_dict() for message in request.messages],
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "response_schema": dict(request.response_schema or {}),
        "schema_name": request.schema_name,
        "extra": dict(request.extra),
    }


def _speech_input(provider: SpeechProvider, request: SpeechRequest) -> dict[str, Any]:
    return {
        "profile_id": provider.profile.profile_id,
        "provider_id": provider.profile.provider_id,
        "model": request.model or provider.profile.model,
        "text": request.text,
        "voice_id": request.voice_id,
        "audio_format": request.audio_format,
        "sample_rate": request.sample_rate,
        "bitrate": request.bitrate,
        "channel": request.channel,
        "speed": request.speed,
        "emotion": request.emotion,
        "language_boost": request.language_boost,
        "subtitle_type": request.subtitle_type,
        "extra": dict(request.extra),
    }


class _LedgerMixin:
    def __init__(self, database: WorkbenchDatabase, task_id: str | None) -> None:
        self._database = database
        self._task_id = task_id

    @property
    def _cache_root(self) -> Path:
        return self._database.path.resolve().parent / "provider_response_cache"

    def _cache_path(self, row: Mapping[str, Any], suffix: str) -> Path:
        return self._cache_root / f"{row['id']}.{suffix}"

    def _binding(self, path: Path) -> dict[str, Any]:
        state_root = self._database.path.resolve().parent
        resolved = path.resolve()
        if not resolved.is_relative_to(state_root) or not resolved.is_file():
            raise OSError("Provider 响应缓存路径越出状态目录或不存在")
        return {
            "path": resolved.relative_to(state_root).as_posix(),
            "sha256": _sha256_bytes(resolved.read_bytes()),
            "byte_size": resolved.stat().st_size,
        }

    def _persist_document(
        self,
        row: Mapping[str, Any],
        *,
        kind: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        document = {
            "schema_version": 1,
            "kind": kind,
            "provider_request_row_id": row["id"],
            "input_sha256": row["input_sha256"],
            "result": dict(result),
        }
        payload = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        path = self._cache_path(row, "json")
        _write_once(path, payload)
        return self._binding(path)

    def _load_document(
        self,
        row: Mapping[str, Any],
        *,
        kind: str,
        require_ledger_binding: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state_root = self._database.path.resolve().parent
        path = self._cache_path(row, "json").resolve()
        binding = self._binding(path)
        ledger_result = row.get("result")
        if require_ledger_binding and (
            not isinstance(ledger_result, Mapping) or ledger_result.get("cache") != binding
        ):
            raise ProviderRequestReplayBlocked("已完成请求缺少一致的响应缓存绑定")
        raw_path = Path(str(binding["path"]))
        if raw_path.is_absolute() or ".." in raw_path.parts:
            raise ProviderRequestReplayBlocked("Provider 响应缓存路径不安全")
        if path != (state_root / raw_path).resolve():
            raise ProviderRequestReplayBlocked("Provider 响应缓存路径绑定不一致")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderRequestReplayBlocked("Provider 响应缓存无法读取") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or document.get("kind") != kind
            or document.get("provider_request_row_id") != row["id"]
            or document.get("input_sha256") != row["input_sha256"]
            or not isinstance(document.get("result"), dict)
        ):
            raise ProviderRequestReplayBlocked("Provider 响应缓存与请求账本不匹配")
        return document, binding

    def _cached_or_blocked(
        self,
        row: dict[str, Any],
        *,
        kind: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if row.get("created"):
            return None
        status = str(row.get("status") or "")
        if status == "uncertain":
            raise UncertainPaidRequest(
                "已存在结果不确定的同一 Provider 请求，禁止自动重发",
                request_id=row.get("provider_request_id") or row["id"],
            )
        if status == "failed":
            raise ProviderRequestReplayBlocked(
                "同一 Provider 请求曾明确失败；需要修复证据后创建新任务"
            )
        try:
            document, binding = self._load_document(
                row,
                kind=kind,
                require_ledger_binding=status == "completed",
            )
        except (OSError, ProviderRequestReplayBlocked) as exc:
            if status == "sending":
                raise UncertainPaidRequest(
                    "已存在发送中的请求且没有可验证响应缓存，禁止自动重发",
                    request_id=row.get("provider_request_id") or row["id"],
                ) from exc
            raise ProviderRequestReplayBlocked(
                "同一 Provider 请求已经完成，但响应缓存缺失或损坏"
            ) from exc
        if status == "sending":
            result = document["result"]
            self._completed(
                row,
                resolved_model=result.get("model"),
                provider_request_id=result.get("request_id") or result.get("trace_id"),
                usage=result.get("usage") or {},
                result={"cache": binding},
                error={},
            )
        elif status != "completed":
            raise ProviderRequestReplayBlocked("Provider 请求状态无法恢复")
        return document, binding

    def _reserve(
        self,
        *,
        provider_id: str,
        profile_id: str,
        model: str,
        idempotency_key: str,
        input_sha256: str,
    ) -> dict[str, Any]:
        row = self._database.reserve_provider_request(
            task_id=self._task_id,
            profile_id=profile_id,
            provider_id=provider_id,
            requested_model=model,
            idempotency_key=idempotency_key,
            input_sha256=input_sha256,
        )
        if row.get("created"):
            return row
        return row

    def _failed(self, row: dict[str, Any], exc: Exception) -> None:
        uncertain = bool(getattr(exc, "uncertain_completion", False))
        try:
            self._database.update_provider_request(
                row["id"],
                status="uncertain" if uncertain else "failed",
                billing_state="uncertain" if uncertain else "rejected_or_failed",
                provider_request_id=getattr(exc, "trace_id", None),
                error={
                    "type": type(exc).__name__,
                    "code": str(getattr(exc, "code", type(exc).__name__)),
                    "automatic_retry": False,
                },
            )
        except Exception as persistence_error:
            # The remote call already happened, but its durable disposition was
            # not recorded.  Treat this as uncertain even when the original
            # service error looked definitive; retrying could otherwise race a
            # database recovery and issue a second billable request.
            raise UncertainPaidRequest(
                "Provider 请求结果无法写入本地账本，禁止自动重发",
                request_id=getattr(exc, "trace_id", None) or row["id"],
            ) from persistence_error

    def _completed(self, row: dict[str, Any], **values: Any) -> None:
        try:
            self._database.update_provider_request(
                row["id"], status="completed", billing_state="settled", **values
            )
        except Exception as persistence_error:
            # A successful response is already potentially billable.  If the
            # completion marker cannot be committed, fail closed so the task
            # can never blindly resend it.
            raise UncertainPaidRequest(
                "Provider 已返回结果但账本提交失败，禁止自动重发",
                request_id=str(values.get("provider_request_id") or row["id"]),
            ) from persistence_error


class LedgeredLLMProvider(_LedgerMixin, LLMProvider):
    def __init__(
        self,
        delegate: LLMProvider,
        database: WorkbenchDatabase,
        task_id: str | None,
    ) -> None:
        LLMProvider.__init__(self, delegate.profile, delegate.capabilities)
        _LedgerMixin.__init__(self, database, task_id)
        self._delegate = delegate

    def probe(self):
        return self._delegate.probe()

    def list_models(self) -> list[ProviderModel]:
        return self._delegate.list_models()

    def generate(self, request: LLMRequest) -> LLMResult:
        inputs = _llm_input(self._delegate, request)
        input_sha = _sha256(inputs)
        key = request.idempotency_key or f"llm-{self.profile.profile_id}-{input_sha[:40]}"
        effective = request if request.idempotency_key else replace(request, idempotency_key=key)
        model = str(effective.model or self.profile.model or "")
        row = self._reserve(
            provider_id=self.profile.provider_id,
            profile_id=self.profile.profile_id,
            model=model,
            idempotency_key=key,
            input_sha256=input_sha,
        )
        cached = self._cached_or_blocked(row, kind="llm")
        if cached is not None:
            result = cached[0]["result"]
            usage = result.get("usage") or {}
            return LLMResult(
                text=str(result.get("text") or ""),
                provider_id=str(result.get("provider_id") or self.profile.provider_id),
                model=str(result.get("model") or model),
                request_id=str(result.get("request_id") or "") or None,
                finish_reason=str(result.get("finish_reason") or "") or None,
                usage=LLMUsage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                ),
                structured=result.get("structured"),
            )
        try:
            result = self._delegate.generate(effective)
        except Exception as exc:
            self._failed(row, exc)
            raise
        result_payload = {
            "text": result.text,
            "structured": result.structured,
            "provider_id": result.provider_id,
            "model": result.model,
            "request_id": result.request_id,
            "finish_reason": result.finish_reason,
            "usage": asdict(result.usage),
        }
        try:
            cache = self._persist_document(row, kind="llm", result=result_payload)
        except Exception as persistence_error:
            raise UncertainPaidRequest(
                "Provider 已返回结果但响应缓存写入失败，禁止自动重发",
                request_id=result.request_id or row["id"],
            ) from persistence_error
        self._completed(
            row,
            resolved_model=result.model,
            provider_request_id=result.request_id,
            usage=asdict(result.usage),
            result={"cache": cache},
            error={},
        )
        return result


class LedgeredSpeechProvider(_LedgerMixin, SpeechProvider):
    def __init__(
        self,
        delegate: SpeechProvider,
        database: WorkbenchDatabase,
        task_id: str | None,
    ) -> None:
        SpeechProvider.__init__(self, delegate.profile, delegate.capabilities)
        _LedgerMixin.__init__(self, database, task_id)
        self._delegate = delegate

    def probe(self):
        return self._delegate.probe()

    def list_voices(self) -> list[Voice]:
        return self._delegate.list_voices()

    def estimate(self, request: SpeechRequest) -> SpeechEstimate:
        return self._delegate.estimate(request)

    def _replay_result(
        self,
        row: dict[str, Any],
        *,
        request: SpeechRequest,
        model: str,
    ) -> SpeechResult:
        cached = self._cached_or_blocked(row, kind="speech")
        if cached is None:
            raise ProviderRequestReplayBlocked("语音请求尚未产生可恢复结果")
        result = cached[0]["result"]
        audio_binding = result.get("audio")
        if not isinstance(audio_binding, Mapping):
            raise ProviderRequestReplayBlocked("语音响应缓存缺少音频绑定")
        state_root = self._database.path.resolve().parent
        raw_path = Path(str(audio_binding.get("path") or ""))
        audio_path = (state_root / raw_path).resolve()
        if (
            not str(raw_path)
            or raw_path.is_absolute()
            or ".." in raw_path.parts
            or not audio_path.is_relative_to(state_root)
            or not audio_path.is_file()
            or _sha256_bytes(audio_path.read_bytes()) != audio_binding.get("sha256")
            or audio_path.stat().st_size != int(audio_binding.get("byte_size") or -1)
        ):
            raise ProviderRequestReplayBlocked("语音响应缓存音频缺失或哈希不匹配")
        return SpeechResult(
            audio=audio_path.read_bytes(),
            audio_format=str(result.get("audio_format") or request.audio_format),
            provider_id=str(result.get("provider_id") or self.profile.provider_id),
            model=str(result.get("model") or model),
            voice_id=str(result.get("voice_id") or request.voice_id),
            request_id=str(result.get("request_id") or "") or None,
            trace_id=str(result.get("trace_id") or "") or None,
            usage_units=(
                int(result["usage_units"])
                if result.get("usage_units") is not None
                else None
            ),
            subtitle_file=(
                str(result.get("subtitle_file"))
                if result.get("subtitle_file") is not None
                else None
            ),
        )

    def replay(self, request: SpeechRequest) -> SpeechResult:
        """Return a previously billed result without ever contacting the provider."""

        inputs = _speech_input(self._delegate, request)
        input_sha = _sha256(inputs)
        key = request.idempotency_key or f"speech-{self.profile.profile_id}-{input_sha[:40]}"
        model = str(request.model or self.profile.model or "")
        row = self._database.find_provider_request(self.profile.provider_id, key)
        if row is None:
            raise UncertainPaidRequest(
                "本地 manifest 标记为已发送，但请求账本中没有对应记录，禁止自动重发"
            )
        if str(row.get("input_sha256") or "") != input_sha:
            raise ProviderRequestReplayBlocked("语音请求账本的输入哈希不匹配")
        row["created"] = False
        return self._replay_result(row, request=request, model=model)

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        inputs = _speech_input(self._delegate, request)
        input_sha = _sha256(inputs)
        key = request.idempotency_key or f"speech-{self.profile.profile_id}-{input_sha[:40]}"
        effective = request if request.idempotency_key else replace(request, idempotency_key=key)
        model = str(effective.model or self.profile.model or "")
        row = self._reserve(
            provider_id=self.profile.provider_id,
            profile_id=self.profile.profile_id,
            model=model,
            idempotency_key=key,
            input_sha256=input_sha,
        )
        if not row.get("created"):
            return self._replay_result(row, request=effective, model=model)
        try:
            result = self._delegate.synthesize(effective)
        except Exception as exc:
            self._failed(row, exc)
            raise
        try:
            audio_path = self._cache_path(row, "audio")
            _write_once(audio_path, result.audio)
            result_payload = {
                "audio": self._binding(audio_path),
                "audio_format": result.audio_format,
                "provider_id": result.provider_id,
                "model": result.model,
                "voice_id": result.voice_id,
                "request_id": result.request_id,
                "trace_id": result.trace_id,
                "usage_units": result.usage_units,
                "subtitle_file": result.subtitle_file,
                "usage": {"units": result.usage_units},
            }
            cache = self._persist_document(row, kind="speech", result=result_payload)
        except Exception as persistence_error:
            raise UncertainPaidRequest(
                "Provider 已返回语音但响应缓存写入失败，禁止自动重发",
                request_id=result.request_id or result.trace_id or row["id"],
            ) from persistence_error
        self._completed(
            row,
            resolved_model=result.model,
            provider_request_id=result.request_id or result.trace_id,
            usage={"units": result.usage_units},
            result={"cache": cache},
            error={},
        )
        return result


def with_provider_ledger(
    provider: LLMProvider | SpeechProvider,
    database: WorkbenchDatabase,
    task_id: str | None,
) -> LLMProvider | SpeechProvider:
    if isinstance(provider, LLMProvider):
        return LedgeredLLMProvider(provider, database, task_id)
    if isinstance(provider, SpeechProvider):
        return LedgeredSpeechProvider(provider, database, task_id)
    raise TypeError("不支持的 Provider 类型")
