"""Provider-neutral, checkpointed TTS generation at native speed 1.0."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backend.providers import SpeechProvider, SpeechRequest

from .library import atomic_json, sha256_file
from .runner import UncertainPaidRequest

SEGMENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SpeechProgress = Callable[[int, int, Mapping[str, Any]], None]


class SpeechManifestError(RuntimeError):
    pass


def _input_hash(provider: SpeechProvider, segment: Mapping[str, Any], model: str) -> str:
    value = {
        "provider_id": provider.profile.provider_id,
        "profile_id": provider.profile.profile_id,
        "model": model,
        "segment_id": str(segment["segment_id"]),
        "voice_id": str(segment["voice_id"]),
        "text": str(segment["text"]),
        "speed": 1.0,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False, suffix=".tmp") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


class SpeechPipeline:
    def __init__(self, provider: SpeechProvider, output_dir: Path) -> None:
        self.provider = provider
        self.output_dir = Path(output_dir)
        self.manifest_path = self.output_dir / "manifest.json"

    def dry_run(self, segments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        normalized = self._normalize(segments)
        estimates = []
        total_units = 0
        total_cost = 0.0
        currency: str | None = None
        cost_complete = True
        for row in normalized:
            request = self._request(row)
            estimate = self.provider.estimate(request)
            estimates.append({"segment_id": row["segment_id"], **asdict(estimate)})
            total_units += estimate.billable_units
            if estimate.estimated_cost is None:
                cost_complete = False
            else:
                total_cost += estimate.estimated_cost
            currency = currency or estimate.currency
        return {
            "status": "pass",
            "provider_id": self.provider.profile.provider_id,
            "profile_id": self.provider.profile.profile_id,
            "model": self.provider.profile.model,
            "segment_count": len(normalized),
            "billable_units": total_units,
            "unit_name": estimates[0]["unit_name"] if estimates else "unknown",
            "estimated_cost": round(total_cost, 8) if cost_complete else None,
            "currency": currency,
            "speed": 1.0,
            "offline_rate": 1.0,
            "items": estimates,
        }

    def synthesize(
        self,
        segments: Sequence[Mapping[str, Any]],
        *,
        authorization: Mapping[str, Any],
        authorization_scope: str = "full_tts_current_inputs",
        progress: SpeechProgress | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize(segments)
        self._validate_authorization(
            normalized,
            authorization,
            expected_scope=authorization_scope,
        )
        progress = progress or (lambda _current, _total, _row: None)
        manifest = self._load_or_create(normalized, authorization)
        model = str(self.provider.profile.model or "")
        if not model:
            raise SpeechManifestError("语音 Provider 没有锁定模型")

        for index, row in enumerate(normalized, 1):
            entry = manifest["segments"][row["segment_id"]]
            if entry["status"] == "ready":
                audio_path = self.output_dir / entry["audio_path"]
                if audio_path.is_file() and sha256_file(audio_path) == entry["sha256"]:
                    progress(index, len(normalized), entry)
                    continue
                entry["status"] = "repair_required"
                entry["error"] = "cached_audio_hash_mismatch"
                atomic_json(self.manifest_path, manifest)
                raise SpeechManifestError(f"缓存语音哈希不匹配：{row['segment_id']}")
            if entry["status"] == "uncertain":
                raise UncertainPaidRequest(
                    f"语音片段 {row['segment_id']} 的上次付费请求状态不确定",
                    request_id=entry.get("provider_request_id") or entry.get("trace_id"),
                )
            request = self._request(row)
            if entry["status"] in {"sending", "accepted"}:
                replay = getattr(self.provider, "replay", None)
                if not callable(replay):
                    raise UncertainPaidRequest(
                        f"语音片段 {row['segment_id']} 已发送但 Provider 不支持安全重放"
                    )
                result = replay(request)
            else:
                entry.update(status="sending", automatic_retry=False)
                atomic_json(self.manifest_path, manifest)
                try:
                    result = self.provider.synthesize(request)
                except Exception as exc:
                    if getattr(exc, "uncertain_completion", False):
                        entry.update(
                            status="uncertain",
                            error_code=str(getattr(exc, "code", type(exc).__name__)),
                            trace_id=getattr(exc, "trace_id", None),
                            automatic_retry=False,
                        )
                        atomic_json(self.manifest_path, manifest)
                        raise UncertainPaidRequest(
                            f"语音片段 {row['segment_id']} 的响应状态不确定",
                            request_id=getattr(exc, "trace_id", None),
                        ) from exc
                    entry.update(
                        status="failed",
                        error_code=str(getattr(exc, "code", type(exc).__name__)),
                        automatic_retry=False,
                    )
                    atomic_json(self.manifest_path, manifest)
                    raise
            if not result.audio:
                entry.update(status="failed", error_code="empty_audio", automatic_retry=False)
                atomic_json(self.manifest_path, manifest)
                raise SpeechManifestError("语音 Provider 返回了空音频")
            extension = re.sub(r"[^a-z0-9]", "", result.audio_format.lower()) or "bin"
            relative = Path("raw") / f"{row['segment_id']}.{extension}"
            audio_path = self.output_dir / relative
            _atomic_bytes(audio_path, result.audio)
            entry.update(
                status="ready",
                audio_path=relative.as_posix(),
                sha256=sha256_file(audio_path),
                byte_size=audio_path.stat().st_size,
                actual_format=result.audio_format,
                resolved_model=result.model,
                provider_request_id=result.request_id,
                trace_id=result.trace_id,
                usage_units=result.usage_units,
                subtitle_file=result.subtitle_file,
                automatic_retry=False,
            )
            atomic_json(self.manifest_path, manifest)
            progress(index, len(normalized), entry)

        manifest["status"] = "ready"
        manifest["ready_count"] = len(normalized)
        atomic_json(self.manifest_path, manifest)
        return manifest

    def _normalize(self, segments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not segments:
            raise SpeechManifestError("没有需要合成的语音片段")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in segments:
            segment_id = str(value.get("segment_id") or "").strip()
            text = str(value.get("text") or "").strip()
            voice_id = str(value.get("voice_id") or "").strip()
            if not SEGMENT_ID.fullmatch(segment_id) or segment_id in seen:
                raise SpeechManifestError("语音片段 ID 缺失、重复或不安全")
            if not text or not voice_id:
                raise SpeechManifestError(f"语音片段缺少文本或音色：{segment_id}")
            if value.get("speed", 1.0) != 1.0:
                raise SpeechManifestError("正式中文 TTS 必须使用原生 speed=1.0")
            seen.add(segment_id)
            normalized.append(
                {
                    "segment_id": segment_id,
                    "text": text,
                    "voice_id": voice_id,
                    "model": str(value.get("model") or self.provider.profile.model or ""),
                    "audio_format": str(value.get("audio_format") or "mp3"),
                    "sample_rate": int(value.get("sample_rate") or 32000),
                    "bitrate": int(value.get("bitrate") or 128000),
                    "channel": int(value.get("channel") or 1),
                    "emotion": value.get("emotion"),
                    "language_boost": str(value.get("language_boost") or "Chinese"),
                }
            )
        return normalized

    def _request(self, row: Mapping[str, Any]) -> SpeechRequest:
        return SpeechRequest(
            text=str(row["text"]),
            voice_id=str(row["voice_id"]),
            model=str(row["model"]),
            audio_format=str(row["audio_format"]),
            sample_rate=int(row["sample_rate"]),
            bitrate=int(row["bitrate"]),
            channel=int(row["channel"]),
            speed=1.0,
            emotion=str(row["emotion"]) if row.get("emotion") else None,
            language_boost=str(row["language_boost"]),
            idempotency_key=_input_hash(self.provider, row, str(row["model"])),
        )

    def _validate_authorization(
        self,
        segments: Sequence[Mapping[str, Any]],
        authorization: Mapping[str, Any],
        *,
        expected_scope: str,
    ) -> None:
        if expected_scope not in {
            "full_tts_current_inputs",
            "audition_current_inputs",
        }:
            raise SpeechManifestError("不支持的语音授权范围")
        frozen_hash = hashlib.sha256(
            json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            authorization.get("status") != "pass"
            or authorization.get("scope") != expected_scope
            or authorization.get("segments_sha256") != frozen_hash
        ):
            raise SpeechManifestError("语音授权范围或冻结输入绑定无效")

    def _load_or_create(
        self,
        segments: Sequence[Mapping[str, Any]],
        authorization: Mapping[str, Any],
    ) -> dict[str, Any]:
        model = str(self.provider.profile.model or "")
        locks = {
            "provider_id": self.provider.profile.provider_id,
            "profile_id": self.provider.profile.profile_id,
            "model": model,
            "speed": 1.0,
            "offline_rate": 1.0,
            "authorization_scope": authorization["scope"],
            "segments_sha256": authorization["segments_sha256"],
        }
        if self.manifest_path.is_file():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if manifest.get("locks") != locks:
                raise SpeechManifestError("现有 TTS manifest 与当前 Provider、模型或输入不匹配，请创建新版本目录")
            return manifest
        manifest = {
            "schema_version": 1,
            "status": "prepared",
            "locks": locks,
            "ready_count": 0,
            "segments": {
                row["segment_id"]: {
                    "segment_id": row["segment_id"],
                    "input_sha256": _input_hash(self.provider, row, model),
                    "status": "prepared",
                    "automatic_retry": False,
                }
                for row in segments
            },
        }
        atomic_json(self.manifest_path, manifest)
        return manifest


def authorization_for_segments(
    provider: SpeechProvider,
    segments: Sequence[Mapping[str, Any]],
    *,
    scope: str = "full_tts_current_inputs",
) -> dict[str, Any]:
    """Build the deterministic part of a user-command-bound authorization record."""
    if scope not in {"full_tts_current_inputs", "audition_current_inputs"}:
        raise SpeechManifestError("不支持的语音授权范围")
    normalized = SpeechPipeline(provider, Path("."))._normalize(segments)
    return {
        "status": "pass",
        "scope": scope,
        "segments_sha256": hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
