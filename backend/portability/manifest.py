"""Create and verify portable, credential-free project transfer archives.

The archive is deliberately a cold project snapshot, not a live runner handoff.
All manifest paths are POSIX-style paths relative to the project root. Runtime
process state, device bindings, credentials and opaque archives are never
included. Large media is opt-in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

SCHEMA_VERSION = "dub-workbench-project-transfer/v1"
MANIFEST_NAME = "transfer-manifest.json"
PROJECT_PREFIX = "project/"
COPY_CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_ENTRIES = 250_000

MEDIA_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".avi",
    ".bmp",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mxf",
    ".npy",
    ".npz",
    ".ogg",
    ".onnx",
    ".opus",
    ".pcm",
    ".png",
    ".pt",
    ".pth",
    ".raw",
    ".safetensors",
    ".tif",
    ".tiff",
    ".wav",
    ".webm",
    ".webp",
}

OPAQUE_ARCHIVE_EXTENSIONS = {".7z", ".gz", ".rar", ".tar", ".tgz", ".zip"}
DEVICE_STATE_EXTENSIONS = {".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3"}
EXECUTABLE_BINARY_EXTENSIONS = {".dll", ".dylib", ".exe", ".msi", ".pyd", ".so"}

EXCLUDED_DIRECTORY_NAMES = {
    ".codex",
    ".device",
    ".git",
    ".idea",
    ".next",
    ".pytest_cache",
    ".venv",
    ".venv_media",
    ".vinext",
    ".vscode",
    "__pycache__",
    "node_modules",
    "runtime",
    "venv",
}

SECRET_NAME_MARKERS = (
    "access_token",
    "api_key",
    "apikey",
    "client_secret",
    "cookie",
    "credential",
    "minimax_api",
    "oauth_token",
    "openai_api",
    "private_key",
    "refresh_token",
)

SECRET_EXACT_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}

SECRET_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
DEVICE_LOCAL_PATTERNS = (
    re.compile(r"^workbench_config(?:_.+)?\.json$", re.IGNORECASE),
    re.compile(r"^.+\.local\.json$", re.IGNORECASE),
)


class PortabilityError(ValueError):
    """A safe, user-actionable transfer failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256_stream(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(COPY_CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)[0]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_junction(path: Path) -> bool:
    """Reject links and Windows junctions so exports cannot escape the root."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _is_secret_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered in SECRET_EXACT_NAMES or lowered.startswith(".env."):
        return True
    if Path(lowered).suffix in SECRET_SUFFIXES:
        return True
    return any(marker in lowered for marker in SECRET_NAME_MARKERS)


def _is_device_local_name(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in DEVICE_LOCAL_PATTERNS)


def _file_exclusion_reason(relative: Path, include_media: bool) -> str | None:
    name = relative.name
    lowered_parts = {part.casefold() for part in relative.parts[:-1]}
    if lowered_parts & EXCLUDED_DIRECTORY_NAMES:
        return "runtime_or_dependency"
    if name == MANIFEST_NAME or ".dub-workbench" in lowered_parts:
        return "runtime_or_dependency"
    if any(_is_secret_name(part) for part in relative.parts):
        return "secret_or_credential"
    if _is_device_local_name(name):
        return "device_binding"
    suffix = relative.suffix.casefold()
    if suffix in OPAQUE_ARCHIVE_EXTENSIONS:
        return "opaque_archive"
    if suffix in DEVICE_STATE_EXTENSIONS or suffix in EXECUTABLE_BINARY_EXTENSIONS:
        return "runtime_or_dependency"
    if suffix in MEDIA_EXTENSIONS and not include_media:
        return "media"
    return None


def _walk_project(
    project_root: Path, include_media: bool
) -> tuple[list[Path], dict[str, int]]:
    included: list[Path] = []
    excluded = {
        "device_binding": 0,
        "media": 0,
        "opaque_archive": 0,
        "runtime_or_dependency": 0,
        "secret_or_credential": 0,
        "symlink_or_reparse_point": 0,
    }

    for current_root, directory_names, file_names in os.walk(
        project_root, topdown=True, followlinks=False
    ):
        current = Path(current_root)
        retained_directories: list[str] = []
        for directory_name in directory_names:
            candidate = current / directory_name
            relative = candidate.relative_to(project_root)
            if _is_link_or_junction(candidate):
                excluded["symlink_or_reparse_point"] += 1
            elif directory_name.casefold() in EXCLUDED_DIRECTORY_NAMES:
                excluded["runtime_or_dependency"] += 1
            elif reason := _file_exclusion_reason(relative / "placeholder", include_media):
                excluded[reason] += 1
            else:
                retained_directories.append(directory_name)
        directory_names[:] = retained_directories

        for file_name in file_names:
            candidate = current / file_name
            relative = candidate.relative_to(project_root)
            if _is_link_or_junction(candidate) or not candidate.is_file():
                excluded["symlink_or_reparse_point"] += 1
                continue
            reason = _file_exclusion_reason(relative, include_media)
            if reason:
                excluded[reason] += 1
                continue
            included.append(candidate)

    included.sort(key=lambda path: path.relative_to(project_root).as_posix())
    return included, excluded


def build_manifest(project_root: Path, *, include_media: bool = False) -> dict[str, Any]:
    """Build a content manifest without recording an absolute source path."""

    root = project_root.expanduser().resolve()
    if not root.is_dir():
        raise PortabilityError("project_missing", "Project root is not a readable directory.")

    files, excluded = _walk_project(root, include_media)
    if len(files) > MAX_MANIFEST_ENTRIES:
        raise PortabilityError("too_many_files", "Project contains too many transferable files.")

    entries: list[dict[str, Any]] = []
    total_size = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_size += size
        entries.append(
            {
                "media": path.suffix.casefold() in MEDIA_EXTENSIONS,
                "path": relative,
                "sha256": sha256_file(path),
                "size": size,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "project_name": root.name,
        "options": {"media_included": include_media},
        "summary": {
            "file_count": len(entries),
            "total_bytes": total_size,
            "excluded_counts": excluded,
        },
        "files": entries,
    }


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def export_project(
    project_root: Path,
    archive_path: Path,
    *,
    include_media: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write an atomic ZIP transfer package and return a secret-free summary."""

    root = project_root.expanduser().resolve()
    requested_output = archive_path.expanduser().absolute()
    if _is_link_or_junction(requested_output):
        raise PortabilityError("unsafe_archive_target", "Transfer archive target cannot be a link.")
    output = requested_output.resolve()
    if _is_relative_to(output, root):
        raise PortabilityError(
            "archive_inside_project", "Transfer archive must be outside the project root."
        )
    if output.exists() and not overwrite:
        raise PortabilityError("archive_exists", "Transfer archive already exists.")
    if not output.parent.is_dir():
        raise PortabilityError("output_parent_missing", "Archive parent directory is missing.")

    manifest = build_manifest(root, include_media=include_media)
    temporary = output.parent / f".{output.name}.tmp-{uuid.uuid4().hex}"
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            archive.writestr(MANIFEST_NAME, _manifest_bytes(manifest))
            for entry in manifest["files"]:
                source = root / PurePosixPath(entry["path"])
                archive.write(source, f"{PROJECT_PREFIX}{entry['path']}")
        verify_archive(temporary)
        os.replace(temporary, output)
    except PortabilityError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        temporary.unlink(missing_ok=True)
        raise PortabilityError("archive_write_failed", "Could not write transfer archive.") from exc

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "archive_sha256": sha256_file(output),
        "file_count": manifest["summary"]["file_count"],
        "total_bytes": manifest["summary"]["total_bytes"],
        "media_included": include_media,
        "excluded_counts": manifest["summary"]["excluded_counts"],
    }


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise PortabilityError("invalid_manifest_path", "Manifest contains an invalid path.")
    if "\\" in value or "\x00" in value:
        raise PortabilityError("invalid_manifest_path", "Manifest paths must use POSIX separators.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortabilityError("unsafe_manifest_path", "Manifest contains an unsafe path.")
    if any(":" in part for part in path.parts):
        raise PortabilityError("unsafe_manifest_path", "Manifest contains a device-qualified path.")
    return path.as_posix()


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise PortabilityError("unsupported_manifest", "Unsupported transfer manifest version.")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) > MAX_MANIFEST_ENTRIES:
        raise PortabilityError("invalid_manifest", "Transfer manifest file list is invalid.")

    exact_paths: set[str] = set()
    portable_paths: set[str] = set()
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise PortabilityError("invalid_manifest", "Transfer manifest entry is invalid.")
        relative = _validate_relative_path(entry.get("path"))
        portable_key = relative.casefold()
        if relative in exact_paths or portable_key in portable_paths:
            raise PortabilityError("duplicate_manifest_path", "Manifest contains duplicate paths.")
        exact_paths.add(relative)
        portable_paths.add(portable_key)
        size = entry.get("size")
        digest = entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PortabilityError("invalid_manifest_size", "Manifest contains an invalid file size.")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PortabilityError("invalid_manifest_hash", "Manifest contains an invalid SHA-256.")
        if _file_exclusion_reason(Path(*PurePosixPath(relative).parts), include_media=True):
            raise PortabilityError("forbidden_manifest_file", "Manifest includes a forbidden file.")
        total_size += size

    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        raise PortabilityError("invalid_manifest_summary", "Manifest summary is missing.")
    if summary.get("file_count") != len(entries) or summary.get("total_bytes") != total_size:
        raise PortabilityError("manifest_summary_mismatch", "Manifest summary does not match entries.")
    return manifest


def _zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise PortabilityError("duplicate_archive_member", "Archive has duplicate members.")
    result: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = _validate_relative_path(info.filename)
        mode = info.external_attr >> 16
        if info.is_dir() or stat.S_ISLNK(mode):
            raise PortabilityError("unsafe_archive_member", "Archive contains an unsafe member.")
        result[name] = info
    return result


def _read_manifest(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]) -> tuple[dict[str, Any], bytes]:
    info = members.get(MANIFEST_NAME)
    if info is None or info.file_size > 64 * 1024 * 1024:
        raise PortabilityError("manifest_missing", "Transfer manifest is missing or invalid.")
    try:
        raw = archive.read(info)
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise PortabilityError("manifest_unreadable", "Transfer manifest is unreadable.") from exc
    return _validate_manifest(manifest), raw


def _verify_open_archive(archive: zipfile.ZipFile) -> tuple[dict[str, Any], bytes]:
    members = _zip_members(archive)
    manifest, raw_manifest = _read_manifest(archive, members)
    expected_members = {MANIFEST_NAME}
    for entry in manifest["files"]:
        member_name = f"{PROJECT_PREFIX}{entry['path']}"
        expected_members.add(member_name)
        info = members.get(member_name)
        if info is None or info.file_size != entry["size"]:
            raise PortabilityError("archive_size_mismatch", "Archive content does not match its manifest.")
        with archive.open(info, "r") as source:
            digest, size = _sha256_stream(source)
        if size != entry["size"] or digest != entry["sha256"]:
            raise PortabilityError("archive_hash_mismatch", "Archive content failed SHA-256 verification.")
    if set(members) != expected_members:
        raise PortabilityError("unexpected_archive_member", "Archive contains unmanifested content.")
    return manifest, raw_manifest


def verify_archive(archive_path: Path) -> dict[str, Any]:
    """Verify every member without extracting it."""

    path = archive_path.expanduser().resolve()
    if not path.is_file():
        raise PortabilityError("archive_missing", "Transfer archive does not exist.")
    try:
        with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
            manifest, raw_manifest = _verify_open_archive(archive)
    except PortabilityError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PortabilityError("archive_unreadable", "Transfer archive is unreadable.") from exc
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "archive_sha256": sha256_file(path),
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "file_count": manifest["summary"]["file_count"],
        "total_bytes": manifest["summary"]["total_bytes"],
        "media_included": bool(manifest.get("options", {}).get("media_included")),
    }


def _copy_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info, "r") as source, destination.open("xb") as target:
        while chunk := source.read(COPY_CHUNK_SIZE):
            target.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def import_project(archive_path: Path, destination: Path) -> dict[str, Any]:
    """Verify, extract to a sibling temporary directory, then atomically install."""

    archive_path = archive_path.expanduser().resolve()
    requested_target = destination.expanduser().absolute()
    if _is_link_or_junction(requested_target):
        raise PortabilityError("unsafe_destination", "Import destination cannot be a link.")
    target = requested_target.resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise PortabilityError("destination_not_empty", "Import destination must be absent or empty.")
    if not target.parent.is_dir():
        raise PortabilityError("destination_parent_missing", "Import destination parent is missing.")

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.import-", dir=str(target.parent))
    )
    try:
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            members = _zip_members(archive)
            manifest, raw_manifest = _read_manifest(archive, members)
            expected_members = {MANIFEST_NAME}
            for entry in manifest["files"]:
                member_name = f"{PROJECT_PREFIX}{entry['path']}"
                expected_members.add(member_name)
                info = members.get(member_name)
                if info is None or info.file_size != entry["size"]:
                    raise PortabilityError("archive_size_mismatch", "Archive content does not match its manifest.")
                relative = PurePosixPath(entry["path"])
                output = temporary.joinpath(*relative.parts)
                digest, size = _copy_member(archive, info, output)
                if size != entry["size"] or digest != entry["sha256"]:
                    raise PortabilityError("archive_hash_mismatch", "Archive content failed SHA-256 verification.")
            if set(members) != expected_members:
                raise PortabilityError("unexpected_archive_member", "Archive contains unmanifested content.")

        receipt_dir = temporary / ".dub-workbench"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": "dub-workbench-import-receipt/v1",
            "imported_at": _utc_now(),
            "verified": True,
            "archive_sha256": sha256_file(archive_path),
            "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "file_count": manifest["summary"]["file_count"],
            "total_bytes": manifest["summary"]["total_bytes"],
            "media_included": bool(manifest.get("options", {}).get("media_included")),
            "rebind_required": True,
        }
        (receipt_dir / "import-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if target.exists():
            target.rmdir()  # Only an already-verified empty directory reaches this point.
        os.replace(temporary, target)
    except PortabilityError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise PortabilityError("import_failed", "Project import failed without modifying the destination.") from exc

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "archive_sha256": receipt["archive_sha256"],
        "manifest_sha256": receipt["manifest_sha256"],
        "file_count": receipt["file_count"],
        "total_bytes": receipt["total_bytes"],
        "media_included": receipt["media_included"],
        "rebind_required": True,
    }
