"""Provider-neutral T/A/B/C translation orchestration with deterministic checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backend.providers import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMUsage,
)

from .library import atomic_json

ProgressCallback = Callable[[str, int, int, str], None]
ResultValidator = Callable[[Any], Any]


class TranslationValidationError(RuntimeError):
    pass


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_text(slot: Mapping[str, Any]) -> str:
    return str(slot.get("source_text") or slot.get("source") or slot.get("text") or "").strip()


def normalize_slots(slots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not slots:
        raise TranslationValidationError("冻结源文不能为空")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, value in enumerate(slots):
        stable_id = str(value.get("id") or "").strip()
        source = _source_text(value)
        if not stable_id or stable_id in seen:
            raise TranslationValidationError("稳定 ID 缺失或重复")
        if not source:
            raise TranslationValidationError(f"源文为空：{stable_id}")
        seen.add(stable_id)
        rows.append(
            {
                "id": stable_id,
                "source_text": source,
                "speaker": value.get("speaker") or value.get("role_id"),
                "start": value.get("start"),
                "end": value.get("end"),
                "context": value.get("context"),
                "position": position,
            }
        )
    return rows


T_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "subtitle_zh"],
                "properties": {
                    "id": {"type": "string"},
                    "subtitle_zh": {"type": "string"},
                    "uncertainty": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}


REVIEW_SCHEMA = {
    "type": "object",
    "required": ["covered_ids", "issues"],
    "properties": {
        "covered_ids": {"type": "array", "items": {"type": "string"}},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity", "category", "suggestion", "rationale"],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


C_SCHEMA = {
    "type": "object",
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "action", "final_text", "rationale"],
                "properties": {
                    "id": {"type": "string"},
                    "action": {"enum": ["keep", "replace"]},
                    "final_text": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}


class TranslationPipeline:
    """Runs isolated model roles; deterministic code remains the workflow owner."""

    def __init__(
        self,
        providers: Mapping[str, LLMProvider],
        prompt_pack: Mapping[str, Any],
        *,
        batch_size: int = 40,
    ) -> None:
        missing = {"T", "A", "B", "C"} - set(providers)
        if missing:
            raise ValueError(f"缺少翻译角色 Provider：{sorted(missing)}")
        if batch_size < 1 or batch_size > 200:
            raise ValueError("batch_size 必须在 1–200 之间")
        self.providers = dict(providers)
        self.prompt_pack = dict(prompt_pack)
        self.batch_size = batch_size

    def run(
        self,
        slots: Sequence[Mapping[str, Any]],
        *,
        glossary: Mapping[str, str] | None = None,
        project_context: str = "",
        output_dir: Path | None = None,
        version: int = 1,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        rows = normalize_slots(slots)
        if version < 1:
            raise TranslationValidationError("翻译版本号必须大于 0")
        ids = [row["id"] for row in rows]
        source_hash = canonical_sha256(rows)
        glossary = {str(key): str(value) for key, value in (glossary or {}).items()}
        glossary_hash = canonical_sha256(glossary)
        progress = progress or (lambda _role, _current, _total, _detail: None)
        checkpoint_dir = (
            Path(output_dir) / "work" / "translation_checkpoints" / f"v{version}"
            if output_dir is not None
            else None
        )

        translated: list[dict[str, Any]] = []
        t_requests: list[dict[str, Any]] = []
        for offset in range(0, len(rows), self.batch_size):
            batch = rows[offset : offset + self.batch_size]
            payload = {
                "project_context": project_context,
                "glossary": glossary,
                "items": [{key: value for key, value in row.items() if key != "position"} for row in batch],
            }
            expected_ids = [row["id"] for row in batch]
            result = self._call(
                "T",
                payload,
                T_SCHEMA,
                checkpoint_path=(
                    checkpoint_dir / f"t_batch_{offset // self.batch_size + 1:04d}.json"
                    if checkpoint_dir is not None
                    else None
                ),
                validate_result=lambda value, expected=expected_ids: self._validate_translation(
                    value, expected
                ),
            )
            items = self._validate_translation(result, expected_ids)
            translated.extend(items)
            t_requests.append(self._result_evidence(result))
            progress("T", len(translated), len(rows), "翻译 T 已验证稳定 ID")

        candidate = [
            {
                **row,
                "subtitle_zh": translated[index]["subtitle_zh"],
                "uncertainty": translated[index].get("uncertainty"),
            }
            for index, row in enumerate(rows)
        ]
        candidate_hash = canonical_sha256(candidate)

        def review(role: str) -> dict[str, Any]:
            covered: list[str] = []
            issues: list[dict[str, Any]] = []
            requests: list[dict[str, Any]] = []
            for offset in range(0, len(candidate), self.batch_size):
                batch = candidate[offset : offset + self.batch_size]
                expected_ids = [row["id"] for row in batch]
                result = self._call(
                    role,
                    {
                        "project_context": project_context,
                        "glossary": glossary,
                        "candidate_sha256": candidate_hash,
                        "items": batch,
                    },
                    REVIEW_SCHEMA,
                    checkpoint_path=(
                        checkpoint_dir
                        / f"{role.lower()}_batch_{offset // self.batch_size + 1:04d}.json"
                        if checkpoint_dir is not None
                        else None
                    ),
                    validate_result=lambda value, expected=expected_ids: self._validate_review(
                        value, expected
                    ),
                )
                report = self._validate_review(result, expected_ids)
                covered.extend(report["covered_ids"])
                issues.extend(report["issues"])
                requests.append(self._result_evidence(result))
                progress(role, len(covered), len(candidate), f"审核 {role} 已验证稳定 ID")
            if covered != ids:
                raise TranslationValidationError(f"审核 {role} 未按顺序完整覆盖全文")
            return {
                "role": role,
                "candidate_sha256": candidate_hash,
                "covered_ids": covered,
                "missing_ids": [],
                "issues": issues,
                "requests": requests,
            }

        # A and B receive the same frozen candidate and cannot read each other's output.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="translation-review") as executor:
            future_a = executor.submit(review, "A")
            future_b = executor.submit(review, "B")
            audit_a = future_a.result()
            audit_b = future_b.result()

        issue_rows = [
            {"reviewer": report["role"], **issue}
            for report in (audit_a, audit_b)
            for issue in report["issues"]
        ]
        result_c = self._call(
            "C",
            {
                "project_context": project_context,
                "glossary": glossary,
                "candidate_sha256": candidate_hash,
                "candidate": candidate,
                "issues": issue_rows,
            },
            C_SCHEMA,
            checkpoint_path=(checkpoint_dir / "c_adjudication.json" if checkpoint_dir else None),
            validate_result=lambda value: self._validate_decisions(
                value, candidate, issue_rows
            ),
        )
        decisions = self._validate_decisions(result_c, candidate, issue_rows)
        by_id = {row["id"]: row for row in decisions}
        final: list[dict[str, Any]] = []
        for row in candidate:
            decision = by_id.get(row["id"])
            text = row["subtitle_zh"]
            if decision and decision["action"] == "replace":
                text = decision["final_text"]
            if not str(text).strip():
                raise TranslationValidationError(f"裁决产生空译文：{row['id']}")
            final.append({**row, "subtitle_zh": text})
        final_hash = canonical_sha256(final)
        progress("C", len(final), len(final), "裁决结果已完成确定性回归")

        evidence = {
            "schema_version": 1,
            "status": "candidate_ready_for_chapter_reading",
            "source_sha256": source_hash,
            "glossary_sha256": glossary_hash,
            "candidate_sha256": candidate_hash,
            "final_translation_sha256": final_hash,
            "slot_count": len(rows),
            "stable_ids": ids,
            "missing_ids": [],
            "role_execution_isolated": True,
            "reviewers": {"A": audit_a, "B": audit_b},
            "translator_requests": t_requests,
            "adjudication": {
                "issues": issue_rows,
                "decisions": decisions,
                "request": self._result_evidence(result_c),
            },
            "translation": final,
        }
        if output_dir is not None:
            self._write_artifacts(output_dir, version, rows, candidate, audit_a, audit_b, decisions, final, evidence)
        return evidence

    def _call(
        self,
        role: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
        *,
        checkpoint_path: Path | None = None,
        validate_result: ResultValidator | None = None,
    ) -> LLMResult:
        role_definition = (self.prompt_pack.get("roles") or {}).get(role) or {}
        common = "\n".join(str(item) for item in self.prompt_pack.get("common_policy", []))
        system = "\n".join(
            value
            for value in (
                common,
                str(role_definition.get("purpose") or ""),
                str(role_definition.get("isolation") or ""),
                "只返回符合 Schema 的 JSON，不要添加 Markdown。",
            )
            if value
        )
        profile = getattr(self.providers[role], "profile", None)
        profile_id = str(getattr(profile, "profile_id", None) or f"role-{role.lower()}")
        request_signature = canonical_sha256(
            {
                "role": role,
                "profile_id": profile_id,
                "provider_id": str(getattr(profile, "provider_id", "") or ""),
                "model": str(getattr(profile, "model", "") or ""),
                "system": system,
                "payload": payload,
                "schema": schema,
            }
        )
        if checkpoint_path is not None and checkpoint_path.is_file():
            result = self._load_checkpoint(
                checkpoint_path,
                role=role,
                request_signature=request_signature,
            )
            if validate_result is not None:
                validate_result(result)
            return result
        request = LLMRequest.from_messages(
            [
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            ],
            response_schema=schema,
            schema_name=f"translation_role_{role.lower()}",
            idempotency_key=(
                f"translation-{profile_id}-{role.lower()}-"
                f"{request_signature[:40]}"
            ),
        )
        result = self.providers[role].generate(request)
        if not isinstance(result.structured, Mapping):
            raise TranslationValidationError(f"角色 {role} 没有返回可验证的结构化结果")
        if validate_result is not None:
            validate_result(result)
        if checkpoint_path is not None:
            self._write_checkpoint(
                checkpoint_path,
                role=role,
                request_signature=request_signature,
                result=result,
            )
        return result

    @staticmethod
    def _write_checkpoint(
        path: Path,
        *,
        role: str,
        request_signature: str,
        result: LLMResult,
    ) -> None:
        structured = dict(result.structured)
        result_payload = {
            "structured": structured,
            "provider_id": result.provider_id,
            "model": result.model,
            "request_id": result.request_id,
            "finish_reason": result.finish_reason,
            "usage": asdict(result.usage),
        }
        atomic_json(
            path,
            {
                "schema_version": 1,
                "status": "validated",
                "role": role,
                "request_sha256": request_signature,
                "output_sha256": canonical_sha256(structured),
                "result_sha256": canonical_sha256(result_payload),
                "result": result_payload,
            },
        )

    @staticmethod
    def _load_checkpoint(
        path: Path,
        *,
        role: str,
        request_signature: str,
    ) -> LLMResult:
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranslationValidationError(
                f"翻译检查点无法读取，禁止盲目重发：{path.name}"
            ) from exc
        result_value = checkpoint.get("result") if isinstance(checkpoint, Mapping) else None
        structured = (
            result_value.get("structured") if isinstance(result_value, Mapping) else None
        )
        if (
            not isinstance(checkpoint, Mapping)
            or checkpoint.get("schema_version") != 1
            or checkpoint.get("status") != "validated"
            or checkpoint.get("role") != role
            or checkpoint.get("request_sha256") != request_signature
            or not isinstance(result_value, Mapping)
            or not isinstance(structured, Mapping)
            or checkpoint.get("output_sha256") != canonical_sha256(structured)
            or checkpoint.get("result_sha256") != canonical_sha256(result_value)
        ):
            raise TranslationValidationError(
                f"翻译检查点与当前冻结输入不匹配，禁止盲目重发：{path.name}"
            )
        usage_value = result_value.get("usage")
        if not isinstance(usage_value, Mapping):
            raise TranslationValidationError(f"翻译检查点缺少用量证据：{path.name}")
        try:
            usage = LLMUsage(
                input_tokens=int(usage_value.get("input_tokens") or 0),
                output_tokens=int(usage_value.get("output_tokens") or 0),
                total_tokens=int(usage_value.get("total_tokens") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise TranslationValidationError(f"翻译检查点用量证据无效：{path.name}") from exc
        return LLMResult(
            text=json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            structured=dict(structured),
            provider_id=str(result_value.get("provider_id") or ""),
            model=str(result_value.get("model") or ""),
            request_id=str(result_value.get("request_id") or "") or None,
            finish_reason=str(result_value.get("finish_reason") or "") or None,
            usage=usage,
        )

    @staticmethod
    def _validate_translation(result: Any, expected_ids: list[str]) -> list[dict[str, Any]]:
        items = result.structured.get("items")
        if not isinstance(items, list) or [str(item.get("id")) for item in items if isinstance(item, Mapping)] != expected_ids:
            raise TranslationValidationError("翻译 T 的稳定 ID 缺失、重复或顺序改变")
        normalized = []
        for item in items:
            text = str(item.get("subtitle_zh") or "").strip()
            if not text:
                raise TranslationValidationError(f"翻译 T 返回空译文：{item.get('id')}")
            normalized.append(
                {
                    "id": str(item["id"]),
                    "subtitle_zh": text,
                    "uncertainty": item.get("uncertainty"),
                }
            )
        return normalized

    @staticmethod
    def _validate_review(result: Any, expected_ids: list[str]) -> dict[str, Any]:
        covered = result.structured.get("covered_ids")
        issues = result.structured.get("issues")
        if covered != expected_ids or not isinstance(issues, list):
            raise TranslationValidationError("审核未完整覆盖当前批次")
        expected = set(expected_ids)
        normalized = []
        for issue in issues:
            if not isinstance(issue, Mapping) or str(issue.get("id")) not in expected:
                raise TranslationValidationError("审核报告引用了未知稳定 ID")
            if issue.get("severity") not in {"low", "medium", "high"}:
                raise TranslationValidationError("审核严重度无效")
            normalized.append({key: str(issue.get(key) or "") for key in ("id", "severity", "category", "suggestion", "rationale")})
        return {"covered_ids": list(covered), "issues": normalized}

    @staticmethod
    def _validate_decisions(
        result: Any,
        candidate: Sequence[Mapping[str, Any]],
        issues: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        decisions = result.structured.get("decisions")
        if not isinstance(decisions, list):
            raise TranslationValidationError("裁决结果缺少 decisions")
        candidate_ids = {str(row["id"]) for row in candidate}
        issue_ids = {str(row["id"]) for row in issues}
        seen: set[str] = set()
        normalized = []
        for row in decisions:
            if not isinstance(row, Mapping):
                raise TranslationValidationError("裁决结构无效")
            stable_id = str(row.get("id") or "")
            if stable_id not in candidate_ids or stable_id in seen:
                raise TranslationValidationError("裁决引用未知或重复稳定 ID")
            if stable_id not in issue_ids:
                raise TranslationValidationError("裁决不能修改审核未提出的问题")
            action = str(row.get("action") or "")
            final_text = str(row.get("final_text") or "").strip()
            if action not in {"keep", "replace"} or not final_text:
                raise TranslationValidationError("裁决动作或最终文本无效")
            seen.add(stable_id)
            normalized.append(
                {
                    "id": stable_id,
                    "action": action,
                    "final_text": final_text,
                    "rationale": str(row.get("rationale") or ""),
                }
            )
        if seen != issue_ids:
            raise TranslationValidationError("裁决没有覆盖全部审核问题")
        return normalized

    @staticmethod
    def _result_evidence(result: Any) -> dict[str, Any]:
        return {
            "provider_id": result.provider_id,
            "model": result.model,
            "request_id": result.request_id,
            "finish_reason": result.finish_reason,
            "usage": asdict(result.usage),
            "output_sha256": canonical_sha256(result.structured),
        }

    @staticmethod
    def _write_artifacts(
        output_dir: Path,
        version: int,
        source: list[dict[str, Any]],
        candidate: list[dict[str, Any]],
        audit_a: dict[str, Any],
        audit_b: dict[str, Any],
        decisions: list[dict[str, Any]],
        final: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> None:
        work = output_dir / "work"
        qa = output_dir / "qa"
        artifacts = {
            work / f"translation_candidate_agent_t_v{version}.json": {"slots": candidate},
            qa / f"translation_audit_agent_a_v{version}.json": audit_a,
            qa / f"translation_audit_agent_b_v{version}.json": audit_b,
            qa / f"translation_audit_decisions_v{version}.json": {"decisions": decisions},
            work / f"translation_final_v{version}.json": {"slots": final},
            qa / f"translation_agent_t_manifest_v{version}.json": {
                "source_sha256": canonical_sha256(source),
                "candidate_sha256": canonical_sha256(candidate),
                "slot_count": len(candidate),
                "stable_ids": [row["id"] for row in candidate],
                "missing_ids": [],
                "requests": evidence["translator_requests"],
            },
            qa / f"translation_regression_v{version}.json": {
                "status": "pass",
                "slot_count": len(final),
                "missing_ids": [],
                "duplicate_ids": [],
                "final_translation_sha256": canonical_sha256(final),
            },
        }
        for path, payload in artifacts.items():
            atomic_json(path, payload)
