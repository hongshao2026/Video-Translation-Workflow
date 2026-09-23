"""Deterministic workflow gates and verbatim chapter-reading artifacts.

This module deliberately contains no model or provider calls.  Every decision is
derived from JSON structure, immutable file bytes, stable IDs, and SHA-256
bindings so the same inputs produce the same gate result on another machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
APP_ROOT = Path(__file__).resolve().parents[2]
GATE_SCHEMAS = {
    "workflow_lock": "workflow_lock.schema.json",
    "ad_edit_gate": "ad_edit_gate.schema.json",
    "translation_gate": "translation_gate.schema.json",
    "production_gate": "production_gate.schema.json",
    "publication_package_gate": "publication_package_gate.schema.json",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SLOT_PATTERN = re.compile(
    r"<!--dub-sid:(?P<start>[A-Za-z0-9_-]+)-->(?P<text>.*?)"
    r"<!--dub-eid:(?P<end>[A-Za-z0-9_-]+)-->",
    re.DOTALL,
)
CHAPTER_PATTERN = re.compile(
    r"<!--dub-chapter-body-start:(?P<start>[A-Za-z0-9_-]+)-->(?P<body>.*?)"
    r"<!--dub-chapter-body-end:(?P<end>[A-Za-z0-9_-]+)-->",
    re.DOTALL,
)
SENTENCE_END_PATTERN = re.compile(r"[.!?。！？…](?:[”’》〉】』」\"']*)$")
EXPLICIT_DOWNSTREAM_PHRASES = (
    "开始选音",
    "进入选音",
    "启动工作台",
    "生成一分钟",
    "生成试听",
    "开始试听",
    "开始配音",
    "生成全片",
    "生成全文",
)


class GateValidationError(ValueError):
    """Raised when a caller requires a gate to pass and it does not."""


class ChapterReadingError(ValueError):
    """Raised for invalid translation slots or chapter assignments."""


class ApprovalBindingError(ValueError):
    """Raised when a command cannot safely approve the displayed artifacts."""


@dataclass(frozen=True)
class ValidationResult:
    """Machine-friendly result shared by gate and approval validators."""

    valid: bool
    errors: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.valid

    def require(self) -> None:
        if not self.valid:
            raise GateValidationError("; ".join(self.errors))

    def to_dict(self) -> dict[str, Any]:
        return {"status": "pass" if self.valid else "fail", "errors": list(self.errors)}


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_gate_schema(kind: str) -> dict[str, Any]:
    """Load one of the bundled, deliberately small JSON Schemas."""

    normalized = _normalize_gate_kind(kind)
    return json.loads((SCHEMA_DIR / GATE_SCHEMAS[normalized]).read_text(encoding="utf-8"))


def validate_gate(
    kind: str,
    payload: Mapping[str, Any],
    *,
    artifact_root: Path | str | None = None,
) -> ValidationResult:
    """Validate a gate's schema, invariants, and optional on-disk hashes.

    ``artifact_root`` is the project/run root against which every portable
    ``{"path", "sha256"}`` binding is resolved.  Absolute paths and traversal
    outside that root are rejected.
    """

    normalized = _normalize_gate_kind(kind)
    schema = load_gate_schema(normalized)
    errors = _schema_errors(payload, schema, schema, "$")
    errors.extend(_gate_semantic_errors(normalized, payload))
    if artifact_root is not None:
        root = Path(artifact_root)
        errors.extend(_artifact_binding_errors(payload, root))
        if normalized == "translation_gate":
            errors.extend(_translation_gate_artifact_errors(payload, root))
    return ValidationResult(not errors, tuple(_deduplicate(errors)))


def validate_gate_file(
    path: Path | str,
    *,
    kind: str | None = None,
    artifact_root: Path | str | None = None,
) -> ValidationResult:
    gate_path = Path(path)
    try:
        payload = json.loads(gate_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return ValidationResult(False, (f"无法读取门禁 {gate_path}: {exc}",))
    inferred = kind or gate_path.name.removesuffix(".json")
    root = Path(artifact_root) if artifact_root is not None else gate_path.parent.parent
    return validate_gate(inferred, payload, artifact_root=root)


def require_gate(
    kind: str,
    payload: Mapping[str, Any],
    *,
    artifact_root: Path | str | None = None,
) -> None:
    validate_gate(kind, payload, artifact_root=artifact_root).require()


def build_chapter_reading(
    translation_path: Path | str,
    chapters: Sequence[Mapping[str, Any]] | None,
    output_path: Path | str,
    validation_path: Path | str,
    *,
    video_id: str = "video",
    chapter_source: str = "generated",
    source_to_edit_timeline_path: Path | str | None = None,
    run_root: Path | str | None = None,
) -> dict[str, Any]:
    """Build a sentence-aligned Markdown reading without changing slot text.

    A chapter entry minimally contains ``id``, ``title`` and ``slot_ids``.
    With no chapter list, all slots are placed in one generated chapter.  Slot
    boundaries are HTML comments, invisible in rendered Markdown but complete
    enough for byte-for-byte reconstruction of every ``subtitle_zh``.
    """

    translation = Path(translation_path)
    output = Path(output_path)
    validation = Path(validation_path)
    root = Path(run_root).resolve() if run_root is not None else output.parent.parent.resolve()
    slots = _load_translation_slots(translation)
    normalized_chapters = _normalize_chapters(chapters, slots)
    _validate_chapter_assignment(normalized_chapters, slots)

    lines = [
        f"# {video_id} 中文阅读版（按章节）",
        "",
        "> 本稿正文逐字取自正式翻译 `subtitle_zh`；隐藏标记仅用于稳定 ID 完整性校验。",
        "",
    ]
    slot_by_id = {row["id"]: row for row in slots}
    for index, chapter in enumerate(normalized_chapters, start=1):
        chapter_id = chapter["id"]
        token = _encode_marker_value(chapter_id)
        lines.append(f"## {chapter_id} {chapter['title']}".rstrip())
        timing = _chapter_timing_line(chapter)
        if timing:
            lines.extend(("", timing))
        lines.extend(("", f"<!--dub-chapter-body-start:{token}-->"))
        body_parts: list[str] = []
        chapter_slot_ids = chapter["slot_ids"]
        for slot_index, stable_id in enumerate(chapter_slot_ids):
            text = slot_by_id[stable_id]["subtitle_zh"]
            sid = _encode_marker_value(stable_id)
            body_parts.append(f"<!--dub-sid:{sid}-->{text}<!--dub-eid:{sid}-->")
            if slot_index == len(chapter_slot_ids) - 1 or _ends_sentence(text):
                body_parts.append("\n\n")
        lines.append("".join(body_parts).rstrip("\n"))
        lines.extend((f"<!--dub-chapter-body-end:{token}-->", ""))

    _atomic_text(output, "\n".join(lines).rstrip() + "\n")
    report = validate_chapter_reading(
        translation,
        output,
        chapter_source=chapter_source,
        source_to_edit_timeline_path=source_to_edit_timeline_path,
        run_root=root,
    )
    _atomic_json(validation, report)
    if report["status"] != "pass":
        raise ChapterReadingError("; ".join(report["errors"]))
    return report


def validate_chapter_reading(
    translation_path: Path | str,
    reading_path: Path | str,
    *,
    chapter_source: str = "generated",
    source_to_edit_timeline_path: Path | str | None = None,
    run_root: Path | str | None = None,
) -> dict[str, Any]:
    """Prove stable-ID coverage and per-slot verbatim reconstruction."""

    translation = Path(translation_path)
    reading = Path(reading_path)
    root = Path(run_root).resolve() if run_root is not None else reading.parent.parent.resolve()
    slots = _load_translation_slots(translation)
    expected_ids = [row["id"] for row in slots]
    expected_by_id = {row["id"]: row["subtitle_zh"] for row in slots}
    text = reading.read_text(encoding="utf-8")

    errors: list[str] = []
    found: list[tuple[str, str]] = []
    mismatch_ids: list[str] = []
    invalid_markers: list[str] = []
    mid_sentence_break_ids: list[str] = []
    visible_additions = 0

    chapters = list(CHAPTER_PATTERN.finditer(text))
    chapter_start_count = text.count("<!--dub-chapter-body-start:")
    chapter_end_count = text.count("<!--dub-chapter-body-end:")
    if chapter_start_count != chapter_end_count or len(chapters) != chapter_start_count:
        errors.append("章节正文边界标记缺失或不配对")

    for chapter in chapters:
        if chapter.group("start") != chapter.group("end"):
            errors.append("章节正文开始和结束标记不匹配")
            continue
        body = chapter.group("body")
        matches = list(SLOT_PATTERN.finditer(body))
        residue_parts: list[str] = []
        cursor = 0
        for position, match in enumerate(matches):
            separator = body[cursor : match.start()]
            residue_parts.append(separator)
            try:
                start_id = _decode_marker_value(match.group("start"))
                end_id = _decode_marker_value(match.group("end"))
            except (UnicodeError, ValueError) as exc:
                invalid_markers.append(str(exc))
                cursor = match.end()
                continue
            if start_id != end_id:
                invalid_markers.append(start_id)
            else:
                found.append((start_id, match.group("text")))
            if position:
                previous = matches[position - 1]
                between = body[previous.end() : match.start()]
                try:
                    previous_id = _decode_marker_value(previous.group("start"))
                except (UnicodeError, ValueError):
                    previous_id = "<invalid>"
                if "\n" in between and not _ends_sentence(previous.group("text")):
                    mid_sentence_break_ids.append(previous_id)
            cursor = match.end()
        residue_parts.append(body[cursor:])
        # Whitespace is the only legal material outside stable-ID spans.
        visible_additions += len(re.sub(r"\s+", "", "".join(residue_parts)))

    raw_start_count = text.count("<!--dub-sid:")
    raw_end_count = text.count("<!--dub-eid:")
    if raw_start_count != raw_end_count or raw_start_count != len(found) + len(invalid_markers):
        errors.append("稳定 ID 开始和结束标记缺失或不配对")

    found_ids = [stable_id for stable_id, _ in found]
    counts = Counter(found_ids)
    duplicate_ids = [stable_id for stable_id in expected_ids if counts[stable_id] > 1]
    duplicate_ids.extend(sorted(stable_id for stable_id in counts if stable_id not in expected_by_id and counts[stable_id] > 1))
    missing_ids = [stable_id for stable_id in expected_ids if counts[stable_id] == 0]
    unknown_ids = [stable_id for stable_id in found_ids if stable_id not in expected_by_id]
    for stable_id, actual in found:
        if stable_id in expected_by_id and actual != expected_by_id[stable_id] and stable_id not in mismatch_ids:
            mismatch_ids.append(stable_id)
    out_of_order = found_ids != expected_ids

    if missing_ids:
        errors.append(f"缺少稳定 ID: {', '.join(missing_ids)}")
    if duplicate_ids:
        errors.append(f"重复稳定 ID: {', '.join(_deduplicate(duplicate_ids))}")
    if unknown_ids:
        errors.append(f"未知稳定 ID: {', '.join(_deduplicate(unknown_ids))}")
    if mismatch_ids:
        errors.append(f"subtitle_zh 未逐字重建: {', '.join(mismatch_ids)}")
    if out_of_order:
        errors.append("稳定 ID 顺序与正式翻译不一致")
    if invalid_markers:
        errors.append("存在无效或不匹配的稳定 ID 标记")
    if visible_additions:
        errors.append(f"章节正文中有 {visible_additions} 个未绑定到稳定 ID 的可见字符")
    if mid_sentence_break_ids:
        errors.append(f"非章节末存在句中换段: {', '.join(_deduplicate(mid_sentence_break_ids))}")

    exact = not (missing_ids or duplicate_ids or unknown_ids or mismatch_ids or invalid_markers or out_of_order)
    timeline = Path(source_to_edit_timeline_path) if source_to_edit_timeline_path is not None else None
    translation_binding = {
        "path": _portable_path(translation, root),
        "sha256": sha256_file(translation),
    }
    output_binding = {"path": _portable_path(reading, root), "sha256": sha256_file(reading)}
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "chapter_reading_layout": "sentence_aligned_verbatim",
        "input_translation_path": translation_binding["path"],
        "input_translation_sha256": translation_binding["sha256"],
        "chapter_source": chapter_source,
        "chapter_count": len(chapters),
        "output_path": output_binding["path"],
        "output_sha256": output_binding["sha256"],
        "slot_count": len(expected_ids),
        "assigned_slot_count": len(found_ids),
        "missing_slot_count": len(missing_ids),
        "duplicate_slot_count": len(duplicate_ids),
        "missing_slot_ids": missing_ids,
        "duplicate_slot_ids": _deduplicate(duplicate_ids),
        "unknown_slot_ids": _deduplicate(unknown_ids),
        "out_of_order": out_of_order,
        "subtitle_text_mismatch_ids": mismatch_ids,
        "stable_id_marker_count": len(found_ids),
        "each_stable_id_marker_unique": not duplicate_ids,
        "subtitle_text_used_verbatim": exact,
        "subtitle_text_reconstructable_per_stable_id": exact,
        "chapter_visible_text_insertions_or_deletions": visible_additions + (0 if exact else 1),
        "nonterminal_nonchapter_final_line_breaks": len(_deduplicate(mid_sentence_break_ids)),
        "nonterminal_nonchapter_final_line_break_ids": _deduplicate(mid_sentence_break_ids),
        "translation_rewritten": bool(mismatch_ids or unknown_ids),
        "translation_compressed": bool(missing_ids),
        "recording_or_pronunciation_text_used": False,
        "inputs": {"formal_translation": translation_binding},
        "output": output_binding,
        "errors": errors,
    }
    if timeline is not None:
        timeline_binding = {
            "path": _portable_path(timeline, root),
            "sha256": sha256_file(timeline),
        }
        report["source_to_edit_timeline_path"] = timeline_binding["path"]
        report["source_to_edit_timeline_sha256"] = timeline_binding["sha256"]
        report["inputs"]["original_to_working_timeline"] = timeline_binding
    return report


def is_explicit_downstream_command(command: str) -> bool:
    normalized = re.sub(r"\s+", "", str(command))
    return bool(normalized) and any(phrase in normalized for phrase in EXPLICIT_DOWNSTREAM_PHRASES)


def bind_translation_approval(
    command: str,
    *,
    translation_path: Path | str,
    reading_path: Path | str,
    validation_path: Path | str,
    approval_path: Path | str | None = None,
    run_root: Path | str | None = None,
    version: int = 1,
    approved_at_utc: str | None = None,
) -> dict[str, Any]:
    """Bind an explicit downstream command to the exact displayed bytes."""

    if not is_explicit_downstream_command(command):
        raise ApprovalBindingError("用户指令不是可绑定的明确下游执行指令")
    translation = Path(translation_path)
    reading = Path(reading_path)
    validation = Path(validation_path)
    approval = Path(approval_path) if approval_path is not None else None
    root = (
        Path(run_root).resolve()
        if run_root is not None
        else (approval.parent.parent.resolve() if approval is not None else reading.parent.parent.resolve())
    )
    try:
        validation_payload = json.loads(validation.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApprovalBindingError(f"无法读取章节阅读验证报告: {exc}") from exc
    translation_sha = sha256_file(translation)
    reading_sha = sha256_file(reading)
    validation_sha = sha256_file(validation)
    if validation_payload.get("status") != "pass":
        raise ApprovalBindingError("章节阅读验证尚未通过")
    if validation_payload.get("input_translation_sha256") != translation_sha:
        raise ApprovalBindingError("章节阅读验证报告绑定的正式翻译已变更")
    if validation_payload.get("output_sha256") != reading_sha:
        raise ApprovalBindingError("章节阅读稿在验证后已变更")
    if not validation_payload.get("subtitle_text_reconstructable_per_stable_id"):
        raise ApprovalBindingError("章节阅读稿无法按稳定 ID 逐字重建")
    current_reading = validate_chapter_reading(translation, reading, run_root=root)
    if current_reading["status"] != "pass":
        raise ApprovalBindingError("当前章节阅读稿确定性验证失败: " + "; ".join(current_reading["errors"]))

    artifact = {
        "schema_version": 1,
        "version": int(version),
        "status": "pass",
        "approved": True,
        "capture_mode": "explicit_downstream_command",
        "approval_policy": "explicit_downstream_command_binds_current_version",
        "command": str(command),
        "user_command": str(command),
        "approved_at_utc": approved_at_utc or datetime.now(UTC).isoformat(),
        "binding": {
            "formal_translation": {
                "path": _portable_path(translation, root),
                "sha256": translation_sha,
            },
            "chapter_reading": {
                "path": _portable_path(reading, root),
                "sha256": reading_sha,
            },
            "chapter_reading_validation": {
                "path": _portable_path(validation, root),
                "sha256": validation_sha,
                "status": "pass",
            },
        },
    }
    if approval is not None:
        _atomic_json(approval, artifact)
    return artifact


def validate_translation_approval(
    approval: Mapping[str, Any] | Path | str,
    *,
    translation_path: Path | str,
    reading_path: Path | str,
    validation_path: Path | str | None = None,
) -> ValidationResult:
    """Return false as soon as any approved artifact byte changes."""

    if isinstance(approval, Mapping):
        payload = dict(approval)
    else:
        try:
            payload = json.loads(Path(approval).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return ValidationResult(False, (f"无法读取批准记录: {exc}",))
    errors: list[str] = []
    if payload.get("status") != "pass" or payload.get("approved") is not True:
        errors.append("批准记录未通过")
    if payload.get("capture_mode") != "explicit_downstream_command":
        errors.append("批准不是由明确下游指令捕获")
    if not is_explicit_downstream_command(str(payload.get("command") or payload.get("user_command") or "")):
        errors.append("批准记录缺少明确下游执行指令")
    binding = payload.get("binding")
    if not isinstance(binding, Mapping):
        return ValidationResult(False, tuple(errors + ["批准记录缺少哈希绑定"]))
    checks = [
        ("formal_translation", Path(translation_path), "正式翻译"),
        ("chapter_reading", Path(reading_path), "章节阅读稿"),
    ]
    if validation_path is not None:
        checks.append(("chapter_reading_validation", Path(validation_path), "章节阅读验证报告"))
    for key, path, label in checks:
        expected = binding.get(key)
        if not isinstance(expected, Mapping) or not SHA256_PATTERN.fullmatch(str(expected.get("sha256") or "")):
            errors.append(f"{label}缺少有效 SHA-256 绑定")
            continue
        try:
            actual = sha256_file(path)
        except OSError as exc:
            errors.append(f"{label}无法读取: {exc}")
            continue
        if actual != expected["sha256"]:
            errors.append(f"{label}哈希不匹配，旧批准已失效")
    return ValidationResult(not errors, tuple(_deduplicate(errors)))


def _normalize_gate_kind(kind: str) -> str:
    value = Path(str(kind)).name
    value = value.removesuffix(".schema.json").removesuffix(".json")
    if value not in GATE_SCHEMAS:
        raise ValueError(f"未知门禁类型: {kind}")
    return value


def _schema_errors(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "$ref" in schema:
        ref = str(schema["$ref"])
        if not ref.startswith("#/"):
            return [f"{path}: 不支持外部 $ref {ref}"]
        target: Any = root
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return [f"{path}: 无法解析 $ref {ref}"]
        errors.extend(_schema_errors(value, target, root, path))
    for branch in schema.get("allOf", []):
        errors.extend(_schema_errors(value, branch, root, path))
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 必须等于 {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 不在允许值 {schema['enum']!r} 中")
    expected_type = schema.get("type")
    if expected_type and not _matches_json_type(value, str(expected_type)):
        errors.append(f"{path}: 应为 {expected_type}")
        return errors
    if isinstance(value, Mapping):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺少必填字段 {key}")
        properties = schema.get("properties", {})
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(_schema_errors(value[key], child_schema, root, f"{path}.{key}"))
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path}: 数组项数小于 {schema['minItems']}")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: 数组项数大于 {schema['maxItems']}")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, schema["items"], root, f"{path}[{index}]"))
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path}: 字符串过短")
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            errors.append(f"{path}: 不匹配 {schema['pattern']}")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        errors.append(f"{path}: 小于最小值 {schema['minimum']}")
    return errors


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _gate_semantic_errors(kind: str, payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if kind == "workflow_lock":
        documents = payload.get("required_documents")
        if isinstance(documents, list):
            paths = [str(row.get("path")) for row in documents if isinstance(row, Mapping)]
            if len(paths) != len(set(paths)):
                errors.append("$.required_documents: 文档路径重复")
    elif kind == "ad_edit_gate":
        if payload.get("decision") == "no_ads_detected" and (
            payload.get("removed_segments") or payload.get("overlay_regions")
        ):
            errors.append("$.decision: no_ads_detected 不能同时包含删除段或遮盖区")
    elif kind == "translation_gate":
        translator = payload.get("translator")
        reviewers = payload.get("reviewers")
        role_ids: list[str] = []
        if isinstance(translator, Mapping):
            role_ids.append(str(translator.get("id") or ""))
        if isinstance(reviewers, list):
            role_ids.extend(str(row.get("id") or "") for row in reviewers if isinstance(row, Mapping))
        role_ids.append(str(payload.get("orchestrator_id") or ""))
        if "" in role_ids or len(role_ids) != len(set(role_ids)):
            errors.append("$.roles_are_distinct: T、A、B 与主控的标识必须全部不同")
        final = payload.get("final_translation")
        reading = payload.get("chapter_reading")
        approval = payload.get("user_approval")
        if (
            isinstance(final, Mapping)
            and isinstance(approval, Mapping)
            and final.get("sha256") != approval.get("final_translation_sha256")
        ):
            errors.append("$.user_approval.final_translation_sha256: 与当前正式翻译不匹配")
        if (
            isinstance(reading, Mapping)
            and isinstance(approval, Mapping)
            and reading.get("sha256") != approval.get("reading_sha256")
        ):
            errors.append("$.user_approval.reading_sha256: 与当前章节阅读稿不匹配")
        if isinstance(approval, Mapping) and not is_explicit_downstream_command(str(approval.get("command") or "")):
            errors.append("$.user_approval.command: 不是明确下游执行指令")
    return errors


def _artifact_binding_errors(payload: Mapping[str, Any], root: Path) -> list[str]:
    root = root.resolve()
    allowed_roots = (root, APP_ROOT.resolve())
    errors: list[str] = []
    for location, artifact in _walk_artifacts(payload):
        raw_path = artifact.get("path")
        expected = str(artifact.get("sha256") or "")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        candidate = Path(raw_path)
        candidates = [candidate.resolve()] if candidate.is_absolute() else [(root / candidate).resolve()]
        if not candidate.is_absolute() and APP_ROOT.resolve() != root:
            candidates.append((APP_ROOT / candidate).resolve())
        safe_candidates = [
            resolved
            for resolved in candidates
            if any(_is_relative_to(resolved, allowed_root) for allowed_root in allowed_roots)
        ]
        if not safe_candidates:
            errors.append(f"{location}.path: 路径越出项目根目录")
            continue
        resolved = next((path for path in safe_candidates if path.is_file()), safe_candidates[0])
        if not resolved.is_file():
            errors.append(f"{location}.path: 文件不存在 {raw_path}")
            continue
        actual = sha256_file(resolved)
        if not SHA256_PATTERN.fullmatch(expected) or actual != expected:
            errors.append(f"{location}.sha256: 哈希不匹配 {raw_path}")
    return errors


def _translation_gate_artifact_errors(payload: Mapping[str, Any], root: Path) -> list[str]:
    """Cross-check the two evidence files that authorize downstream work."""

    errors: list[str] = []
    final = payload.get("final_translation")
    reading = payload.get("chapter_reading")
    approval = payload.get("user_approval")
    if not all(isinstance(value, Mapping) for value in (final, reading, approval)):
        return errors
    validation_ref = reading.get("validation")
    approval_ref = approval.get("artifact")
    if isinstance(validation_ref, Mapping):
        validation_payload = _read_bound_json(validation_ref, root)
        if validation_payload is None:
            errors.append("$.chapter_reading.validation: 验证报告不是可读的 JSON 对象")
        else:
            if validation_payload.get("status") != "pass":
                errors.append("$.chapter_reading.validation: 验证报告未通过")
            if validation_payload.get("input_translation_sha256") != final.get("sha256"):
                errors.append("$.chapter_reading.validation: 正式翻译哈希绑定不匹配")
            if validation_payload.get("output_sha256") != reading.get("sha256"):
                errors.append("$.chapter_reading.validation: 阅读稿哈希绑定不匹配")
    if isinstance(approval_ref, Mapping):
        approval_payload = _read_bound_json(approval_ref, root)
        if approval_payload is None:
            errors.append("$.user_approval.artifact: 批准记录不是可读的 JSON 对象")
        else:
            binding = approval_payload.get("binding")
            if not isinstance(binding, Mapping):
                errors.append("$.user_approval.artifact: 批准记录缺少 binding")
            else:
                bound_translation = binding.get("formal_translation")
                bound_reading = binding.get("chapter_reading")
                if not isinstance(bound_translation, Mapping) or bound_translation.get("sha256") != final.get("sha256"):
                    errors.append("$.user_approval.artifact: 批准未绑定当前正式翻译")
                if not isinstance(bound_reading, Mapping) or bound_reading.get("sha256") != reading.get("sha256"):
                    errors.append("$.user_approval.artifact: 批准未绑定当前章节阅读稿")
    return errors


def _read_bound_json(binding: Mapping[str, Any], root: Path) -> Mapping[str, Any] | None:
    raw_path = binding.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    resolved = path.resolve() if path.is_absolute() else (root.resolve() / path).resolve()
    if not _is_relative_to(resolved, root.resolve()) or not resolved.is_file():
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _walk_artifacts(value: Any, path: str = "$") -> Iterable[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        if "path" in value and "sha256" in value:
            yield path, value
        for key, child in value.items():
            yield from _walk_artifacts(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_artifacts(child, f"{path}[{index}]")


def _load_translation_slots(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterReadingError(f"无法读取正式翻译: {exc}") from exc
    rows = payload.get("slots") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list) or not rows:
        raise ChapterReadingError("正式翻译必须包含非空 slots 数组")
    slots: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ChapterReadingError(f"第 {index + 1} 个字幕槽结构无效")
        stable_id = str(row.get("id") or "")
        subtitle = row.get("subtitle_zh")
        if not stable_id:
            raise ChapterReadingError(f"第 {index + 1} 个字幕槽缺少稳定 ID")
        if not isinstance(subtitle, str) or not subtitle:
            raise ChapterReadingError(f"字幕槽 {stable_id} 缺少非空 subtitle_zh")
        slots.append({"id": stable_id, "subtitle_zh": subtitle})
    ids = [row["id"] for row in slots]
    duplicates = [stable_id for stable_id, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise ChapterReadingError(f"正式翻译存在重复稳定 ID: {', '.join(duplicates)}")
    return slots


def _normalize_chapters(
    chapters: Sequence[Mapping[str, Any]] | None,
    slots: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    if chapters is None:
        return [{"id": "C01", "title": "全文", "slot_ids": [row["id"] for row in slots]}]
    if not chapters:
        raise ChapterReadingError("章节列表不能为空")
    normalized: list[dict[str, Any]] = []
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, Mapping):
            raise ChapterReadingError(f"第 {index} 个章节结构无效")
        chapter_id = str(chapter.get("id") or f"C{index:02d}")
        title = str(chapter.get("title") or f"第 {index} 章")
        slot_ids = chapter.get("slot_ids")
        if not isinstance(slot_ids, list) or not slot_ids:
            raise ChapterReadingError(f"章节 {chapter_id} 必须包含非空 slot_ids")
        row = {"id": chapter_id, "title": title, "slot_ids": [str(value) for value in slot_ids]}
        for key in ("working_start", "source_start"):
            if chapter.get(key) is not None:
                row[key] = str(chapter[key])
        normalized.append(row)
    return normalized


def _validate_chapter_assignment(
    chapters: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, str]],
) -> None:
    expected = [row["id"] for row in slots]
    assigned = [str(stable_id) for chapter in chapters for stable_id in chapter["slot_ids"]]
    counts = Counter(assigned)
    missing = [stable_id for stable_id in expected if counts[stable_id] == 0]
    duplicates = [stable_id for stable_id in expected if counts[stable_id] > 1]
    unknown = [stable_id for stable_id in assigned if stable_id not in set(expected)]
    errors = []
    if missing:
        errors.append(f"章节分配缺失稳定 ID: {', '.join(missing)}")
    if duplicates:
        errors.append(f"章节分配重复稳定 ID: {', '.join(_deduplicate(duplicates))}")
    if unknown:
        errors.append(f"章节分配包含未知稳定 ID: {', '.join(_deduplicate(unknown))}")
    if assigned != expected:
        errors.append("章节分配改变了稳定 ID 顺序或跨章移动")
    if errors:
        raise ChapterReadingError("; ".join(errors))


def _chapter_timing_line(chapter: Mapping[str, Any]) -> str:
    values = []
    if chapter.get("working_start"):
        values.append(f"工作母版 {chapter['working_start']}")
    if chapter.get("source_start"):
        values.append(f"原视频 {chapter['source_start']}")
    return "｜".join(values)


def _ends_sentence(text: str) -> bool:
    return SENTENCE_END_PATTERN.search(text.rstrip()) is not None


def _encode_marker_value(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_marker_value(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True).decode("utf-8")


def _portable_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"制品路径必须位于项目根目录内: {resolved}") from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


def _deduplicate(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "ApprovalBindingError",
    "ChapterReadingError",
    "GateValidationError",
    "ValidationResult",
    "bind_translation_approval",
    "build_chapter_reading",
    "is_explicit_downstream_command",
    "load_gate_schema",
    "require_gate",
    "sha256_file",
    "validate_chapter_reading",
    "validate_gate",
    "validate_gate_file",
    "validate_translation_approval",
]
