from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
RUN_SUBDIRECTORIES = (
    "source",
    "work",
    "qa",
    "deliverables/covers",
    "scripts",
    "tts",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize a local video translation run directory.")
    parser.add_argument("video_id", help="Stable video ID used in the run directory name.")
    parser.add_argument("--url", required=True, help="Source page URL. Stored only in the ignored local run directory.")
    parser.add_argument("--source-language", default="auto", help="BCP-47 or short source-language label.")
    parser.add_argument("--target-language", default="zh-CN", help="Target-language label. Default: zh-CN.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Directory that will contain <video_id>_run.")
    return parser.parse_args()


def validate_inputs(video_id: str, source_url: str) -> None:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise SystemExit("video_id may contain only letters, digits, underscore and hyphen (4-64 characters).")
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--url must be a valid http(s) URL.")


def initialize_project(
    video_id: str,
    source_url: str,
    source_language: str,
    target_language: str,
    workspace: Path,
    repo_root: Path | None = None,
) -> Path:
    validate_inputs(video_id, source_url)
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    template_path = repo_root / "templates" / "PROJECT.template.md"
    workspace = workspace.resolve()
    run_dir = workspace / f"{video_id}_run"
    project_path = run_dir / "PROJECT.md"

    if project_path.exists():
        raise SystemExit(f"Refusing to overwrite existing project: {project_path}")

    for relative in RUN_SUBDIRECTORIES:
        (run_dir / relative).mkdir(parents=True, exist_ok=True)

    rendered = template_path.read_text(encoding="utf-8")
    replacements = {
        "{{VIDEO_ID}}": video_id,
        "{{SOURCE_URL}}": source_url,
        "{{SOURCE_LANGUAGE}}": source_language,
        "{{TARGET_LANGUAGE}}": target_language,
        "{{CREATED_AT}}": datetime.now(timezone.utc).isoformat(),
    }
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    project_path.write_text(rendered, encoding="utf-8", newline="\n")
    return run_dir


def main() -> int:
    args = parse_args()
    run_dir = initialize_project(
        video_id=args.video_id,
        source_url=args.url,
        source_language=args.source_language,
        target_language=args.target_language,
        workspace=args.workspace,
    )

    print(f"Initialized local run directory: {run_dir}")
    print("Next: create qa/workflow_lock.json with scripts/create_workflow_lock.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
