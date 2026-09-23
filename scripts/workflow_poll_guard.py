"""Best-effort Codex hook; write_stdin itself is not intercepted by the host."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from .workflow_runtime import atomic, now, read
except ImportError:
    from workflow_runtime import atomic, now, read


QUERY = re.compile(r"进度|状态|完成了吗|怎么样了|停止|取消|\b(?:status|progress|cancel|stop)\b", re.I)
PROBE = re.compile(r"\b(?:Get-Process|Get-Job|Receive-Job|Wait-Process|nvidia-smi|tasklist)\b|Get-Content[^\r\n]*-Tail\b|\btail\s+-[fn]|workflow_runtime\.py[\s\"']+status\b|(?:runtime[/\\]jobs|_status[^\s]*\.json|state\.json)", re.I)
STATE_TOOLS = {"list_agents", "read_thread", "wait_threads", "read_thread_terminal"}


def workspace(cwd: str) -> Path | None:
    path = Path(cwd).resolve()
    for root in (path, *path.parents):
        if (root / ".codex/workflow/active").is_dir():
            return root
    return None


def state_is_local(root: Path, state_path: Path) -> bool:
    resolved = state_path.resolve()
    if resolved.is_relative_to(root.resolve()):
        return True
    # Media projects may be workspace directory junctions onto another drive.
    # Accept only a real <video>_run link and its fixed runtime job-state layout.
    for linked_run in root.glob("*_run"):
        if not linked_run.is_dir():
            continue
        try:
            parts = resolved.relative_to(linked_run.resolve()).parts
        except ValueError:
            continue
        if (len(parts) == 4 and parts[:2] == ("runtime", "jobs")
                and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", parts[2])
                and parts[3] == "state.json"):
            return True
    return False


def evaluate(payload: dict) -> dict:
    root = workspace(payload.get("cwd", "."))
    session = str(payload.get("session_id", ""))
    if not root or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session):
        return {}
    folder = root / ".codex/workflow"
    active_path = folder / "active" / f"{session}.json"
    if not active_path.is_file():
        return {}
    active = read(active_path)
    state_path = Path(active["state_path"])
    # Never follow an arbitrary external path from mutable hook state.
    if not state_is_local(root, state_path):
        return {}
    active = read(state_path)
    permission = folder / "queries" / f"{session}.json"
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt", ""))
        atomic(permission, {"remaining": 1 if QUERY.search(prompt) else 0,
                            "turn_id": payload.get("turn_id"), "updated_at": now()})
        return {}
    if event != "PreToolUse" or active.get("status") not in {"starting", "running"}:
        return {}
    tool = payload.get("tool_name", "")
    short = tool.split("__")[-1].split(".")[-1]
    args = payload.get("tool_input") or {}
    command = args.get("command", args.get("cmd", "")) if isinstance(args, dict) else str(args)
    blocked = short in STATE_TOOLS or (tool in {"Bash", "exec_command"} and bool(PROBE.search(command)))
    # Long waits are event waiting, not progress polling; zero-time snapshots are blocked.
    if short == "wait_threads" and isinstance(args, dict) and args.get("timeoutMs", 120000) > 0:
        blocked = False
    if not blocked:
        return {}
    permit = read(permission) if permission.is_file() else {}
    if permit.get("remaining", 0) and permit.get("turn_id") == payload.get("turn_id"):
        permit["remaining"] = 0
        atomic(permission, permit)
        return {}
    reason = "工作流禁止主动查询运行进度。该任务由本地执行器持有；等待完成/失败事件，或处理其他独立工作。不得换工具重试查询。"
    record = {"at": now(), "tool": tool, "reason": "unsolicited_running_job_probe",
              "input_sha256": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()}
    audit = folder / "denials" / f"{session}.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": reason}}


def main() -> int:
    try:
        result = evaluate(json.load(sys.stdin))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        # Visible failure, not an invented claim that the host enforced anything.
        print(json.dumps({"systemMessage": "Workflow poll guard could not read its local state; inspect configuration."}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
