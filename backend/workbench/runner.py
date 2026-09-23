"""Persistent local task runner with cooperative checkpoints and safe recovery."""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .database import WorkbenchDatabase, utc_now

Handler = Callable[[Mapping[str, Any], "ProgressReporter", "TaskToken"], Mapping[str, Any] | None]


def _redact(message: str) -> str:
    message = re.sub(r"\b(?:sk-|eyJ)[A-Za-z0-9_.-]{10,}", "[REDACTED]", message)
    message = re.sub(
        r"(?i)((?:authorization|api[_ -]?key|cookie|token)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    return message[:2000]


class PauseRequested(RuntimeError):
    pass


class CancelRequested(RuntimeError):
    pass


class WaitingProvider(RuntimeError):
    def __init__(self, message: str, *, retry_at: str | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class UncertainPaidRequest(RuntimeError):
    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass(slots=True)
class TaskToken:
    pause_requested: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)

    def checkpoint(self) -> None:
        if self.cancel_requested.is_set():
            raise CancelRequested("用户请求取消")
        if self.pause_requested.is_set():
            raise PauseRequested("用户请求暂停")


class ProgressReporter:
    def __init__(self, database: WorkbenchDatabase, task_id: str) -> None:
        self.database = database
        self.task_id = task_id

    def exact(self, current: float, total: float, unit: str, detail: str) -> None:
        if total <= 0 or current < 0 or current > total:
            raise ValueError("Invalid exact progress")
        self.database.update_task(
            self.task_id,
            progress_mode="exact",
            progress_current=current,
            progress_total=total,
            progress_unit=unit,
            detail=detail,
        )

    def checkpointed(self, current: float, total: float, unit: str, detail: str) -> None:
        if total <= 0 or current < 0 or current > total:
            raise ValueError("Invalid checkpoint progress")
        self.database.update_task(
            self.task_id,
            progress_mode="checkpointed",
            progress_current=current,
            progress_total=total,
            progress_unit=unit,
            detail=detail,
        )

    def indeterminate(self, detail: str) -> None:
        self.database.update_task(
            self.task_id,
            progress_mode="indeterminate",
            progress_current=None,
            progress_total=None,
            progress_unit=None,
            detail=detail,
        )


class LocalTaskRunner:
    def __init__(
        self,
        database: WorkbenchDatabase,
        max_workers: int = 2,
        worker_id: str = "local-worker",
    ) -> None:
        self.database = database
        self.worker_id = f"{worker_id}:{uuid.uuid4().hex[:12]}"
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dub-worker")
        self._handlers: dict[str, Handler] = {}
        self._tokens: dict[str, TaskToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()

    def register(self, kind: str, handler: Handler) -> None:
        if not kind or kind in self._handlers:
            raise ValueError(f"Task handler already registered or invalid: {kind}")
        self._handlers[kind] = handler

    def recover_interrupted(self) -> int:
        recovered = 0
        for task in self.database.fetch_all(
            "SELECT * FROM tasks WHERE status IN ('running','pausing','cancel_requested')"
        ):
            paid = task["kind"].startswith(("provider.llm", "provider.speech", "speech.synthesize"))
            requests = self.database.list_provider_requests(task["id"]) if paid else []
            has_unresolved_request = any(
                row["status"] in {"sending", "uncertain"} for row in requests
            )
            blocked = paid and has_unresolved_request
            self.database.update_task(
                task["id"],
                status="blocked_uncertain" if blocked else "repair_required",
                detail=(
                    "程序中断时外部请求可能已经受理；请先核对供应商状态"
                    if blocked
                    else "程序中断；账本没有未决付费请求，已保留检查点，可安全恢复"
                ),
                error_code=(
                    "interrupted_paid_request"
                    if blocked
                    else "interrupted_recoverable_task"
                ),
                error={"automatic_retry": False},
            )
            recovered += 1
        return recovered

    def enqueue(self, task_id: str) -> Future[None]:
        task = self.database.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["kind"] not in self._handlers:
            raise ValueError(f"No handler registered for {task['kind']}")
        if task["status"] not in {
            "queued",
            "paused",
            "waiting_provider",
            "waiting_worker",
            "repair_required",
        }:
            raise ValueError(f"Task cannot be enqueued from {task['status']}")
        with self._lock:
            existing = self._futures.get(task_id)
            if existing and not existing.done():
                return existing
            token = TaskToken()
            self._tokens[task_id] = token
            future = self._executor.submit(self._run, task_id, token)
            self._futures[task_id] = future
            return future

    def run_inline(self, task_id: str) -> None:
        self._run(task_id, TaskToken())

    def request_pause(self, task_id: str) -> None:
        with self._lock:
            token = self._tokens.get(task_id)
        if token is None:
            raise ValueError("Task is not running in this worker")
        token.pause_requested.set()
        self.database.update_task(task_id, status="pausing", detail="等待当前安全检查点")

    def request_cancel(self, task_id: str) -> None:
        with self._lock:
            token = self._tokens.get(task_id)
        if token is None:
            raise ValueError("Task is not running in this worker")
        token.cancel_requested.set()
        self.database.update_task(task_id, status="cancel_requested", detail="等待当前安全取消点")

    def _run(self, task_id: str, token: TaskToken) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            return
        handler = self._handlers.get(task["kind"])
        if handler is None:
            self.database.update_task(
                task_id,
                status="repair_required",
                error_code="handler_missing",
                error={"kind": task["kind"]},
                detail="当前设备没有安装这个任务处理器",
            )
            return
        try:
            lease = self.database.acquire_lease(task["run_id"], self.worker_id, ttl_seconds=120)
        except RuntimeError:
            self.database.update_task(
                task_id,
                status="waiting_worker",
                detail="当前运行已被另一台设备的 Worker 接管",
                error_code="worker_lease_busy",
                error={"automatic_retry": False},
            )
            return
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(30):
                try:
                    self.database.heartbeat_lease(
                        task["run_id"], lease["lease_token"], ttl_seconds=120
                    )
                except RuntimeError:
                    # The active handler is allowed to reach its next safe
                    # checkpoint; paid requests are never force-killed here.
                    token.pause_requested.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"lease-{task_id[-8:]}",
            daemon=True,
        )
        heartbeat_thread.start()
        reporter = ProgressReporter(self.database, task_id)
        self.database.update_task(
            task_id,
            status="running",
            attempt=int(task.get("attempt") or 0) + 1,
            started_at=task.get("started_at") or utc_now(),
            error_code=None,
            error={},
        )
        try:
            result = handler(task.get("input", {}), reporter, token) or {}
            token.checkpoint()
            self.database.update_task(
                task_id,
                status="completed",
                detail=str(result.get("detail") or "任务完成"),
                result=dict(result),
                completed_at=utc_now(),
            )
        except PauseRequested:
            self.database.update_task(task_id, status="paused", detail="已在安全检查点暂停")
        except CancelRequested:
            self.database.update_task(
                task_id,
                status="cancelled",
                detail="已安全取消",
                completed_at=utc_now(),
            )
        except WaitingProvider as exc:
            self.database.update_task(
                task_id,
                status="waiting_provider",
                detail=_redact(str(exc)),
                error_code="waiting_provider",
                error={"retry_at": exc.retry_at, "automatic_retry": False},
            )
        except UncertainPaidRequest as exc:
            self.database.update_task(
                task_id,
                status="blocked_uncertain",
                detail=_redact(str(exc)),
                error_code="billing_uncertain",
                error={"request_id": exc.request_id, "automatic_retry": False},
            )
        # The runner is the outer task boundary: every handler failure must be
        # persisted as repair-required (or billing-uncertain) before release.
        except Exception as exc:  # noqa: BLE001
            uncertain = bool(getattr(exc, "uncertain_completion", False))
            self.database.update_task(
                task_id,
                status="blocked_uncertain" if uncertain else "repair_required",
                detail=(
                    "外部请求结果不确定，请先核对供应商状态"
                    if uncertain
                    else "任务失败；已保留此前通过验证的检查点"
                ),
                error_code=str(getattr(exc, "code", type(exc).__name__)),
                error={
                    "type": type(exc).__name__,
                    "message": _redact(str(exc)),
                    "automatic_retry": False,
                },
            )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            self.database.release_lease(task["run_id"], lease["lease_token"])
            with self._lock:
                self._tokens.pop(task_id, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
