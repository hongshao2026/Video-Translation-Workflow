from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
FORBIDDEN_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
    ".srt", ".vtt", ".ass",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".zip", ".7z", ".tar", ".gz",
    ".bin", ".pt", ".pth", ".ckpt", ".safetensors",
}
FORBIDDEN_DIRECTORY_PATTERNS = (
    re.compile(r".+_run$", re.IGNORECASE),
    re.compile(r"^(source|work|qa|deliverables|outputs?|data|cache|logs|tts)$", re.IGNORECASE),
)
FORBIDDEN_NAME_PATTERNS = (
    re.compile(r"(^|[._-])cookies?([._-]|$)", re.IGNORECASE),
    re.compile(r"api[._-]?key", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"(^|[._-])secret([._-]|$)", re.IGNORECASE),
    re.compile(r"(^|[._-])token([._-]|$)", re.IGNORECASE),
)
SENSITIVE_CONTENT_PATTERNS = (
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Bearer credential", re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)),
    ("signed media URL", re.compile(r"https?://[^\s]+googlevideo\.com/[^\s]+", re.IGNORECASE)),
    ("YouTube Netscape Cookie row", re.compile(r"(?m)^(?:#HttpOnly_)?\.?youtube\.com\t(?:TRUE|FALSE)\t", re.IGNORECASE)),
    ("personal Windows path", re.compile(r"[A-Za-z]:\\Users\\(?!<|\$env:USERPROFILE)[^\\\s]+\\", re.IGNORECASE)),
    ("personal macOS path", re.compile(re.escape("/" + "Users/") + r"(?!<)[^/\s]+/")),
    ("personal Linux path", re.compile(re.escape("/" + "home/") + r"(?!<)[^/\s]+/")),
)
TEXT_SUFFIXES = {".md", ".txt", ".json", ".py", ".ps1", ".sh", ".yml", ".yaml", ".toml", ".ini"}


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPO_ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    files = candidate_files()

    for path in files:
        relative = path.relative_to(REPO_ROOT)
        for directory_name in relative.parts[:-1]:
            if any(pattern.fullmatch(directory_name) for pattern in FORBIDDEN_DIRECTORY_PATTERNS):
                findings.append(f"forbidden runtime directory in candidate path: {relative}")
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            findings.append(f"forbidden binary/media extension: {relative}")
        if path.stat().st_size > MAX_FILE_BYTES:
            findings.append(f"file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        if path.name != ".env.example" and any(pattern.search(path.name) for pattern in FORBIDDEN_NAME_PATTERNS):
            findings.append(f"sensitive filename: {relative}")

        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"AGENTS.md", ".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"non-UTF-8 text file: {relative}")
            continue
        for label, pattern in SENSITIVE_CONTENT_PATTERNS:
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
        if path.suffix.lower() == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                findings.append(f"invalid JSON ({exc}): {relative}")

    if findings:
        print("Repository audit FAILED:")
        for finding in sorted(set(findings)):
            print(f"- {finding}")
        return 1

    print(f"Repository audit PASS: {len(files)} files checked; no credentials, media, run data or oversized files found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
