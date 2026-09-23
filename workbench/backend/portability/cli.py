"""Command-line interface for project transfer and environment rebind checks."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.workbench.database import WorkbenchDatabase
from backend.workbench.library import ProjectLibrary
from backend.workbench.settings import WorkbenchSettings

from .environment import run_diagnostics, write_rebind_receipt
from .manifest import PortabilityError, export_project, import_project, verify_archive


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dub-portability",
        description="Export, verify and import credential-free dubbing project snapshots.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="Create a verified project ZIP.")
    export.add_argument("project_root", type=Path)
    export.add_argument("archive", type=Path)
    export.add_argument("--include-media", action="store_true")
    export.add_argument("--overwrite", action="store_true")

    verify = commands.add_parser("verify", help="Verify every archived file by SHA-256.")
    verify.add_argument("archive", type=Path)

    restore = commands.add_parser("import", help="Verify and atomically restore a project ZIP.")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    restore.add_argument(
        "--attach",
        action="store_true",
        help="Register the imported project in the configured local library index.",
    )

    attach = commands.add_parser(
        "attach", help="Register a verified project directory already inside the local library."
    )
    attach.add_argument("project_root", type=Path)

    diagnose = commands.add_parser("diagnose", help="Check this device without revealing paths or keys.")
    diagnose.add_argument("--workbench-root", type=Path, default=Path.cwd())
    diagnose.add_argument("--require-project", action="store_true")
    diagnose.add_argument(
        "--provider", choices=("none", "any", "minimax", "openai"), default="none"
    )
    diagnose.add_argument("--strict", action="store_true")

    rebind = commands.add_parser("rebind", help="Run strict checks and write a local rebind receipt.")
    rebind.add_argument("project_root", type=Path)
    rebind.add_argument("--workbench-root", type=Path, default=Path.cwd())
    rebind.add_argument(
        "--provider", choices=("none", "any", "minimax", "openai"), default="none"
    )
    return parser


def _attach_project(project_root: Path) -> dict[str, Any]:
    settings = WorkbenchSettings.from_environment()
    # Resolve the environment once so CLI attachment has the exact same path
    # and database semantics as the application process.
    settings = replace(
        settings,
        state_dir=settings.state_dir.resolve(),
        library_dir=settings.library_dir.resolve(),
        database_path=settings.database_path.resolve(),
    )
    database = WorkbenchDatabase(settings.database_path)
    database.initialize()
    return ProjectLibrary(database, settings).restore_existing(project_root)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            result = export_project(
                args.project_root,
                args.archive,
                include_media=args.include_media,
                overwrite=args.overwrite,
            )
        elif args.command == "verify":
            result = verify_archive(args.archive)
        elif args.command == "import":
            result = import_project(args.archive, args.destination)
            if args.attach:
                result = {**result, "attachment": _attach_project(args.destination)}
        elif args.command == "attach":
            result = _attach_project(args.project_root)
        elif args.command == "diagnose":
            result = run_diagnostics(
                args.workbench_root,
                require_project=args.require_project,
                required_provider=args.provider,
            )
            _print_json(result)
            return 1 if args.strict and result["status"] == "fail" else 0
        elif args.command == "rebind":
            diagnostic = run_diagnostics(
                args.workbench_root,
                require_project=True,
                required_provider=args.provider,
            )
            if diagnostic["status"] == "fail":
                _print_json(diagnostic)
                return 1
            result = write_rebind_receipt(args.project_root, diagnostic)
        else:  # pragma: no cover - argparse prevents this branch.
            raise PortabilityError("unknown_command", "Unknown command.")
    except PortabilityError as exc:
        _print_json({"status": "error", "code": exc.code, "message": str(exc)})
        return 2
    except (OSError, ValueError) as exc:
        _print_json(
            {
                "status": "error",
                "code": "attach_failed",
                "message": str(exc) if isinstance(exc, ValueError) else "Project attachment failed.",
            }
        )
        return 2
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
