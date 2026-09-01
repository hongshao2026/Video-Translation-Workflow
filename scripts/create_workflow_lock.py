from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_DOCUMENTS = (
    "AGENTS.md",
    "docs/LOCAL_DUBBING_WORKFLOW.md",
    "docs/TRANSLATION_REVIEW_SOP.md",
    "docs/AD_DETECTION_AND_OVERLAY_SOP.md",
    "docs/workflow.definition.json",
)

POLICIES = {
    "ad_policy": "detect_then_apply_evidence_based",
    "translation_mode": "codex_agent_direct_quality_first",
    "translation_review": "two_independent_agents_full_coverage",
    "chapter_reading_review": "required_before_translation_gate",
    "chapter_reading_layout": "sentence_aligned_verbatim",
    "chinese_tts_speed": 1.0,
    "chinese_offline_rate": 1.0,
    "sync_strategy": "video_retime_only",
    "preserve_all_formal_working_master_frames": True,
    "subtitle_timeline": "rebuilt_from_chinese_audio",
    "audition_authorization": "voice_selection_implies_audition",
    "full_tts_authorization": "user_generate_full_command",
    "publication_package": "required_after_final_machine_qa",
    "publication_text_format": "utf8_txt_only",
    "cover_variants": "16x9_and_4x3",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a hash-bound workflow lock for one local run.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--next-gate", required=True)
    parser.add_argument(
        "--frozen-input",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="Optional frozen input to hash. Repeat for multiple files.",
    )
    parser.add_argument("--check", action="store_true", help="Check the existing lock instead of rewriting it.")
    return parser.parse_args()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def project_constraints(project_path: Path) -> bytes:
    text = project_path.read_text(encoding="utf-8")
    for marker in ("\n## 当前状态", "\n## Current status"):
        if marker in text:
            text = text.split(marker, 1)[0]
            break
    return text.rstrip().encode("utf-8") + b"\n"


def display_path(path: Path, repo_root: Path, run_dir: Path) -> str:
    resolved = path.resolve()
    for base, prefix in ((repo_root, "repo"), (run_dir, "run")):
        try:
            relative = resolved.relative_to(base.resolve())
            return f"{prefix}/{relative.as_posix()}"
        except ValueError:
            continue
    return f"external/{path.name}"


def parse_frozen_inputs(values: list[str], repo_root: Path, run_dir: Path) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for value in values:
        if "=" not in value:
            errors.append(f"Invalid --frozen-input value: {value}")
            continue
        label, raw_path = value.split("=", 1)
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = run_dir / path
        if not label.strip() or not path.is_file():
            errors.append(f"Missing frozen input: {label}={path}")
            continue
        rows.append(
            {
                "label": label.strip(),
                "path": display_path(path, repo_root, run_dir),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return rows, errors


def build_payload(args: argparse.Namespace) -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    run_dir = args.run_dir.resolve()
    project_path = run_dir / "PROJECT.md"
    errors: list[str] = []

    documents: list[dict] = []
    for relative in REQUIRED_DOCUMENTS:
        path = repo_root / relative
        if not path.is_file():
            errors.append(f"Missing required document: {relative}")
            continue
        documents.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )

    project: dict = {"path": "run/PROJECT.md"}
    if project_path.is_file():
        project.update(
            {
                "constraints_sha256": sha256_bytes(project_constraints(project_path)),
                "file_sha256_at_generation": sha256_file(project_path),
                "bytes": project_path.stat().st_size,
            }
        )
    else:
        errors.append(f"Missing project file: {project_path}")

    frozen_inputs, input_errors = parse_frozen_inputs(args.frozen_input, repo_root, run_dir)
    errors.extend(input_errors)

    workflow_definition = repo_root / "docs" / "workflow.definition.json"
    schema_version = None
    if workflow_definition.is_file():
        try:
            schema_version = json.loads(workflow_definition.read_text(encoding="utf-8"))["schema_version"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            errors.append(f"Invalid workflow definition: {exc}")

    return {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workflow_schema_version": schema_version,
        "current_stage": args.stage,
        "next_gate": args.next_gate,
        "required_documents": documents,
        "project": project,
        "frozen_inputs": frozen_inputs,
        "policies": POLICIES,
        "errors": errors,
        "notes": [
            "The Current status section of PROJECT.md is excluded from constraints_sha256.",
            "A status-only progress update does not invalidate this lock.",
            "Credentials and media URLs are intentionally absent.",
        ],
    }


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def comparable(payload: dict) -> dict:
    result = {key: value for key, value in payload.items() if key not in {"generated_at", "notes"}}
    project = dict(result.get("project") or {})
    project.pop("file_sha256_at_generation", None)
    project.pop("bytes", None)
    result["project"] = project
    return result


def main() -> int:
    args = parse_args()
    lock_path = args.run_dir.resolve() / "qa" / "workflow_lock.json"
    expected = build_payload(args)

    if args.check:
        if not lock_path.is_file():
            print(f"FAIL: lock does not exist: {lock_path}")
            return 1
        try:
            actual = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"FAIL: invalid lock JSON: {exc}")
            return 1
        if comparable(actual) != comparable(expected):
            print("FAIL: workflow lock does not match current constraints or frozen inputs.")
            return 1
        print(f"PASS: workflow lock is current: {lock_path}")
        return 0

    atomic_write_json(lock_path, expected)
    print(f"{expected['status'].upper()}: wrote workflow lock: {lock_path}")
    for error in expected["errors"]:
        print(f"- {error}")
    return 0 if expected["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
