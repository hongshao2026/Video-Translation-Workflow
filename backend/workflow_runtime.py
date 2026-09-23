"""Workspace entry point; canonical runner lives in the portable workflow repo."""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    script = Path(__file__).resolve().parents[2] / "Video-Translation-Workflow/scripts/workflow_runtime.py"
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")
