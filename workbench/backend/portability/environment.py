"""Path-free environment diagnostics used when a project moves devices."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from .manifest import PortabilityError

EXPECTED_PACKAGES = {
    "fastapi": "0.141.1",
    "pydantic": "2.13.4",
    "python-multipart": "0.0.32",
    "requests": "2.34.2",
    "tzdata": "2026.3",
    "uvicorn": "0.52.4",
    "yt-dlp": "2026.8.19",
}

REQUIRED_COMMANDS = ("node", "npm", "ffmpeg", "ffprobe")
OPTIONAL_COMMANDS = ("yt-dlp",)
MODEL_ENVIRONMENT = (
    "DUB_QWEN_MODEL_DIR",
    "DUB_KOKORO_MODEL_DIR",
    "DUB_COSYVOICE_MODEL_DIR",
    "DUB_COSYVOICE_REPO_DIR",
)
PROJECT_FILE_FIELDS = (
    "source_video",
    "background_audio",
    "translation",
    "translation_gate",
    "audition_items",
)
PROVIDER_ENVIRONMENT = {
    "minimax": ("MINIMAX_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _check(
    check_id: str,
    status: str,
    *,
    required: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "required": required,
        "detail": detail,
    }


def _package_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for package_name, expected in EXPECTED_PACKAGES.items():
        try:
            installed = metadata.version(package_name)
        except metadata.PackageNotFoundError:
            checks.append(
                _check(
                    f"python.package.{package_name}",
                    "fail",
                    required=True,
                    detail="not installed",
                )
            )
            continue
        status = "pass" if installed == expected else "fail"
        checks.append(
            _check(
                f"python.package.{package_name}",
                status,
                required=True,
                detail=f"installed={installed}; expected={expected}",
            )
        )
    return checks


def _command_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for command in REQUIRED_COMMANDS:
        checks.append(
            _check(
                f"command.{command}",
                "pass" if shutil.which(command) else "fail",
                required=True,
                detail="available" if shutil.which(command) else "not found on PATH",
            )
        )

    yt_dlp_available = bool(shutil.which("yt-dlp")) or importlib.util.find_spec("yt_dlp") is not None
    checks.append(
        _check(
            "command.yt-dlp",
            "pass" if yt_dlp_available else "warn",
            required=False,
            detail="available" if yt_dlp_available else "not installed; URL import is disabled",
        )
    )
    return checks


def _model_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for variable in MODEL_ENVIRONMENT:
        configured = os.environ.get(variable, "").strip()
        if not configured:
            checks.append(
                _check(
                    f"model.{variable}",
                    "warn",
                    required=False,
                    detail="not configured; the corresponding local engine is disabled",
                )
            )
            continue
        exists = Path(configured).expanduser().is_dir()
        checks.append(
            _check(
                f"model.{variable}",
                "pass" if exists else "warn",
                required=False,
                detail="configured and available" if exists else "configured path is unavailable",
            )
        )
    return checks


def _provider_checks(required_provider: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for provider, variables in PROVIDER_ENVIRONMENT.items():
        configured = all(bool(os.environ.get(variable, "").strip()) for variable in variables)
        required = required_provider == provider
        checks.append(
            _check(
                f"provider.{provider}",
                "pass" if configured else ("fail" if required else "warn"),
                required=required,
                detail="credential present" if configured else "credential not configured",
            )
        )
    if required_provider == "any":
        configured = any(
            all(bool(os.environ.get(variable, "").strip()) for variable in variables)
            for variables in PROVIDER_ENVIRONMENT.values()
        )
        checks.append(
            _check(
                "provider.any",
                "pass" if configured else "fail",
                required=True,
                detail="at least one provider credential present" if configured else "no provider credential configured",
            )
        )
    return checks


def _library_check() -> dict[str, Any]:
    configured = os.environ.get("DUB_LIBRARY_ROOT", "").strip()
    if not configured:
        return _check(
            "library.root",
            "warn",
            required=False,
            detail="not configured; the default local library will be used",
        )
    root = Path(configured).expanduser()
    available = root.is_dir() and os.access(root, os.R_OK | os.W_OK)
    return _check(
        "library.root",
        "pass" if available else "fail",
        required=True,
        detail="available and writable" if available else "configured directory is unavailable or read-only",
    )


def _project_checks(workbench_root: Path, require_project: bool) -> list[dict[str, Any]]:
    config_value = os.environ.get("DUB_PROJECT_CONFIG", "").strip()
    if not config_value:
        return [
            _check(
                "project.config",
                "fail" if require_project else "warn",
                required=require_project,
                detail="DUB_PROJECT_CONFIG is not configured",
            )
        ]

    config_path = Path(config_value).expanduser()
    if not config_path.is_file():
        return [
            _check(
                "project.config",
                "fail",
                required=True,
                detail="configured file is unavailable",
            )
        ]
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [
            _check(
                "project.config",
                "fail",
                required=True,
                detail="configured file is not valid UTF-8 JSON",
            )
        ]
    if not isinstance(payload, dict):
        return [
            _check(
                "project.config",
                "fail",
                required=True,
                detail="configured JSON must be an object",
            )
        ]

    checks = [
        _check(
            "project.config",
            "pass",
            required=require_project,
            detail="configured file is readable",
        )
    ]
    workspace_root = workbench_root.parent
    for field in PROJECT_FILE_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            checks.append(
                _check(
                    f"project.file.{field}",
                    "fail",
                    required=True,
                    detail="binding is missing",
                )
            )
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        checks.append(
            _check(
                f"project.file.{field}",
                "pass" if candidate.is_file() else "fail",
                required=True,
                detail="available" if candidate.is_file() else "bound file is unavailable",
            )
        )
    return checks


def run_diagnostics(
    workbench_root: Path,
    *,
    require_project: bool = False,
    required_provider: str = "none",
) -> dict[str, Any]:
    """Return a report that never contains credential values or filesystem paths."""

    if required_provider not in {"none", "any", *PROVIDER_ENVIRONMENT}:
        raise PortabilityError("invalid_provider", "Unknown required provider.")
    root = workbench_root.expanduser().resolve()
    checks: list[dict[str, Any]] = []

    python_supported = sys.version_info[:2] == (3, 12)
    checks.append(
        _check(
            "python.version",
            "pass" if python_supported else "fail",
            required=True,
            detail=f"installed={platform.python_version()}; required=3.12.x",
        )
    )
    checks.extend(_package_checks())
    checks.extend(_command_checks())
    checks.append(
        _check(
            "workbench.root",
            "pass" if root.is_dir() else "fail",
            required=True,
            detail="available" if root.is_dir() else "missing",
        )
    )
    checks.append(_library_check())
    checks.extend(_project_checks(root, require_project))
    checks.extend(_provider_checks(required_provider))
    checks.extend(_model_checks())

    required_failures = sum(
        1 for row in checks if row["required"] and row["status"] == "fail"
    )
    warnings = sum(1 for row in checks if row["status"] == "warn")
    status = "fail" if required_failures else ("warn" if warnings else "pass")
    return {
        "schema_version": "dub-workbench-environment-diagnostic/v1",
        "checked_at": _utc_now(),
        "status": status,
        "required_failures": required_failures,
        "warnings": warnings,
        "credential_values_included": False,
        "filesystem_paths_included": False,
        "checks": checks,
    }


def write_rebind_receipt(project_root: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Record that a freshly imported project passed its device checks."""

    root = project_root.expanduser().resolve()
    if not root.is_dir():
        raise PortabilityError("project_missing", "Project root is not a readable directory.")
    if report.get("status") == "fail":
        raise PortabilityError("rebind_failed", "Environment checks must pass before rebind.")
    receipt = {
        "schema_version": "dub-workbench-rebind-receipt/v1",
        "rebound_at": _utc_now(),
        "status": "pass",
        "diagnostic_schema_version": report.get("schema_version"),
        "required_failures": report.get("required_failures"),
        "warnings": report.get("warnings"),
        "credential_values_included": False,
        "filesystem_paths_included": False,
    }
    receipt_dir = root / ".dub-workbench"
    is_junction = getattr(receipt_dir, "is_junction", None)
    if receipt_dir.is_symlink() or bool(is_junction and is_junction()):
        raise PortabilityError("unsafe_receipt_directory", "Rebind receipt directory cannot be a link.")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    output = receipt_dir / "rebind-receipt.json"
    temporary = receipt_dir / f".rebind-receipt.tmp-{uuid.uuid4().hex}"
    try:
        temporary.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PortabilityError("rebind_receipt_failed", "Could not write rebind receipt.") from exc
    return receipt
