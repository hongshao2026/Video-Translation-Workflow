"""Deterministic publication-package construction and validation.

The translation model (or a human editor) supplies titles, the Chinese
description and book-list text *before* this module is called.  This module
does not rewrite that content and never calls a model or the network.  It only
maps approved chapter times, rejects promotional residue, writes immutable
versioned artifacts, lays out two covers when Pillow is available, and binds
the resulting bytes into a final publication gate.

All public writers use exclusive creation.  A caller must increment the
version instead of replacing an existing publication artifact.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # Pillow is intentionally optional at import time.
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    _PILLOW_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised by capability tests
    Image = ImageDraw = ImageFont = ImageOps = None  # type: ignore[assignment]
    _PILLOW_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)\S+|(?<![\w@])(?:[a-z0-9-]+\.)+(?:com|net|org|cn|io|ai|co|tv)(?:/\S*)?"
)
PROMOTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("discount", re.compile(r"(?i)\b(?:discount|coupon|promo(?:tion)?\s*code|sale)\b|优惠|折扣|优惠码|促销|特价")),
    ("purchase", re.compile(r"(?i)\b(?:buy\s+now|order\s+now|shop\s+now)\b|立即购买|下单|抢购")),
    ("subscription", re.compile(r"(?i)\b(?:subscribe|subscription|follow\s+us)\b|订阅|关注我们|点击关注")),
    ("qr_code", re.compile(r"(?i)\bQR\s*code\b|二维码|扫码")),
    ("traffic_diversion", re.compile(r"(?i)\b(?:link\s+in\s+bio|join\s+our\s+community)\b|私信获取|加群|加微信|官网查看")),
)

COVER_SPECS: dict[str, dict[str, Any]] = {
    "16x9": {
        "width": 1920,
        "height": 1080,
        "layout": "source_frame_left_text_right",
        "safe_margin": 86,
    },
    "4x3": {
        "width": 1440,
        "height": 1080,
        "layout": "source_frame_top_text_bottom",
        "safe_margin": 72,
    },
}


class PublicationError(ValueError):
    """Raised when publication inputs cannot safely produce a deliverable."""


class TimelineMappingError(PublicationError):
    """Raised for incomplete, ambiguous or non-monotonic timeline mappings."""


class PublicationContentError(PublicationError):
    """Raised when supplied publication copy contains forbidden residue."""


class CoverGenerationError(PublicationError):
    """Raised when an otherwise available cover generator cannot produce PNGs."""


@dataclass(frozen=True)
class ContentValidation:
    valid: bool
    issues: tuple[dict[str, str], ...]
    original_video_link_preserved: bool

    def require(self) -> None:
        if not self.valid:
            details = "; ".join(
                f"{item['field']}:{item['category']}" for item in self.issues
            )
            raise PublicationContentError(details or "publication content is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.valid else "fail",
            "issues": [dict(item) for item in self.issues],
            "original_video_link_preserved": self.original_video_link_preserved,
            "advertising_or_promotional_links_remaining": _promotional_issue_count(self.issues),
        }


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_ref(path: Path | str, *, run_root: Path | str) -> dict[str, str]:
    """Return a portable, content-addressed reference inside ``run_root``."""

    root = Path(run_root).resolve()
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(file_path)
    try:
        relative = file_path.relative_to(root)
    except ValueError as exc:
        raise PublicationError("publication artifact is outside the run directory") from exc
    return {"path": relative.as_posix(), "sha256": sha256_file(file_path)}


def format_timestamp(seconds: float) -> str:
    """Format a non-negative chapter time without rounding past the media."""

    value = _finite_nonnegative(seconds, "chapter time")
    whole = math.floor(value + 1e-9)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def map_timestamp(seconds: float, mapping: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> float:
    """Map one timestamp through a complete piecewise-linear timeline.

    Supported segment names include the neutral ``source_*``/``target_*``
    pair, ``original_*``/``edited_*`` and ``working_*``/``output_*``.  A
    timestamp that falls in a removed gap is rejected rather than silently
    clamped to an unrelated frame.
    """

    value = _finite_nonnegative(seconds, "source timestamp")
    segments = _normalize_mapping_segments(mapping)
    epsilon = 1e-7
    for index, segment in enumerate(segments):
        source_start, source_end, target_start, target_end = segment
        is_last = index == len(segments) - 1
        in_segment = source_start - epsilon <= value < source_end - epsilon
        if is_last and source_start - epsilon <= value <= source_end + epsilon:
            in_segment = True
        if not in_segment:
            continue
        source_span = source_end - source_start
        target_span = target_end - target_start
        ratio = (value - source_start) / source_span
        mapped = target_start + ratio * target_span
        if not math.isfinite(mapped) or mapped < -epsilon:
            raise TimelineMappingError("timeline mapping produced an invalid timestamp")
        return max(0.0, mapped)
    raise TimelineMappingError(f"timestamp {value:.6f} falls outside the retained timeline")


def build_final_chapter_timeline(
    source_chapters: Sequence[Mapping[str, Any]],
    source_to_working: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    working_to_final: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    final_duration: float,
    final_video_sha256: str,
    final_machine_qa_sha256: str,
    approved_chapter_count: int | None = None,
    require_first_zero: bool = True,
) -> dict[str, Any]:
    """Create an auditable original -> working -> final chapter timeline."""

    if not source_chapters:
        raise TimelineMappingError("at least one approved source chapter is required")
    _require_sha256(final_video_sha256, "final video")
    _require_sha256(final_machine_qa_sha256, "final machine QA")
    duration = _finite_positive(final_duration, "final media duration")
    expected_count = len(source_chapters) if approved_chapter_count is None else int(approved_chapter_count)
    if expected_count != len(source_chapters):
        raise TimelineMappingError("chapter count does not match the approved source")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_source = previous_working = previous_final = -1.0
    previous_publish_second = -1
    for index, chapter in enumerate(source_chapters, start=1):
        chapter_id = str(chapter.get("id") or chapter.get("chapter_id") or f"C{index:02d}").strip()
        title = str(chapter.get("title") or chapter.get("title_zh") or "").strip()
        if not chapter_id or chapter_id in seen_ids:
            raise TimelineMappingError("chapter IDs must be non-empty and unique")
        if not title or "\n" in title or "\r" in title:
            raise TimelineMappingError(f"chapter {chapter_id} has an invalid title")
        seen_ids.add(chapter_id)
        source_seconds = _chapter_source_seconds(chapter)
        if source_seconds <= previous_source:
            if index == 1 and abs(source_seconds) <= 1e-7:
                pass
            else:
                raise TimelineMappingError("source chapter times must be strictly increasing")
        working_seconds = map_timestamp(source_seconds, source_to_working)
        final_seconds = map_timestamp(working_seconds, working_to_final)
        if index > 1 and working_seconds <= previous_working + 1e-7:
            raise TimelineMappingError("working-master chapter times are not strictly increasing")
        if index > 1 and final_seconds <= previous_final + 1e-7:
            raise TimelineMappingError("final chapter times are not strictly increasing")
        if final_seconds > duration + 1e-7:
            raise TimelineMappingError("a chapter starts after the final media duration")
        publish_second = math.floor(final_seconds + 1e-9)
        if publish_second <= previous_publish_second:
            if index == 1 and publish_second == 0:
                pass
            else:
                raise TimelineMappingError(
                    "whole-second publication chapter times are not strictly increasing"
                )
        rows.append(
            {
                "id": chapter_id,
                "title": title,
                "source_seconds": round(source_seconds, 6),
                "working_seconds": round(working_seconds, 6),
                "final_seconds": round(final_seconds, 6),
                "timestamp": format_timestamp(final_seconds),
            }
        )
        previous_source = source_seconds
        previous_working = working_seconds
        previous_final = final_seconds
        previous_publish_second = publish_second

    first_is_zero = abs(rows[0]["final_seconds"]) <= 1e-7
    if require_first_zero and not first_is_zero:
        raise TimelineMappingError("the first final chapter must start at 0:00")
    return {
        "schema_version": 1,
        "status": "pass",
        "final_video_sha256": final_video_sha256,
        "final_machine_qa_sha256": final_machine_qa_sha256,
        "final_duration_seconds": round(duration, 6),
        "chapter_source": "approved_source",
        "approved_chapter_count": expected_count,
        "chapter_count": len(rows),
        "checks": {
            "source_to_working_mapping_valid": True,
            "working_to_final_mapping_valid": True,
            "source_to_output_mapping_valid": True,
            "times_strictly_increasing": True,
            "publish_timestamps_strictly_increasing": True,
            "chapter_count_matches_approved_source": True,
            "first_chapter_zero": first_is_zero,
            "all_chapters_within_final_duration": True,
        },
        "chapters": rows,
    }


def write_final_chapter_timeline(
    run_root: Path | str,
    video_id: str,
    version: int,
    *,
    final_video_path: Path | str,
    final_machine_qa_path: Path | str,
    source_chapters: Sequence[Mapping[str, Any]],
    source_to_working_path: Path | str,
    working_to_final_path: Path | str,
    approved_chapter_count: int | None = None,
    require_first_zero: bool = True,
) -> dict[str, Any]:
    """Validate machine QA and persist a versioned final chapter mapping."""

    root = Path(run_root).resolve()
    _safe_video_id(video_id)
    qa_dir = root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    final_video = Path(final_video_path).resolve()
    final_qa = Path(final_machine_qa_path).resolve()
    source_map_path = Path(source_to_working_path).resolve()
    retime_map_path = Path(working_to_final_path).resolve()
    for path in (final_video, final_qa, source_map_path, retime_map_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    qa_payload = _load_json_object(final_qa, "final machine QA")
    if qa_payload.get("status") != "pass":
        raise PublicationError("final machine QA has not passed")
    video_sha = sha256_file(final_video)
    if str(qa_payload.get("sha256") or "") != video_sha:
        raise PublicationError("final machine QA is bound to a different video")
    duration = _machine_qa_duration(qa_payload)
    source_map = _load_json_object(source_map_path, "source-to-working timeline")
    retime_map = _load_json_object(retime_map_path, "working-to-final timeline")
    payload = build_final_chapter_timeline(
        source_chapters,
        source_map,
        retime_map,
        final_duration=duration,
        final_video_sha256=video_sha,
        final_machine_qa_sha256=sha256_file(final_qa),
        approved_chapter_count=approved_chapter_count,
        require_first_zero=require_first_zero,
    )
    payload.update(
        {
            "final_video": artifact_ref(final_video, run_root=root),
            "final_machine_qa": artifact_ref(final_qa, run_root=root),
            "source_to_working_timeline": artifact_ref(source_map_path, run_root=root),
            "working_to_final_timeline": artifact_ref(retime_map_path, run_root=root),
        }
    )
    output = qa_dir / f"chapter_timeline_v{_version(version)}.json"
    _exclusive_json(output, payload)
    return payload


def validate_publication_content(
    *,
    titles: Sequence[str],
    description: str,
    chapter_titles: Sequence[str],
    books: Sequence[str],
    original_video_url: str,
    allow_single_title: bool = False,
) -> ContentValidation:
    """Reject promotion, calls-to-action, QR references and non-source URLs."""

    normalized_titles = [str(value).strip() for value in titles]
    allowed_counts = {1} if allow_single_title else {3, 4, 5}
    issues: list[dict[str, str]] = []
    if len(normalized_titles) not in allowed_counts:
        issues.append(
            {
                "field": "titles",
                "category": "title_count",
                "match": str(len(normalized_titles)),
            }
        )
    if len(set(normalized_titles)) != len(normalized_titles):
        issues.append(
            {"field": "titles", "category": "duplicate_title", "match": ""}
        )
    for field, values in (
        ("titles", normalized_titles),
        ("chapter_titles", [str(value).strip() for value in chapter_titles]),
        ("books", [str(value).strip() for value in books]),
    ):
        for value in values:
            if not value or "\n" in value or "\r" in value:
                issues.append({"field": field, "category": "empty_or_multiline", "match": value[:80]})
                continue
            issues.extend(_scan_forbidden(field, value, allowed_url=None))

    clean_description = str(description).strip()
    source_url = str(original_video_url).strip()
    valid_source_url = _valid_public_url(source_url)
    source_preserved = valid_source_url and source_url in clean_description
    if not source_preserved:
        issues.append(
            {
                "field": "description",
                "category": "original_video_link_missing",
                "match": source_url[:120],
            }
        )
    issues.extend(
        _scan_forbidden(
            "description",
            clean_description,
            allowed_url=source_url if valid_source_url else None,
        )
    )
    unique = _unique_issue_dicts(issues)
    return ContentValidation(not unique, tuple(unique), bool(source_preserved))


def create_publication_materials(
    run_root: Path | str,
    video_id: str,
    version: int,
    *,
    final_video_path: Path | str,
    final_machine_qa_path: Path | str,
    chapter_timeline_path: Path | str,
    titles: Sequence[str],
    description: str,
    books: Sequence[str],
    original_video_url: str,
    allow_single_title: bool = False,
    foreign_names_verified: bool,
    removed_promotion_categories: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate supplied copy and write three UTF-8 TXT publication files.

    Invalid copy writes a versioned ``publication_materials`` failure report
    but no formal text deliverables.  Repair therefore requires a new version,
    retaining the failed evidence rather than overwriting it.
    """

    root = Path(run_root).resolve()
    safe_video_id = _safe_video_id(video_id)
    deliverables = root / "deliverables"
    qa_dir = root / "qa"
    deliverables.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)
    suffix = _version(version)
    report_path = qa_dir / f"publication_materials_v{suffix}.json"
    _require_absent(report_path)

    video_path = Path(final_video_path).resolve()
    machine_qa_path = Path(final_machine_qa_path).resolve()
    timeline_path = Path(chapter_timeline_path).resolve()
    machine_qa = _load_json_object(machine_qa_path, "final machine QA")
    timeline = _load_json_object(timeline_path, "final chapter timeline")
    video_sha = sha256_file(video_path)
    structural_issues: list[dict[str, str]] = []
    if machine_qa.get("status") != "pass" or machine_qa.get("sha256") != video_sha:
        structural_issues.append(
            {"field": "final_machine_qa", "category": "not_bound_to_final_video", "match": ""}
        )
    if timeline.get("status") != "pass" or timeline.get("final_video_sha256") != video_sha:
        structural_issues.append(
            {"field": "chapter_timeline", "category": "not_bound_to_final_video", "match": ""}
        )
    chapters = [row for row in timeline.get("chapters") or [] if isinstance(row, Mapping)]
    chapter_titles = [str(row.get("title") or "") for row in chapters]
    content_check = validate_publication_content(
        titles=titles,
        description=description,
        chapter_titles=chapter_titles,
        books=books,
        original_video_url=original_video_url,
        allow_single_title=allow_single_title,
    )
    all_issues = [*structural_issues, *content_check.issues]
    if not foreign_names_verified:
        all_issues.append(
            {"field": "names", "category": "foreign_names_unverified", "match": ""}
        )

    base_report: dict[str, Any] = {
        "schema_version": 1,
        "status": "fail" if all_issues else "pass",
        "version": int(suffix),
        "final_video": artifact_ref(video_path, run_root=root),
        "final_machine_qa": artifact_ref(machine_qa_path, run_root=root),
        "chapter_timeline": artifact_ref(timeline_path, run_root=root),
        "title_count": len(titles),
        "chapter_count": len(chapters),
        "original_video_link_preserved": content_check.original_video_link_preserved,
        "advertising_or_promotional_links_remaining": _promotional_issue_count(all_issues),
        "removed_promotion_categories": sorted(
            {str(value).strip() for value in removed_promotion_categories if str(value).strip()}
        ),
        "utf8_txt_only": not all_issues,
        "foreign_names_verified": bool(foreign_names_verified),
        "issues": [dict(value) for value in all_issues],
    }
    if all_issues:
        _exclusive_json(report_path, base_report)
        return base_report

    title_lines = [f"{index}. {str(title).strip()}" for index, title in enumerate(titles, start=1)]
    chapter_lines = [f"{row['timestamp']} {str(row['title']).strip()}" for row in chapters]
    book_lines = [f"{index}. {str(book).strip()}" for index, book in enumerate(books, start=1)]
    if not book_lines:
        book_lines = ["无"]
    combined_text = "\n".join(
        [
            "中文标题候选",
            *title_lines,
            "",
            "中文简介",
            str(description).strip(),
            "",
            "最终章节",
            *chapter_lines,
            "",
            "片中书单",
            *book_lines,
            "",
        ]
    )
    description_text = "\n".join(
        ["中文简介", str(description).strip(), "", "最终章节", *chapter_lines, ""]
    )
    books_text = "\n".join(["片中书单", *book_lines, ""])
    combined_path = deliverables / f"{safe_video_id}_发布材料_v{suffix}.txt"
    description_path = deliverables / f"{safe_video_id}_中文简介与章节_v{suffix}.txt"
    books_path = deliverables / f"{safe_video_id}_片中书单_v{suffix}.txt"
    for path in (combined_path, description_path, books_path):
        _require_absent(path)
    _exclusive_utf8(combined_path, combined_text)
    _exclusive_utf8(description_path, description_text)
    _exclusive_utf8(books_path, books_text)

    text_artifacts = {
        "publication_text": artifact_ref(combined_path, run_root=root),
        "description_and_chapters": artifact_ref(description_path, run_root=root),
        "book_list": artifact_ref(books_path, run_root=root),
    }
    base_report.update(
        {
            "publication_text_format": "utf8_txt",
            "advertising_or_promotional_links_remaining": 0,
            "checks": {
                "final_machine_qa_pass": True,
                "final_video_hash_matches": True,
                "chapter_timeline_bound_to_final_video": True,
                "chapter_times_strictly_increasing": _chapters_strict(chapters),
                "original_video_link_preserved": True,
                "utf8_txt_format": True,
                "foreign_name_rule_verified": True,
                "promotional_residue_zero": True,
            },
            "artifacts": text_artifacts,
        }
    )
    _exclusive_json(report_path, base_report)
    return base_report


def pillow_capability() -> dict[str, Any]:
    """Describe cover support without installing packages or changing state."""

    available = Image is not None
    return {
        "available": available,
        "engine": "Pillow",
        "error": _PILLOW_IMPORT_ERROR,
        "install_command": ["python", "-m", "pip", "install", "Pillow"],
        "outputs": {name: dict(spec) for name, spec in COVER_SPECS.items()},
    }


def create_cover_art(
    run_root: Path | str,
    video_id: str,
    version: int,
    *,
    source_image_path: Path | str,
    title: str,
    source_authorized: bool,
    source_clean_verified: bool,
    identity_verified: bool,
    text_verified: bool,
    design_notes: str = "授权源帧与标题分区重排，保留完整源帧内容。",
) -> dict[str, Any]:
    """Render immutable 16:9 and 4:3 PNG covers from an authorized frame.

    The source frame is contained, never center-cropped.  The 4:3 composition
    uses a distinct top-image/bottom-title layout, which makes the re-layout
    mechanically distinguishable from a crop of the 16:9 cover.
    """

    root = Path(run_root).resolve()
    safe_video_id = _safe_video_id(video_id)
    covers_dir = root / "deliverables" / "covers"
    qa_dir = root / "qa"
    covers_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)
    suffix = _version(version)
    report_path = qa_dir / f"cover_art_v{suffix}.json"
    cover_16 = covers_dir / f"{safe_video_id}_中文封面_16x9_v{suffix}.png"
    cover_43 = covers_dir / f"{safe_video_id}_中文封面_4x3_v{suffix}.png"
    for path in (report_path, cover_16, cover_43):
        _require_absent(path)

    source = Path(source_image_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    cleaned_title = str(title).strip()
    title_issues = _scan_forbidden("cover_title", cleaned_title, allowed_url=None)
    input_checks = {
        "source_authorized": bool(source_authorized),
        "source_clean_verified": bool(source_clean_verified),
        "identity_verified": bool(identity_verified),
        "text_verified": bool(text_verified),
        "title_nonempty_single_line": bool(cleaned_title)
        and "\n" not in cleaned_title
        and "\r" not in cleaned_title,
        "title_promotional_residue_zero": not title_issues,
    }
    capability = pillow_capability()
    if not capability["available"] or not all(input_checks.values()):
        report = {
            "schema_version": 1,
            "status": "unavailable" if not capability["available"] else "fail",
            "version": int(suffix),
            "generation_mode": "pillow_source_frame_relayout",
            "design_notes": str(design_notes),
            "title": cleaned_title,
            "reference_input": {
                "name": source.name,
                "sha256": sha256_file(source),
                "byte_size": source.stat().st_size,
            },
            "capability": capability,
            "checks": input_checks,
            "issues": title_issues,
            "cover_advertising_residue": len(title_issues) + (0 if source_clean_verified else 1),
        }
        _exclusive_json(report_path, report)
        return report

    try:
        with Image.open(source) as opened:  # type: ignore[union-attr]
            opened.load()
            source_image = ImageOps.exif_transpose(opened).convert("RGB")  # type: ignore[union-attr]
        rendered_16 = _render_cover(source_image, cleaned_title, "16x9")
        rendered_43 = _render_cover(source_image, cleaned_title, "4x3")
        _exclusive_bytes(cover_16, _png_bytes(rendered_16))
        _exclusive_bytes(cover_43, _png_bytes(rendered_43))
    except (OSError, ValueError) as exc:
        raise CoverGenerationError(f"Pillow cover generation failed: {exc}") from exc

    cover_refs = {
        "cover_16x9": {
            **artifact_ref(cover_16, run_root=root),
            "width": 1920,
            "height": 1080,
            "byte_size": cover_16.stat().st_size,
        },
        "cover_4x3": {
            **artifact_ref(cover_43, run_root=root),
            "width": 1440,
            "height": 1080,
            "byte_size": cover_43.stat().st_size,
        },
    }
    decoded_checks = _validate_cover_files(cover_16, cover_43)
    checks = {
        **input_checks,
        **decoded_checks,
        "safe_margins_applied": True,
        "four_by_three_relayout_not_crop": True,
        "visual_series_consistent": True,
    }
    failures = [name for name, passed in checks.items() if not passed]
    report = {
        "schema_version": 1,
        "status": "pass" if not failures else "fail",
        "version": int(suffix),
        "generation_mode": "pillow_source_frame_relayout",
        "design_notes": str(design_notes),
        "title": cleaned_title,
        "reference_input": {
            "name": source.name,
            "sha256": sha256_file(source),
            "byte_size": source.stat().st_size,
        },
        "checks": checks,
        "failure_codes": failures,
        "cover_advertising_residue": 0,
        "covers": cover_refs,
    }
    _exclusive_json(report_path, report)
    return report


def build_publication_package_gate(
    run_root: Path | str,
    version: int,
    *,
    final_video_path: Path | str,
    ad_edit_gate_path: Path | str,
    final_machine_qa_path: Path | str,
    chapter_timeline_path: Path | str,
    publication_materials_path: Path | str,
    cover_art_path: Path | str,
) -> dict[str, Any]:
    """Bind the current final media and all publication reports into one gate."""

    root = Path(run_root).resolve()
    suffix = _version(version)
    output = root / "qa" / f"publication_package_gate_v{suffix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    _require_absent(output)
    paths = {
        "final_video": Path(final_video_path).resolve(),
        "ad_edit_gate": Path(ad_edit_gate_path).resolve(),
        "final_machine_qa": Path(final_machine_qa_path).resolve(),
        "chapter_mapping": Path(chapter_timeline_path).resolve(),
        "publication_materials": Path(publication_materials_path).resolve(),
        "cover_art": Path(cover_art_path).resolve(),
    }
    for path in paths.values():
        if not path.is_file():
            raise FileNotFoundError(path)
    video_sha = sha256_file(paths["final_video"])
    ad_gate = _load_json_object(paths["ad_edit_gate"], "ad edit gate")
    machine_qa = _load_json_object(paths["final_machine_qa"], "final machine QA")
    timeline = _load_json_object(paths["chapter_mapping"], "chapter timeline")
    materials = _load_json_object(paths["publication_materials"], "publication materials")
    cover = _load_json_object(paths["cover_art"], "cover art")

    text_ref = ((materials.get("artifacts") or {}).get("publication_text") or {})
    cover_16 = ((cover.get("covers") or {}).get("cover_16x9") or {})
    cover_43 = ((cover.get("covers") or {}).get("cover_4x3") or {})
    checks: dict[str, bool] = {
        "ad_edit_gate_pass": ad_gate.get("status") == "pass",
        "final_machine_qa_pass": machine_qa.get("status") == "pass",
        "final_media_sha256_matches_machine_qa": machine_qa.get("sha256") == video_sha,
        "publication_materials_qa": materials.get("status") == "pass",
        "chapter_timeline_source_to_output_mapping_valid": timeline.get("status") == "pass"
        and bool((timeline.get("checks") or {}).get("source_to_output_mapping_valid")),
        "chapter_times_strictly_increasing": bool(
            (timeline.get("checks") or {}).get("times_strictly_increasing")
        ),
        "chapter_count_matches_approved_source": timeline.get("chapter_count")
        == timeline.get("approved_chapter_count"),
        "promotional_links_remaining_zero": materials.get(
            "advertising_or_promotional_links_remaining"
        )
        == 0,
        "cover_art_qa": cover.get("status") == "pass",
        "cover_16x9_dimensions": cover_16.get("width") == 1920
        and cover_16.get("height") == 1080,
        "cover_4x3_dimensions": cover_43.get("width") == 1440
        and cover_43.get("height") == 1080,
        "cover_text_and_identity_verified": bool((cover.get("checks") or {}).get("text_verified"))
        and bool((cover.get("checks") or {}).get("identity_verified")),
        "cover_advertising_residue_zero": cover.get("cover_advertising_residue") == 0,
        "all_artifact_hashes_match": False,
        "old_versions_preserved": all(
            _path_has_version(str(value.get("path") or ""), suffix)
            for value in (text_ref, cover_16, cover_43)
        ),
    }
    nested_refs = [
        artifact
        for payload in (ad_gate, timeline, materials, cover)
        for artifact in _walk_artifact_refs(payload)
    ]
    artifact_hashes_match = all(
        _artifact_matches(root, value)
        for value in (
            artifact_ref(paths["final_video"], run_root=root),
            artifact_ref(paths["ad_edit_gate"], run_root=root),
            artifact_ref(paths["final_machine_qa"], run_root=root),
            artifact_ref(paths["chapter_mapping"], run_root=root),
            artifact_ref(paths["publication_materials"], run_root=root),
            artifact_ref(paths["cover_art"], run_root=root),
            text_ref,
            cover_16,
            cover_43,
            *nested_refs,
        )
    )
    checks["all_artifact_hashes_match"] = artifact_hashes_match
    failure_codes = [name for name, passed in checks.items() if not passed]
    payload = {
        "schema_version": 1,
        "status": "pass" if not failure_codes else "fail",
        "version": int(suffix),
        "final_media_sha256": video_sha,
        "final_video": artifact_ref(paths["final_video"], run_root=root),
        "ad_edit_gate": artifact_ref(paths["ad_edit_gate"], run_root=root),
        "final_machine_qa": artifact_ref(paths["final_machine_qa"], run_root=root),
        "chapter_mapping": artifact_ref(paths["chapter_mapping"], run_root=root),
        "publication_materials": artifact_ref(paths["publication_materials"], run_root=root),
        "cover_art": artifact_ref(paths["cover_art"], run_root=root),
        "publication_text": {**dict(text_ref), "format": "utf8_txt", "ads_removed": True},
        "cover_16x9": dict(cover_16),
        "cover_4x3": dict(cover_43),
        "checks": checks,
        "failure_codes": failure_codes,
        "delivery_state": "ready_for_human_review" if not failure_codes else "not_deliverable",
    }
    _exclusive_json(output, payload)
    return payload


def _normalize_mapping_segments(
    mapping: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[tuple[float, float, float, float]]:
    if isinstance(mapping, Mapping):
        raw_segments = (
            mapping.get("segments")
            or mapping.get("video_retime_segments")
            or mapping.get("mapping")
            or mapping.get("intervals")
            or mapping.get("blocks")
        )
        if raw_segments is None and isinstance(mapping.get("retained_ranges"), Sequence):
            raw_segments = [
                {
                    "source_start": row["source_seconds"][0],
                    "source_end": row["source_seconds"][1],
                    "target_start": row["working_seconds"][0],
                    "target_end": row["working_seconds"][1],
                }
                for row in mapping["retained_ranges"]
                if isinstance(row, Mapping)
                and isinstance(row.get("source_seconds"), Sequence)
                and len(row["source_seconds"]) == 2
                and isinstance(row.get("working_seconds"), Sequence)
                and len(row["working_seconds"]) == 2
            ]
        if raw_segments is None and all(
            key in mapping for key in ("source_start", "source_end", "target_start", "target_end")
        ):
            raw_segments = [mapping]
    else:
        raw_segments = mapping
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)) or not raw_segments:
        raise TimelineMappingError("timeline mapping must contain non-empty segments")
    result: list[tuple[float, float, float, float]] = []
    previous_source_end = previous_target_end = -1.0
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            raise TimelineMappingError("timeline mapping segment must be an object")
        # Advertisement timelines retain removed source intervals as auditable
        # records with a single working anchor.  They are intentionally not a
        # mappable span; chapter markers inside them must fail instead of being
        # silently clamped to unrelated content.
        if raw.get("kind") == "remove":
            continue
        keys = _mapping_keys(raw)
        values = tuple(_finite_nonnegative(raw[key], f"timeline {key}") for key in keys)
        source_start, source_end, target_start, target_end = values
        if source_end <= source_start or target_end <= target_start:
            raise TimelineMappingError("timeline segments must have positive spans")
        if result and source_start < previous_source_end - 1e-7:
            raise TimelineMappingError("timeline source segments overlap or are out of order")
        if result and target_start < previous_target_end - 1e-7:
            raise TimelineMappingError("timeline target segments overlap or are out of order")
        result.append(values)
        previous_source_end = source_end
        previous_target_end = target_end
    if not result:
        raise TimelineMappingError("timeline mapping has no retained segments")
    return result


def _mapping_keys(segment: Mapping[str, Any]) -> tuple[str, str, str, str]:
    candidates = (
        ("source_start", "source_end", "target_start", "target_end"),
        ("source_start", "source_end", "working_start", "working_end"),
        ("source_start", "source_end", "edit_start", "edit_end"),
        ("original_start", "original_end", "edited_start", "edited_end"),
        ("working_start", "working_end", "output_start", "output_end"),
        ("input_start", "input_end", "output_start", "output_end"),
    )
    for keys in candidates:
        if all(key in segment for key in keys):
            return keys
    raise TimelineMappingError("timeline segment does not expose a supported coordinate pair")


def _chapter_source_seconds(chapter: Mapping[str, Any]) -> float:
    for key in ("source_seconds", "source_start", "start", "time"):
        if chapter.get(key) is not None:
            return _parse_timestamp(chapter[key])
    raise TimelineMappingError("chapter is missing a source timestamp")


def _parse_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite_nonnegative(value, "chapter timestamp")
    raw = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return _finite_nonnegative(float(raw), "chapter timestamp")
    parts = raw.split(":")
    if len(parts) not in (2, 3) or any(not re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
        raise TimelineMappingError(f"invalid chapter timestamp: {raw}")
    values = [float(part) for part in parts]
    if len(values) == 2:
        minutes, seconds = values
        hours = 0.0
    else:
        hours, minutes, seconds = values
    if minutes >= 60 or seconds >= 60:
        raise TimelineMappingError(f"invalid chapter timestamp: {raw}")
    return _finite_nonnegative(hours * 3600 + minutes * 60 + seconds, "chapter timestamp")


def _scan_forbidden(field: str, text: str, *, allowed_url: str | None) -> list[dict[str, str]]:
    scan_text = text
    if allowed_url:
        scan_text = scan_text.replace(allowed_url, " ")
    issues: list[dict[str, str]] = []
    for category, pattern in PROMOTION_PATTERNS:
        for match in pattern.finditer(scan_text):
            issues.append({"field": field, "category": category, "match": match.group(0)[:120]})
    for match in URL_RE.finditer(scan_text):
        issues.append({"field": field, "category": "non_source_url", "match": match.group(0)[:120]})
    return issues


def _valid_public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _unique_issue_dicts(issues: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (str(issue.get("field") or ""), str(issue.get("category") or ""), str(issue.get("match") or ""))
        if key not in seen:
            seen.add(key)
            result.append({"field": key[0], "category": key[1], "match": key[2]})
    return result


def _promotional_issue_count(issues: Iterable[Mapping[str, str]]) -> int:
    promotional_categories = {name for name, _pattern in PROMOTION_PATTERNS} | {"non_source_url"}
    return sum(1 for issue in issues if str(issue.get("category") or "") in promotional_categories)


def _render_cover(source: Any, title: str, variant: str) -> Any:
    spec = COVER_SPECS[variant]
    width, height = int(spec["width"]), int(spec["height"])
    margin = int(spec["safe_margin"])
    canvas = Image.new("RGB", (width, height), "#17201D")  # type: ignore[union-attr]
    draw = ImageDraw.Draw(canvas)  # type: ignore[union-attr]
    if variant == "16x9":
        image_box = (margin, margin, int(width * 0.58), height - margin)
        text_box = (int(width * 0.62), margin, width - margin, height - margin)
    else:
        image_box = (margin, margin, width - margin, int(height * 0.61))
        text_box = (margin, int(height * 0.67), width - margin, height - margin)
    _paste_contained(canvas, source, image_box)
    accent_y = text_box[1]
    draw.rounded_rectangle(
        (text_box[0], accent_y, min(text_box[0] + 180, text_box[2]), accent_y + 14),
        radius=7,
        fill="#D7FF63",
    )
    content_box = (text_box[0], accent_y + 48, text_box[2], text_box[3])
    font, lines = _fit_title(title, content_box)
    line_height = _font_line_height(font)
    total_height = line_height * len(lines) + max(0, len(lines) - 1) * 16
    y = content_box[1] + max(0, (content_box[3] - content_box[1] - total_height) // 2)
    for line in lines:
        draw.text((content_box[0], y), line, font=font, fill="#F3F5F1")
        y += line_height + 16
    return canvas


def _paste_contained(canvas: Any, source: Any, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    region_width, region_height = right - left, bottom - top
    frame = Image.new("RGB", (region_width, region_height), "#28322E")  # type: ignore[union-attr]
    fitted = ImageOps.contain(source, (region_width, region_height), method=Image.Resampling.LANCZOS)  # type: ignore[union-attr]
    x = (region_width - fitted.width) // 2
    y = (region_height - fitted.height) // 2
    frame.paste(fitted, (x, y))
    canvas.paste(frame, (left, top))


def _fit_title(title: str, box: tuple[int, int, int, int]) -> tuple[Any, list[str]]:
    max_width = box[2] - box[0]
    max_height = box[3] - box[1]
    for size in range(88, 31, -4):
        font = _load_font(size)
        lines = _wrap_text(title, font, max_width, max_lines=5)
        height = _font_line_height(font) * len(lines) + max(0, len(lines) - 1) * 16
        if lines and height <= max_height and all(_text_width(line, font) <= max_width for line in lines):
            return font, lines
    raise CoverGenerationError("cover title is too long for the safe text region")


def _load_font(size: int) -> Any:
    candidates = (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyhbd.ttc",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)  # type: ignore[union-attr]
            except OSError:
                continue
    return ImageFont.load_default(size=size)  # type: ignore[union-attr]


def _wrap_text(text: str, font: Any, max_width: int, *, max_lines: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and _text_width(candidate, font) > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    return lines if len(lines) <= max_lines else []


def _text_width(text: str, font: Any) -> int:
    left, _top, right, _bottom = font.getbbox(text)
    return int(right - left)


def _font_line_height(font: Any) -> int:
    left, top, right, bottom = font.getbbox("国Ag")
    del left, right
    return int(bottom - top)


def _png_bytes(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _validate_cover_files(cover_16: Path, cover_43: Path) -> dict[str, bool]:
    expected = ((cover_16, (1920, 1080)), (cover_43, (1440, 1080)))
    results: dict[str, bool] = {}
    for path, size in expected:
        key = "cover_16x9" if size[0] == 1920 else "cover_4x3"
        try:
            with Image.open(path) as opened:  # type: ignore[union-attr]
                opened.verify()
            with Image.open(path) as opened:  # type: ignore[union-attr]
                results[f"{key}_png_decodable"] = opened.format == "PNG"
                results[f"{key}_dimensions"] = opened.size == size
        except OSError:
            results[f"{key}_png_decodable"] = False
            results[f"{key}_dimensions"] = False
    return results


def _machine_qa_duration(payload: Mapping[str, Any]) -> float:
    for candidate in (
        payload.get("duration"),
        (payload.get("observed") or {}).get("duration") if isinstance(payload.get("observed"), Mapping) else None,
        (payload.get("expected") or {}).get("duration") if isinstance(payload.get("expected"), Mapping) else None,
    ):
        if candidate is not None:
            return _finite_positive(candidate, "final machine QA duration")
    raise PublicationError("final machine QA does not record the final duration")


def _chapters_strict(chapters: Sequence[Mapping[str, Any]]) -> bool:
    try:
        values = [float(row["final_seconds"]) for row in chapters]
    except (KeyError, TypeError, ValueError):
        return False
    return bool(values) and all(current > previous for previous, current in pairwise(values))


def _artifact_matches(root: Path, artifact: Any) -> bool:
    if not isinstance(artifact, Mapping):
        return False
    raw_path = artifact.get("path")
    expected = artifact.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        return False
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_file() and sha256_file(candidate) == expected


def _walk_artifact_refs(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if (
            isinstance(value.get("path"), str)
            and isinstance(value.get("sha256"), str)
            and SHA256_RE.fullmatch(str(value.get("sha256")))
        ):
            yield value
        for child in value.values():
            yield from _walk_artifact_refs(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk_artifact_refs(child)


def _path_has_version(path: str, version: int) -> bool:
    return bool(re.search(rf"_v{version}(?:\.|_)", Path(path).name))


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be a JSON object")
    return value


def _finite_nonnegative(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TimelineMappingError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise TimelineMappingError(f"{label} must be finite and non-negative")
    return number


def _finite_positive(value: Any, label: str) -> float:
    number = _finite_nonnegative(value, label)
    if number <= 0:
        raise TimelineMappingError(f"{label} must be positive")
    return number


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PublicationError(f"{label} SHA-256 is invalid")


def _version(value: int) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise PublicationError("version must be a positive integer") from exc
    if version <= 0 or isinstance(value, bool):
        raise PublicationError("version must be a positive integer")
    return version


def _safe_video_id(value: str) -> str:
    video_id = str(value).strip()
    windows_reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        not video_id
        or video_id in {".", ".."}
        or video_id.endswith(".")
        or video_id.split(".", 1)[0].upper() in windows_reserved
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", video_id)
    ):
        raise PublicationError("video_id must be a portable filename identifier")
    return video_id


def _require_absent(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"versioned publication artifact already exists: {path}")


def _exclusive_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _exclusive_utf8(path: Path, text: str) -> None:
    if "\x00" in text:
        raise PublicationContentError("publication text contains a NUL byte")
    _exclusive_bytes(path, text.encode("utf-8"))


def _exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _exclusive_bytes(path, encoded)


__all__ = [
    "COVER_SPECS",
    "ContentValidation",
    "CoverGenerationError",
    "PublicationContentError",
    "PublicationError",
    "TimelineMappingError",
    "artifact_ref",
    "build_final_chapter_timeline",
    "build_publication_package_gate",
    "create_cover_art",
    "create_publication_materials",
    "format_timestamp",
    "map_timestamp",
    "pillow_capability",
    "sha256_file",
    "validate_publication_content",
    "write_final_chapter_timeline",
]
