"""Write reviewable project hooks. Does not alter Codex hook trust or permissions."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

try:
    from .workflow_runtime import atomic, read
except ImportError:
    from workflow_runtime import atomic, read


def install(workspace: Path) -> Path:
    script = Path(__file__).with_name("workflow_poll_guard.py").resolve()
    path = workspace.resolve() / ".codex/hooks.json"
    config = read(path) if path.is_file() else {"hooks": {}}
    hooks = config.setdefault("hooks", {})
    handler = {"type": "command", "command": shlex.join([sys.executable, str(script)]),
               "commandWindows": subprocess.list2cmdline([sys.executable, str(script)]),
               "timeout": 5, "statusMessage": "Checking workflow event policy"}
    for event, matcher in (("PreToolUse", "Bash|.*list_agents$|.*read_thread$|.*wait_threads$|.*read_thread_terminal$"), ("UserPromptSubmit", None)):
        groups = hooks.setdefault(event, [])
        # Replace only our own handler; preserve all other hooks and matcher groups.
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", []) if h.get("statusMessage") != handler["statusMessage"]]
        groups[:] = [g for g in groups if g.get("hooks")]
        group = {"hooks": [handler.copy()]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
    atomic(path, config)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps({"path": str(install(args.workspace)), "status": "written_pending_host_trust",
                      "next": "Review and trust these exact hooks in Codex /hooks. No trust bypass was used."}, ensure_ascii=True))
