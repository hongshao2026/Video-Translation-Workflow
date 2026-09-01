from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .init_project import VIDEO_ID_RE, initialize_project
except ImportError:
    from init_project import VIDEO_ID_RE, initialize_project


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a local YouTube Cookie file and build a ready-to-paste Codex workflow prompt."
    )
    parser.add_argument("--url", required=True, help="YouTube video URL.")
    parser.add_argument("--cookie-file", required=True, type=Path, help="Local Netscape Cookie file path.")
    parser.add_argument("--video-id", help="Optional explicit video ID when it cannot be inferred from the URL.")
    parser.add_argument("--source-language", default="auto")
    parser.add_argument("--target-language", default="zh-CN")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--force", action="store_true", help="Replace an existing generated CODEX_PROMPT.md.")
    parser.add_argument("--print", action="store_true", dest="print_prompt", help="Print the generated prompt.")
    return parser.parse_args()


def extract_video_id(source_url: str, explicit_video_id: str | None = None) -> str:
    if explicit_video_id:
        if not VIDEO_ID_RE.fullmatch(explicit_video_id):
            raise ValueError("--video-id contains unsupported characters.")
        return explicit_video_id

    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                candidate = parts[1]
    else:
        raise ValueError("--url must point to youtube.com or youtu.be.")

    if not VIDEO_ID_RE.fullmatch(candidate):
        raise ValueError("Could not infer a valid video ID; pass --video-id explicitly.")
    return candidate


def validate_cookie_file(cookie_file: Path) -> Path:
    path = cookie_file.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Cookie file does not exist: {path}")
    size = path.stat().st_size
    if size <= 0 or size > 50 * 1024 * 1024:
        raise ValueError("Cookie file size is invalid.")

    has_netscape_header = False
    has_youtube_domain = False
    with path.open("r", encoding="utf-8-sig", errors="strict") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if "Netscape HTTP Cookie File" in line:
                has_netscape_header = True
            if line.startswith("#HttpOnly_"):
                line = line.removeprefix("#HttpOnly_")
            elif not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                domain = fields[0].lstrip(".").lower()
                if domain == "youtube.com" or domain.endswith(".youtube.com"):
                    has_youtube_domain = True

    if not has_netscape_header:
        raise ValueError("Cookie file is missing the Netscape header.")
    if not has_youtube_domain:
        raise ValueError("Cookie file contains no youtube.com domain row.")
    return path


def normalized_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def render_prompt(
    template: str,
    video_id: str,
    source_url: str,
    source_language: str,
    target_language: str,
    run_dir: Path,
    cookie_file: Path,
) -> str:
    replacements = {
        "{{VIDEO_ID}}": video_id,
        "{{SOURCE_URL}}": source_url,
        "{{SOURCE_LANGUAGE}}": source_language,
        "{{TARGET_LANGUAGE}}": target_language,
        "{{RUN_DIR}}": run_dir.name,
        "{{COOKIE_FILE}}": str(cookie_file),
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    unresolved = re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)
    if unresolved:
        raise ValueError(f"Unresolved prompt template markers: {', '.join(unresolved)}")
    return rendered.rstrip() + "\n"


def main() -> int:
    args = parse_args()
    try:
        video_id = extract_video_id(args.url, args.video_id)
        cookie_file = validate_cookie_file(args.cookie_file)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"Input validation failed: {exc}") from exc

    repo_root = Path(__file__).resolve().parents[1]
    workspace = args.workspace.resolve()
    run_dir = workspace / f"{video_id}_run"
    project_path = run_dir / "PROJECT.md"
    source_url = normalized_youtube_url(video_id)

    if not project_path.exists():
        run_dir = initialize_project(
            video_id=video_id,
            source_url=source_url,
            source_language=args.source_language,
            target_language=args.target_language,
            workspace=workspace,
            repo_root=repo_root,
        )

    template_path = repo_root / "templates" / "CODEX_PROMPT.template.md"
    prompt = render_prompt(
        template=template_path.read_text(encoding="utf-8"),
        video_id=video_id,
        source_url=source_url,
        source_language=args.source_language,
        target_language=args.target_language,
        run_dir=run_dir,
        cookie_file=cookie_file,
    )
    prompt_path = run_dir / "CODEX_PROMPT.md"
    if prompt_path.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite existing prompt; use --force: {prompt_path}")
    prompt_path.write_text(prompt, encoding="utf-8", newline="\n")

    print("Cookie validation PASS: Netscape header and youtube.com domain found; values were not printed.")
    print(f"Project directory: {run_dir}")
    print(f"Codex prompt: {prompt_path}")
    if args.print_prompt:
        print("\n--- GENERATED CODEX PROMPT ---\n")
        print(prompt, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
