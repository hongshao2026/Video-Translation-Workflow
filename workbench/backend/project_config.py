"""Optional per-process project binding for local workbenches."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
CONFIG_PATH = os.environ.get("DUB_PROJECT_CONFIG")
CONFIG = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8-sig")) if CONFIG_PATH else {}


def project_path(key: str, default: Path) -> Path:
    value = CONFIG.get(key)
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else WORKSPACE / path


def verify_translation_gate() -> bool:
    """Configured projects cannot cast against an absent or stale approval."""
    if not CONFIG:
        return True  # Retain the legacy project's separate approval contract.
    try:
        gate = json.loads(project_path("translation_gate", Path()).read_text(encoding="utf-8-sig"))
        translation = project_path("translation", Path())
        digest = hashlib.sha256(translation.read_bytes()).hexdigest()
        approval = gate["user_approval"]
        reading = gate["chapter_reading"]
        run_dir = project_path("translation_gate", Path()).resolve().parent.parent
        def bound_path(value):
            value = value["path"] if isinstance(value, dict) else value
            path = Path(value)
            return path if path.is_absolute() else run_dir / path
        validation = reading["validation"]
        validation_sha = validation["sha256"] if isinstance(validation, dict) else reading["validation_sha256"]
        artifact = approval["artifact"]
        artifact_sha = artifact["sha256"] if isinstance(artifact, dict) else approval["artifact_sha256"]
        return (
            gate["status"] == "pass" and gate["unresolved_total"] == 0
            and gate["roles_are_distinct"] is True
            and bound_path(gate["final_translation"]).resolve() == translation.resolve()
            and gate["final_translation_sha256"] == digest
            and approval["approved"] is True
            and approval["final_translation_sha256"] == digest
            and reading["status"] == "pass"
            and hashlib.sha256(bound_path(reading["path"]).read_bytes()).hexdigest()
            == reading["sha256"] == approval["reading_sha256"]
            and hashlib.sha256(bound_path(validation).read_bytes()).hexdigest() == validation_sha
            and hashlib.sha256(bound_path(artifact).read_bytes()).hexdigest() == artifact_sha
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
