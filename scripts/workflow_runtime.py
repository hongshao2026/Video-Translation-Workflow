"""Local machine-step runner. It never calls a model to wait or check progress."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TERMINAL = {"completed", "failed", "uncertain", "needs_agent", "needs_user"}
PAID_EFFECTS = {"paid_tts", "paid_audition"}
AUDITION_EFFECTS = {"paid_audition", "audition_render"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    try:
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                # Windows readers/AV may briefly deny atomic replacement.
                time.sleep(min(0.02 * (attempt + 1), 0.15))
    finally:
        Path(temporary).unlink(missing_ok=True)


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Artifact path must stay inside the run directory")
    return path


@contextmanager
def exclusive(path: Path):
    """An OS lock releases on crashes; never delete a possibly-live worker lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if path.stat().st_size == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("A worker already owns this job") from None
    try:
        yield
    finally:
        stream.close()


def verify(root: Path, specs: list[dict]) -> dict[str, str]:
    result = {}
    for spec in specs:
        path = inside(root, spec["path"])
        if not path.is_file():
            raise ValueError(f"Missing required artifact: {spec['path']}")
        actual = sha(path)
        if spec.get("sha256") and actual != spec["sha256"]:
            raise ValueError(f"Artifact hash mismatch: {spec['path']}")
        if "status" in spec and read(path).get("status") != spec["status"]:
            raise ValueError(f"Artifact gate did not pass: {spec['path']}")
        result[spec["path"]] = actual
    return result


def validate_audition_scope(step: dict, root: Path) -> None:
    """Keep a short audition authorization distinct from full production."""
    authorizations = [x for x in step.get("requires", []) if x.get("role") == "audition_authorization"]
    if len(authorizations) != 1:
        raise ValueError("Audition needs exactly one scoped authorization")
    spec = authorizations[0]
    if not re.fullmatch(r"[0-9a-f]{64}", spec.get("sha256", "")):
        raise ValueError("Audition authorization must be frozen by SHA-256")
    verify(root, [spec])
    authorization = read(inside(root, spec["path"]))
    if (authorization.get("status") != "pass"
            or authorization.get("scope") != "one_minute_audition_only"
            or authorization.get("full_tts_authorized") is not False):
        raise ValueError("Invalid audition authorization scope")
    for field in ("user_authorization", "voice_selection", "voice_selection_validation",
                  "translation_gate", "audition_items", "audition_validation"):
        bound = authorization.get(field, {})
        if not bound.get("path") or not re.fullmatch(r"[0-9a-f]{64}", bound.get("sha256", "")):
            raise ValueError("Audition authorization must bind all frozen inputs")
        verify(root, [bound])
    for role, field in (("translation_gate", "translation_gate"),
                        ("voice_selection_lock", "voice_selection_validation")):
        for required in (x for x in step.get("requires", []) if x.get("role") == role):
            bound = authorization[field]
            if (inside(root, required["path"]) != inside(root, bound["path"])
                    or required.get("sha256") != bound["sha256"]):
                raise ValueError("Audition gate does not match its authorized input")


def validate_plan(plan: dict, root: Path) -> None:
    if plan.get("schema_version") != 1 or not NAME.fullmatch(plan.get("job_id", "")):
        raise ValueError("Plan needs schema_version=1 and a valid job_id")
    lock = plan.get("workflow_lock", {})
    if lock.get("status") != "pass" or not re.fullmatch(r"[0-9a-f]{64}", lock.get("sha256", "")):
        raise ValueError("Plan must bind a passing workflow lock SHA-256")
    verify(root, [lock])
    locked = read(inside(root, lock["path"]))
    # Current locks expose their source paths so unchanged lock bytes cannot hide
    # changed rules or frozen inputs. Legacy project executors keep their own gates.
    if locked.get("schema_version") == 2 and locked.get("rules_root"):
        rules = Path(locked["rules_root"])
        for document in locked.get("required_documents", []):
            if sha(inside(rules, document["path"])) != document["sha256"]:
                raise ValueError("A required workflow document changed")
        try:
            from .create_workflow_lock import project_constraints
        except ImportError:
            from create_workflow_lock import project_constraints
        if hashlib.sha256(project_constraints(root / "PROJECT.md")).hexdigest() != locked["project"]["constraints_sha256"]:
            raise ValueError("Project frozen constraints changed")
        for item in locked.get("frozen_inputs", []):
            if sha(Path(item["source_path"])) != item["sha256"]:
                raise ValueError("A frozen input changed")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Plan needs nonempty steps")
    ids = set()
    for step in steps:
        identity = step.get("id", "")
        if not NAME.fullmatch(identity) or identity in ids:
            raise ValueError("Step IDs must be unique safe names")
        ids.add(identity)
        if step.get("kind") not in {"machine", "agent", "human"}:
            raise ValueError("Step kind must be machine, agent or human")
        for spec in step.get("requires", []) + step.get("outputs", []):
            inside(root, spec["path"])
        if step["kind"] != "machine":
            inside(root, step["receipt"])
            continue
        command = step.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("Machine command must be an argv array")
        if any(re.search(r"(?:^--(?:api-key|token|authorization)$|sk-[A-Za-z0-9]{12,}|googlevideo\.com)", x, re.I) for x in command):
            raise ValueError("Use credential files/environment; do not put secrets or media URLs in plans")
        effects = step.get("effects", "local")
        if effects not in {"local", "paid_tts", "render", "paid_audition", "audition_render"}:
            raise ValueError("Unsupported effects class")
        attempts = step.get("max_attempts", 1)
        if not isinstance(attempts, int) or not 1 <= attempts <= 6:
            raise ValueError("max_attempts must be 1..6")
        if attempts > 1 and (not step.get("idempotent") or effects in PAID_EFFECTS):
            raise ValueError("Retries require an idempotent local step; paid TTS cannot auto-retry")
        if not 0 < step.get("timeout_seconds", 7200) <= 86400:
            raise ValueError("Each machine step needs a bounded timeout")
        if not step.get("outputs"):
            raise ValueError("Each machine step needs validated outputs")
        named = {x.get("role") for x in step.get("requires", []) if x.get("status") == "pass"}
        mandatory = {
            "paid_tts": {"translation_gate", "full_generation_authorization", "dry_run"},
            "render": {"production_gate"},
            "paid_audition": {"translation_gate", "voice_selection_lock", "audition_authorization", "dry_run"},
            "audition_render": {"translation_gate", "audition_authorization", "audition_production_gate"},
        }.get(effects, set())
        if not mandatory.issubset(named):
            raise ValueError(f"Missing production preconditions for {effects}")
        if effects != "local" and not step.get("executor_enforces_project_gates"):
            raise ValueError("Production executor must enforce the existing full project gate semantics")
        if effects in AUDITION_EFFECTS:
            validate_audition_scope(step, root)


def marker(job: Path, state: dict) -> None:
    if state.get("notify_thread"):
        atomic(Path(state["workspace"]) / ".codex/workflow/active" / f"{state['notify_thread']}.json", {
            "job": str(job), "state_path": str(job / "state.json"),
            "status": state["status"], "job_id": state["job_id"], "updated_at": now(),
        })


def save(job: Path, state: dict) -> None:
    state["updated_at"] = now()
    atomic(job / "state.json", state)
    marker(job, state)


def redact(line: str) -> str:
    line = re.sub(r"https?://[^\s\"<>]*googlevideo\.com[^\s\"<>]*", "[REDACTED_MEDIA_URL]", line, flags=re.I)
    line = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED_KEY]", line)
    line = re.sub(r"(?i)((?:authorization|api[_ -]?key|cookie)\s*[:=])[^\r\n]+", r"\1[REDACTED]", line)
    return line


def command_run(step: dict, root: Path, log: Path) -> tuple[int, bool]:
    command = [sys.executable if x == "{python}" else x for x in step["command"]]
    log.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs)
    def collect():
        with log.open("w", encoding="utf-8") as stream:
            for line in process.stdout:
                stream.write(redact(line))
    collector = threading.Thread(target=collect, daemon=True)
    collector.start()
    timed_out = False
    try:
        code = process.wait(timeout=step.get("timeout_seconds", 7200))
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        code = process.wait()
    collector.join(timeout=5)
    if collector.is_alive():
        raise RuntimeError("Child left inherited log handles open")
    process.stdout.close()
    return code, timed_out


def notify(job: Path, event_path: Path) -> None:
    """At-most-once enqueue. An ambiguous enqueue is never blindly retried."""
    event = read(event_path)
    if event["delivery"]["status"] != "pending":
        return
    state = read(job / "state.json")
    if not state.get("notify_thread"):
        event["delivery"] = {"status": "manual", "automatic_continuation": False}
        atomic(event_path, event)
        return
    executable = state.get("codex_executable") or shutil.which("codex")
    if not executable:
        event["delivery"] = {"status": "failed", "reason": "codex_executable_unavailable"}
        atomic(event_path, event)
        return
    event["delivery"] = {"status": "sending", "started_at": now()}
    atomic(event_path, event)
    message = (f"本地工作流事件：{event['status']}；任务 {event['job_id']}，步骤 {event.get('step_id') or 'end'}。"
               f"事件文件：{event_path}。event_id={event['event_id']}，plan_sha256={event['plan_sha256']}。"
               "读取并核验该事件后处理所需判断或恢复；用 ack-event 记录消费。不要查询其他运行中任务的进度。"
               "若为 smoke 自检，只确认通知送达，不启动视频生产。")
    try:
        result = subprocess.run([executable, "queue", "--thread", state["notify_thread"], "--message", message],
                                cwd=state["workspace"], capture_output=True, timeout=30,
                                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        event["delivery"] = {"status": "accepted" if result.returncode == 0 else "failed", "returncode": result.returncode,
                             "completed_at": now(), "consumption_confirmed": False}
    except (OSError, subprocess.TimeoutExpired):
        event["delivery"] = {"status": "uncertain", "completed_at": now(), "automatic_retry": False}
    atomic(event_path, event)


def emit(job: Path, state: dict, status: str, step_id: str | None, reason: str) -> Path:
    previous = state.get("event_path")
    if previous:
        old = read(Path(previous))
        if old["status"] == status and old.get("step_id") == step_id and old["reason"] == reason:
            state["status"] = status
            save(job, state)
            return Path(previous)
    event_id = str(uuid.uuid4())
    event_path = job / "events" / f"{event_id}.json"
    event = {"schema_version": 1, "event_id": event_id, "job_id": state["job_id"], "status": status,
             "step_id": step_id, "reason": reason, "plan_sha256": state["plan_sha256"], "created_at": now(),
             "state_path": str(job / "state.json"), "delivery": {"status": "pending"}}
    atomic(event_path, event)
    state.update(status=status, event_path=str(event_path))
    save(job, state)
    notify(job, event_path)
    return event_path


def worker(job: Path, dispatch_id: str | None = None) -> None:
    with exclusive(job / "worker.lock"):
        state = read(job / "state.json")
        if dispatch_id is not None:
            if state.get("dispatch_id") != dispatch_id or state.get("claimed_dispatch_id") == dispatch_id:
                return
            state["claimed_dispatch_id"] = dispatch_id
            save(job, state)
        root = Path(state["run_dir"])
        plan = read(job / "plan.json")
        current = None
        effect = "local"
        try:
            if sha(job / "plan.json") != state["plan_sha256"]:
                raise ValueError("Frozen plan changed")
            validate_plan(plan, root)
            state.update(status="running", worker_pid=os.getpid())
            save(job, state)
            for step in plan["steps"]:
                current = step["id"]
                effect = step.get("effects", "local")
                state["current_step"] = current
                entry = state["steps"].get(current, {})
                if effect in AUDITION_EFFECTS:
                    validate_audition_scope(step, root)
                inputs = verify(root, [plan["workflow_lock"]] + step.get("requires", []))
                if entry.get("status") == "completed":
                    output_specs = step.get("outputs", []) + ([{"path": entry["receipt"]}] if entry.get("receipt") else [])
                    if inputs != entry["inputs"] or verify(root, output_specs) != entry["outputs"]:
                        raise ValueError("Completed checkpoint no longer matches inputs/outputs")
                    continue
                if entry.get("status") in {"running", "uncertain"} and effect in PAID_EFFECTS:
                    emit(job, state, "uncertain", current, "interrupted_paid_request_requires_reconciliation")
                    return
                if step["kind"] in {"agent", "human"}:
                    receipt = inside(root, step["receipt"])
                    expected_status = "needs_agent" if step["kind"] == "agent" else "needs_user"
                    prior = read(Path(state["event_path"])) if state.get("event_path") else {}
                    if receipt.is_file():
                        result = read(receipt)
                        if not (result.get("status") == "pass" and result.get("event_id") == prior.get("event_id")
                                and prior.get("step_id") == current and result.get("plan_sha256") == state["plan_sha256"]):
                            raise ValueError("Barrier receipt is not bound to this event and plan")
                        outputs = verify(root, step.get("outputs", []))
                        outputs[step["receipt"]] = sha(receipt)
                        state["steps"][current] = {"status": "completed", "inputs": inputs, "outputs": outputs,
                                                  "receipt": step["receipt"], "completed_at": now()}
                        save(job, state)
                        continue
                    emit(job, state, expected_status, current, "semantic_or_user_decision_required")
                    return
                attempts = entry.get("attempts", [])
                for retry in range(step.get("max_attempts", 1)):
                    log = job / "logs" / f"{current}_{len(attempts) + 1}.log"
                    record = {"started_at": now(), "log": str(log)}
                    attempts.append(record)
                    entry = {"status": "running", "inputs": inputs, "attempts": attempts}
                    state["steps"][current] = entry
                    save(job, state)
                    code, timeout = command_run(step, root, log)
                    record.update(returncode=code, timed_out=timeout, log_sha256=sha(log), completed_at=now())
                    if code == 0 and not timeout:
                        outputs = verify(root, step["outputs"])
                        entry.update(status="completed", outputs=outputs, completed_at=now())
                        save(job, state)
                        break
                    if effect in PAID_EFFECTS:
                        entry["status"] = "uncertain"
                        emit(job, state, "uncertain", current, "paid_step_failed_requires_reconciliation")
                        return
                    if retry + 1 == step.get("max_attempts", 1):
                        entry["status"] = "failed"
                        emit(job, state, "failed", current, "bounded_local_attempts_exhausted")
                        return
                    save(job, state)
                    time.sleep(min(2 ** retry, 30))
            emit(job, state, "completed", None, "all_declared_steps_and_outputs_verified")
        except Exception as exc:
            # Diagnostics exclude raw exception text, commands and logs: paths/hashes stay local.
            atomic(job / "diagnostic.json", {"exception_type": type(exc).__name__, "step_id": current,
                                            "message": redact(str(exc))[:2000],
                                            "plan_sha256": state["plan_sha256"], "created_at": now()})
            status = "uncertain" if effect in PAID_EFFECTS and state["steps"].get(current, {}).get("status") == "running" else "failed"
            emit(job, state, status, current, f"{type(exc).__name__}_see_local_diagnostic_and_step_evidence")


def launch(job: Path) -> int:
    dispatch_id = read(job / "state.json")["dispatch_id"]
    with (job / "worker.log").open("ab") as stream:
        kwargs = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker", "--job", str(job), "--dispatch-id", dispatch_id],
                                   stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, close_fds=True, **kwargs)
    threading.Thread(target=process.wait, daemon=True).start()
    return process.pid


def start(root: Path, plan_path: Path, workspace: Path, notify_thread: str | None, manual: bool) -> dict:
    root, workspace = root.resolve(), workspace.resolve()
    linked_project = any(link.is_dir() and link.resolve() == root
                         for link in workspace.glob("*_run"))
    if not root.is_relative_to(workspace) and not linked_project:
        raise ValueError("Run directory must be inside the selected workspace")
    if not notify_thread and not manual:
        raise ValueError("Choose --notify-thread or explicitly --manual-events")
    if notify_thread:
        uuid.UUID(notify_thread)
        if not shutil.which("codex"):
            raise ValueError("codex queue is unavailable; automatic notification is not configured")
        pointer = workspace / ".codex/workflow/active" / f"{notify_thread}.json"
        if pointer.is_file():
            other = read(pointer)
            other_state = Path(other["state_path"])
            if other_state.is_file() and read(other_state).get("status") in {"running", "starting"}:
                raise ValueError("This task already owns an active machine plan; do not launch a duplicate")
    plan = read(plan_path)
    validate_plan(plan, root)
    job = root / "runtime/jobs" / plan["job_id"]
    job.mkdir(parents=True, exist_ok=True)
    with exclusive(job / "worker.lock"):
        if (job / "state.json").exists():
            raise ValueError("Job already exists; use a new plan version or an evidence-bound resume")
        atomic(job / "plan.json", plan)
        state = {"schema_version": 1, "job_id": plan["job_id"], "status": "starting", "run_dir": str(root),
                 "workspace": str(workspace), "notify_thread": notify_thread, "codex_executable": shutil.which("codex"),
                 "plan_sha256": sha(job / "plan.json"), "steps": {}, "created_at": now(), "dispatch_id": str(uuid.uuid4())}
        save(job, state)
    pid = launch(job)
    return {"job_id": plan["job_id"], "worker_pid": pid, "state_path": str(job / "state.json"),
            "events_directory": str(job / "events"), "notification": "codex_queue" if notify_thread else "manual_events",
            "media_session_id": None, "instruction": "Wait for an event; do not poll this job."}


def resume(job: Path, evidence: Path | None) -> dict:
    with exclusive(job / "worker.lock"):
        state = read(job / "state.json")
        if state["status"] == "completed":
            return {"status": "completed", "action": "none"}
        if state["status"] == "uncertain":
            raise ValueError("Uncertain paid work must be reconciled in a new verified plan; never auto-retry")
        if state["status"] in {"needs_agent", "needs_user"}:
            plan = read(job / "plan.json")
            step = next(x for x in plan["steps"] if x["id"] == state["current_step"])
            if not inside(Path(state["run_dir"]), step["receipt"]).is_file():
                raise ValueError("The required decision receipt is not available; wait for the decision")
        if state["status"] in {"failed", "running", "starting"}:
            if not evidence or not evidence.is_file():
                raise ValueError("Recovery requires new repair evidence")
            value = sha(evidence)
            if value in state.get("recovery_evidence", []):
                raise ValueError("Unchanged repair evidence cannot authorize another recovery")
            state.setdefault("recovery_evidence", []).append(value)
        state.update(dispatch_id=str(uuid.uuid4()), status="starting")
        save(job, state)
    return {"job_id": state["job_id"], "worker_pid": launch(job), "instruction": "Wait for an event; do not poll."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("start")
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--notify-thread")
    choice.add_argument("--manual-events", action="store_true")
    for name in ("_worker", "resume", "status", "ack-event"):
        p = sub.add_parser(name)
        p.add_argument("--job", required=True, type=Path)
        if name == "resume":
            p.add_argument("--repair-evidence", type=Path)
        if name == "_worker":
            p.add_argument("--dispatch-id", required=True)
        if name == "ack-event":
            p.add_argument("--event-id", required=True)
    args = parser.parse_args()
    try:
        if args.action == "start":
            result = start(args.run_dir.resolve(), args.plan.resolve(), args.workspace.resolve(), args.notify_thread, args.manual_events)
        elif args.action == "_worker":
            worker(args.job.resolve(), args.dispatch_id)
            return 0
        elif args.action == "resume":
            result = resume(args.job.resolve(), args.repair_evidence)
        elif args.action == "ack-event":
            uuid.UUID(args.event_id)
            path = args.job.resolve() / "events" / f"{args.event_id}.json"
            event = read(path)
            event["consumed_at"] = now()
            event["delivery"]["consumption_confirmed"] = True
            atomic(path, event)
            result = {"status": "acknowledged", "event_id": args.event_id}
        else:
            state = read(args.job.resolve() / "state.json")
            result = {k: state.get(k) for k in ("job_id", "status", "current_step", "event_path", "updated_at")}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
