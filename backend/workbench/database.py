"""SQLite persistence for projects, runs, jobs, requests and audit events."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_STATUSES = {
    "draft",
    "queued",
    "running",
    "waiting_user",
    "waiting_provider",
    "paused",
    "blocked_uncertain",
    "repair_required",
    "machine_passed",
    "completed",
    "cancelled",
}

RUN_STATUSES = PROJECT_STATUSES | {"ready", "pausing", "cancel_requested", "invalidated", "superseded"}
JOB_STATUSES = RUN_STATUSES | {"retry_wait", "waiting_worker"}
PROVIDER_REQUEST_STATUSES = {"sending", "completed", "failed", "uncertain"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^prj_[A-Za-z0-9_.:-]{3,180}$")
_RUN_ID = re.compile(r"^run_[A-Za-z0-9_.:-]{3,180}$")
_PROFILE_ID = re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
_PRESET_ID = re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
_PROVIDER_REQUEST_TRANSITIONS = {
    "sending": {"sending", "completed", "failed", "uncertain"},
    "completed": {"completed"},
    "failed": {"failed"},
    "uncertain": {"uncertain"},
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('video_url','local_file')),
    source TEXT NOT NULL,
    source_display TEXT NOT NULL,
    library_path TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL DEFAULT '01',
    progress_mode TEXT NOT NULL DEFAULT 'gate',
    progress_current REAL,
    progress_total REAL,
    progress_unit TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_status_updated
ON projects(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS workflow_presets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 1,
    prompt_pack_json TEXT NOT NULL,
    prompt_pack_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS provider_profiles (
    id TEXT PRIMARY KEY,
    service_kind TEXT NOT NULL CHECK(service_kind IN ('llm','speech')),
    provider_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    base_url TEXT,
    model TEXT NOT NULL,
    credential_ref TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    capability_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    preset_id TEXT NOT NULL REFERENCES workflow_presets(id),
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL DEFAULT '01',
    provider_lock_json TEXT NOT NULL DEFAULT '{}',
    prompt_lock_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_project_created
ON workflow_runs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS stage_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    stage_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_mode TEXT NOT NULL DEFAULT 'gate',
    progress_current REAL,
    progress_total REAL,
    progress_unit TEXT,
    detail TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, stage_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    stage_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    progress_mode TEXT NOT NULL DEFAULT 'indeterminate',
    progress_current REAL,
    progress_total REAL,
    progress_unit TEXT,
    detail TEXT NOT NULL DEFAULT '',
    input_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
ON tasks(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS provider_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    profile_id TEXT REFERENCES provider_profiles(id) ON DELETE SET NULL,
    provider_id TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    resolved_model TEXT,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    billing_state TEXT NOT NULL DEFAULT 'not_started',
    input_sha256 TEXT NOT NULL,
    provider_request_id TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    cost_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, relative_path, sha256)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);

CREATE TABLE IF NOT EXISTS worker_leases (
    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
"""


class WorkbenchDatabase:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._schema_lock:
            if self._initialized:
                return
            with self.connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(SCHEMA)
                task_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
                if "input_json" not in task_columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN input_json TEXT NOT NULL DEFAULT '{}'")
                if "result_json" not in task_columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")
                if "parent_task_id" not in task_columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE")
                provider_request_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(provider_requests)"
                    ).fetchall()
                }
                if "result_json" not in provider_request_columns:
                    connection.execute(
                        "ALTER TABLE provider_requests "
                        "ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'"
                    )
                connection.execute("PRAGMA user_version=3")
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> None:
        self.initialize()
        with self.connection() as connection:
            connection.execute(sql, parameters)

    def fetch_one(self, sql: str, parameters: Sequence[object] = ()) -> dict[str, Any] | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return self._row(row) if row else None

    def fetch_all(self, sql: str, parameters: Sequence[object] = ()) -> list[dict[str, Any]]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json"):
                result[key[:-5]] = _loads(result.pop(key), {})
        for key in ("needs_attention", "locked", "enabled"):
            if key in result:
                result[key] = bool(result[key])
        return result

    def emit_event(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        self.initialize()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO events(event_type,project_id,run_id,task_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_type, project_id, run_id, task_id, _json(payload), utc_now()),
            )
            return int(cursor.lastrowid)

    def events_after(self, event_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (event_id, min(max(limit, 1), 500))
        )

    def create_project(self, values: Mapping[str, object]) -> dict[str, Any]:
        now = utc_now()
        project_id = str(values.get("id") or new_id("prj"))
        status = str(values.get("status") or "draft")
        if status not in PROJECT_STATUSES:
            raise ValueError(f"Unsupported project status: {status}")
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO projects(
                    id,title,source_kind,source,source_display,library_path,status,current_stage,
                    progress_mode,progress_current,progress_total,progress_unit,needs_attention,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id,
                    str(values["title"]),
                    str(values["source_kind"]),
                    str(values["source"]),
                    str(values.get("source_display") or values["source"]),
                    str(values["library_path"]),
                    status,
                    str(values.get("current_stage") or "01"),
                    str(values.get("progress_mode") or "gate"),
                    values.get("progress_current"),
                    values.get("progress_total"),
                    values.get("progress_unit"),
                    1 if values.get("needs_attention") else 0,
                    _json(values.get("metadata") or {}),
                    now,
                    now,
                ),
            )
        self.emit_event("project.created", {"status": status}, project_id=project_id)
        project = self.get_project(project_id)
        assert project is not None
        return project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM projects WHERE id=?", (project_id,))

    def get_project_by_library_path(self, library_path: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM projects WHERE library_path=?", (library_path,))

    def list_projects(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.fetch_all(
                "SELECT * FROM projects WHERE status=? ORDER BY updated_at DESC", (status,)
            )
        return self.fetch_all("SELECT * FROM projects ORDER BY updated_at DESC")

    def update_project(self, project_id: str, values: Mapping[str, object]) -> dict[str, Any]:
        allowed = {
            "title",
            "status",
            "current_stage",
            "progress_mode",
            "progress_current",
            "progress_total",
            "progress_unit",
            "needs_attention",
            "metadata_json",
        }
        assignments: list[str] = []
        parameters: list[object] = []
        for key, value in values.items():
            storage_key = "metadata_json" if key == "metadata" else key
            if storage_key not in allowed:
                continue
            if storage_key == "status" and str(value) not in PROJECT_STATUSES:
                raise ValueError(f"Unsupported project status: {value}")
            if storage_key == "metadata_json":
                value = _json(value or {})
            if storage_key == "needs_attention":
                value = 1 if value else 0
            assignments.append(f"{storage_key}=?")
            parameters.append(value)
        if not assignments:
            project = self.get_project(project_id)
            if project is None:
                raise KeyError(project_id)
            return project
        assignments.append("updated_at=?")
        parameters.extend([utc_now(), project_id])
        self.execute(f"UPDATE projects SET {','.join(assignments)} WHERE id=?", parameters)
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        self.emit_event("project.updated", {"fields": sorted(values)}, project_id=project_id)
        return project

    def rebind_project_source(
        self,
        project_id: str,
        *,
        source_kind: str,
        source: str,
        source_display: str,
        metadata: Mapping[str, object],
    ) -> dict[str, Any]:
        """Replace only the machine-local source binding for an existing project."""

        if source_kind not in {"video_url", "local_file"}:
            raise ValueError("Unsupported source kind")
        now = utc_now()
        with self.transaction() as connection:
            result = connection.execute(
                """UPDATE projects SET source_kind=?,source=?,source_display=?,metadata_json=?,updated_at=?
                WHERE id=?""",
                (
                    source_kind,
                    source,
                    source_display,
                    _json(metadata),
                    now,
                    project_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError(project_id)
        self.emit_event(
            "project.source_rebound",
            {"source_kind": source_kind},
            project_id=project_id,
        )
        project = self.get_project(project_id)
        assert project is not None
        return project

    def upsert_provider_profile(self, values: Mapping[str, object]) -> dict[str, Any]:
        now = utc_now()
        profile_id = str(values.get("id") or new_id("provider"))
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO provider_profiles(
                    id,service_kind,provider_id,display_name,base_url,model,credential_ref,
                    config_json,capability_json,enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    service_kind=excluded.service_kind,
                    provider_id=excluded.provider_id,
                    display_name=excluded.display_name,
                    base_url=excluded.base_url,
                    model=excluded.model,
                    credential_ref=excluded.credential_ref,
                    config_json=excluded.config_json,
                    capability_json=excluded.capability_json,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at""",
                (
                    profile_id,
                    str(values["service_kind"]),
                    str(values["provider_id"]),
                    str(values["display_name"]),
                    values.get("base_url"),
                    str(values["model"]),
                    values.get("credential_ref"),
                    _json(values.get("config") or {}),
                    _json(values.get("capability") or {}),
                    1 if values.get("enabled", True) else 0,
                    str(values.get("created_at") or now),
                    now,
                ),
            )
        profile = self.get_provider_profile(profile_id)
        assert profile is not None
        return profile

    def get_provider_profile(self, profile_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM provider_profiles WHERE id=?", (profile_id,))

    def list_provider_profiles(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM provider_profiles ORDER BY service_kind,display_name")

    def seed_workflow_preset(
        self,
        *,
        preset_id: str,
        name: str,
        version: int,
        description: str,
        locked: bool,
        prompt_pack: Mapping[str, object],
        prompt_pack_sha256: str,
    ) -> None:
        self.initialize()
        self.execute(
            """INSERT OR IGNORE INTO workflow_presets(
                id,name,version,description,locked,prompt_pack_json,prompt_pack_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                preset_id,
                name,
                version,
                description,
                1 if locked else 0,
                _json(prompt_pack),
                prompt_pack_sha256,
                utc_now(),
            ),
        )

    def get_workflow_preset(self, preset_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM workflow_presets WHERE id=?", (preset_id,))

    def list_workflow_presets(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM workflow_presets ORDER BY locked DESC,name,version DESC")

    def create_run(
        self,
        *,
        project_id: str,
        preset_id: str,
        stages: Sequence[Mapping[str, object]],
        provider_lock: Mapping[str, object],
        prompt_lock: Mapping[str, object],
    ) -> dict[str, Any]:
        run_id = new_id("run")
        now = utc_now()
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(project_id)
            if connection.execute("SELECT 1 FROM workflow_presets WHERE id=?", (preset_id,)).fetchone() is None:
                raise KeyError(preset_id)
            connection.execute(
                """INSERT INTO workflow_runs(
                    id,project_id,preset_id,status,current_stage,provider_lock_json,prompt_lock_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_id, project_id, preset_id, "ready", "01", _json(provider_lock), _json(prompt_lock), now, now),
            )
            for ordinal, stage in enumerate(stages, 1):
                connection.execute(
                    """INSERT INTO stage_runs(
                        id,run_id,stage_key,ordinal,title,status,progress_mode,detail,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("stage"),
                        run_id,
                        str(stage["key"]),
                        ordinal,
                        str(stage["title"]),
                        "ready" if ordinal == 1 else "draft",
                        str(stage.get("progress_mode") or "gate"),
                        str(stage.get("detail") or ""),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE projects SET status='queued',current_stage='01',updated_at=? WHERE id=?",
                (now, project_id),
            )
        self.emit_event("run.created", {"preset_id": preset_id}, project_id=project_id, run_id=run_id)
        run = self.get_run(run_id)
        assert run is not None
        return run

    def restore_project_run(
        self,
        *,
        project: Mapping[str, object],
        run_id: str,
        preset: Mapping[str, object],
        stages: Sequence[Mapping[str, object]],
        provider_lock: Mapping[str, object],
        prompt_lock: Mapping[str, object],
        provider_profiles: Sequence[Mapping[str, object]],
        run_status: str,
        current_stage: str,
    ) -> dict[str, Any]:
        """Atomically attach one verified cold snapshot to this device.

        This deliberately restores no tasks, events from the old device,
        provider-request ledger rows, worker leases or credential references.
        Callers must validate the on-disk project and workflow lock first.
        """

        project_id = str(project.get("id") or "")
        preset_id = str(preset.get("id") or "")
        if not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("Invalid restored project id")
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid restored run id")
        if not _PRESET_ID.fullmatch(preset_id):
            raise ValueError("Invalid restored preset id")
        if run_status not in RUN_STATUSES:
            raise ValueError("Invalid restored run status")
        if current_stage not in {f"{number:02d}" for number in range(1, 9)}:
            raise ValueError("Invalid restored current stage")
        if len(stages) != 8 or [str(row.get("key")) for row in stages] != [
            f"{number:02d}" for number in range(1, 9)
        ]:
            raise ValueError("A restored run must contain the canonical eight stages")

        now = utc_now()
        project_status = str(project.get("status") or run_status)
        if project_status not in PROJECT_STATUSES:
            raise ValueError("Invalid restored project status")
        prompt_pack = preset.get("prompt_pack")
        prompt_sha256 = str(preset.get("prompt_pack_sha256") or "")
        if not isinstance(prompt_pack, Mapping) or not _SHA256.fullmatch(prompt_sha256):
            raise ValueError("Invalid restored prompt preset")

        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone() is not None:
                raise ValueError("Project id is already registered")
            if connection.execute(
                "SELECT 1 FROM projects WHERE library_path=?",
                (str(project["library_path"]),),
            ).fetchone() is not None:
                raise ValueError("Project directory is already registered")
            if connection.execute(
                "SELECT 1 FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone() is not None:
                raise ValueError("Run id is already registered")

            existing_preset = connection.execute(
                "SELECT * FROM workflow_presets WHERE id=?", (preset_id,)
            ).fetchone()
            if existing_preset is None:
                connection.execute(
                    """INSERT INTO workflow_presets(
                        id,name,version,description,locked,prompt_pack_json,
                        prompt_pack_sha256,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        preset_id,
                        str(preset.get("name") or preset_id),
                        int(preset.get("version") or 1),
                        str(preset.get("description") or "Restored frozen prompt pack"),
                        1,
                        _json(prompt_pack),
                        prompt_sha256,
                        now,
                    ),
                )
            elif (
                str(existing_preset["prompt_pack_sha256"]) != prompt_sha256
                or _loads(str(existing_preset["prompt_pack_json"]), {}) != dict(prompt_pack)
            ):
                raise ValueError("Preset id conflicts with a different prompt pack")

            for frozen in provider_profiles:
                profile_id = str(frozen.get("id") or "")
                if not _PROFILE_ID.fullmatch(profile_id):
                    raise ValueError("Invalid restored provider profile id")
                existing = connection.execute(
                    "SELECT * FROM provider_profiles WHERE id=?", (profile_id,)
                ).fetchone()
                comparable = {
                    "service_kind": str(frozen["service_kind"]),
                    "provider_id": str(frozen["provider_id"]),
                    "base_url": frozen.get("base_url"),
                    "model": str(frozen["model"]),
                    "config": dict(frozen.get("config") or {}),
                    "capability": dict(frozen.get("capability") or {}),
                }
                if existing is None:
                    connection.execute(
                        """INSERT INTO provider_profiles(
                            id,service_kind,provider_id,display_name,base_url,model,
                            credential_ref,config_json,capability_json,enabled,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            profile_id,
                            comparable["service_kind"],
                            comparable["provider_id"],
                            str(frozen.get("display_name") or f"Restored {profile_id}"),
                            comparable["base_url"],
                            comparable["model"],
                            None,
                            _json(comparable["config"]),
                            _json(comparable["capability"]),
                            1,
                            now,
                            now,
                        ),
                    )
                else:
                    existing_comparable = {
                        "service_kind": str(existing["service_kind"]),
                        "provider_id": str(existing["provider_id"]),
                        "base_url": existing["base_url"],
                        "model": str(existing["model"]),
                        "config": _loads(str(existing["config_json"]), {}),
                        "capability": _loads(str(existing["capability_json"]), {}),
                    }
                    if existing_comparable != comparable:
                        raise ValueError(
                            f"Provider profile id conflicts with frozen run: {profile_id}"
                        )

            connection.execute(
                """INSERT INTO projects(
                    id,title,source_kind,source,source_display,library_path,status,current_stage,
                    progress_mode,progress_current,progress_total,progress_unit,needs_attention,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id,
                    str(project["title"]),
                    str(project["source_kind"]),
                    str(project.get("source") or ""),
                    str(project.get("source_display") or "source rebind required"),
                    str(project["library_path"]),
                    project_status,
                    current_stage,
                    "gate",
                    None,
                    None,
                    None,
                    1 if project.get("needs_attention") else 0,
                    _json(project.get("metadata") or {}),
                    str(project.get("created_at") or now),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO workflow_runs(
                    id,project_id,preset_id,status,current_stage,provider_lock_json,
                    prompt_lock_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    project_id,
                    preset_id,
                    run_status,
                    current_stage,
                    _json(provider_lock),
                    _json(prompt_lock),
                    str(project.get("created_at") or now),
                    now,
                ),
            )
            for ordinal, stage in enumerate(stages, 1):
                status = str(stage.get("status") or "draft")
                if status not in RUN_STATUSES:
                    raise ValueError("Invalid restored stage status")
                connection.execute(
                    """INSERT INTO stage_runs(
                        id,run_id,stage_key,ordinal,title,status,progress_mode,detail,
                        started_at,completed_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("stage"),
                        run_id,
                        str(stage["key"]),
                        ordinal,
                        str(stage["title"]),
                        status,
                        str(stage.get("progress_mode") or "gate"),
                        str(stage.get("detail") or "Restored cold snapshot"),
                        None,
                        now if status == "completed" else None,
                        now,
                    ),
                )

        self.emit_event(
            "project.restored",
            {
                "run_id": run_id,
                "current_stage": current_stage,
                "credentials_restored": False,
                "active_tasks_restored": False,
            },
            project_id=project_id,
            run_id=run_id,
        )
        run = self.get_run(run_id)
        assert run is not None
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.fetch_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
        if run:
            run["stages"] = self.fetch_all(
                "SELECT * FROM stage_runs WHERE run_id=? ORDER BY ordinal", (run_id,)
            )
        return run

    def list_runs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            return self.fetch_all(
                "SELECT * FROM workflow_runs WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            )
        return self.fetch_all("SELECT * FROM workflow_runs ORDER BY created_at DESC")

    def create_task(
        self,
        *,
        run_id: str,
        stage_key: str,
        kind: str,
        detail: str,
        progress_mode: str = "indeterminate",
        progress_total: float | None = None,
        progress_unit: str | None = None,
        input_payload: Mapping[str, object] | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = new_id("task")
        now = utc_now()
        self.execute(
            """INSERT INTO tasks(
                id,parent_task_id,run_id,stage_key,kind,status,progress_mode,progress_current,progress_total,
                progress_unit,detail,input_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                parent_task_id,
                run_id,
                stage_key,
                kind,
                "queued",
                progress_mode,
                0 if progress_total is not None else None,
                progress_total,
                progress_unit,
                detail,
                _json(input_payload or {}),
                now,
                now,
            ),
        )
        task = self.get_task(task_id)
        assert task is not None
        run = self.get_run(run_id)
        self.emit_event(
            "task.created",
            {"kind": kind, "stage_key": stage_key},
            project_id=run["project_id"] if run else None,
            run_id=run_id,
            task_id=task_id,
        )
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_tasks(self, active_only: bool = False) -> list[dict[str, Any]]:
        if active_only:
            placeholders = ",".join("?" for _ in (JOB_STATUSES - {"completed", "cancelled", "superseded"}))
            states = sorted(JOB_STATUSES - {"completed", "cancelled", "superseded"})
            return self.fetch_all(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY updated_at DESC", states
            )
        return self.fetch_all("SELECT * FROM tasks ORDER BY updated_at DESC")

    def update_task(self, task_id: str, **values: object) -> dict[str, Any]:
        allowed = {
            "status",
            "attempt",
            "progress_mode",
            "progress_current",
            "progress_total",
            "progress_unit",
            "detail",
            "error_code",
            "error_json",
            "result_json",
            "input_json",
            "started_at",
            "completed_at",
        }
        assignments: list[str] = []
        params: list[object] = []
        for key, value in values.items():
            storage_key = "error_json" if key == "error" else key
            storage_key = "result_json" if key == "result" else storage_key
            storage_key = "input_json" if key == "input" else storage_key
            if storage_key not in allowed:
                continue
            if storage_key == "status" and str(value) not in JOB_STATUSES:
                raise ValueError(f"Unsupported task status: {value}")
            if storage_key in {"error_json", "result_json", "input_json"}:
                value = _json(value or {})
            assignments.append(f"{storage_key}=?")
            params.append(value)
        assignments.append("updated_at=?")
        params.extend([utc_now(), task_id])
        self.execute(f"UPDATE tasks SET {','.join(assignments)} WHERE id=?", params)
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        run = self.get_run(task["run_id"])
        self.emit_event(
            "task.updated",
            {"status": task["status"], "detail": task["detail"]},
            project_id=run["project_id"] if run else None,
            run_id=task["run_id"],
            task_id=task_id,
        )
        self.sync_task_rollup(task_id)
        return task

    def sync_task_rollup(self, task_id: str) -> None:
        """Project durable task state onto its stage, run, and library row."""
        task = self.get_task(task_id)
        if task is None:
            return
        run = self.fetch_one("SELECT * FROM workflow_runs WHERE id=?", (task["run_id"],))
        if run is None:
            return
        tasks = self.fetch_all(
            "SELECT * FROM tasks WHERE run_id=? AND stage_key=? ORDER BY created_at",
            (task["run_id"], task["stage_key"]),
        )
        statuses = {str(row["status"]) for row in tasks}
        attention = statuses.intersection(
            {"blocked_uncertain", "repair_required", "waiting_provider", "waiting_worker"}
        )
        if attention:
            stage_status = min(attention)
        elif statuses and statuses <= {"completed"}:
            stage_status = "completed"
        elif statuses.intersection({"running", "pausing", "cancel_requested"}):
            stage_status = "running"
        elif statuses.intersection({"paused"}):
            stage_status = "paused"
        elif statuses.intersection({"cancelled"}):
            stage_status = "cancelled"
        elif statuses.intersection({"queued"}):
            stage_status = "queued"
        else:
            stage_status = str(task["status"])
        preferred = next((row for row in tasks if row.get("parent_task_id") is None), task)
        now = utc_now()
        self.execute(
            """UPDATE stage_runs SET status=?,progress_mode=?,progress_current=?,progress_total=?,
            progress_unit=?,detail=?,started_at=COALESCE(started_at,?),
            completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,?) ELSE completed_at END,
            updated_at=? WHERE run_id=? AND stage_key=?""",
            (
                stage_status,
                preferred.get("progress_mode") or "indeterminate",
                preferred.get("progress_current"),
                preferred.get("progress_total"),
                preferred.get("progress_unit"),
                preferred.get("detail") or "",
                preferred.get("started_at"),
                stage_status,
                now,
                now,
                task["run_id"],
                task["stage_key"],
            ),
        )
        run_status = (
            stage_status
            if stage_status in {
                "blocked_uncertain",
                "repair_required",
                "waiting_provider",
                "waiting_worker",
                "paused",
            }
            else "running"
        )
        self.execute(
            "UPDATE workflow_runs SET status=?,current_stage=?,updated_at=? WHERE id=?",
            (run_status, task["stage_key"], now, task["run_id"]),
        )
        project_status = run_status if run_status in PROJECT_STATUSES else "running"
        self.execute(
            """UPDATE projects SET status=?,current_stage=?,needs_attention=?,updated_at=?
            WHERE id=?""",
            (
                project_status,
                task["stage_key"],
                1 if attention else 0,
                now,
                run["project_id"],
            ),
        )

    def reserve_provider_request(
        self,
        *,
        task_id: str | None,
        profile_id: str | None,
        provider_id: str,
        requested_model: str,
        idempotency_key: str,
        input_sha256: str,
    ) -> dict[str, Any]:
        """Atomically reserve one billable request across local workers/devices."""
        if not provider_id or not requested_model or not idempotency_key:
            raise ValueError("Provider 请求缺少供应商、模型或幂等键")
        if not _SHA256.fullmatch(input_sha256):
            raise ValueError("Provider 请求输入哈希无效")
        now = utc_now()
        request_id = new_id("preq")
        created = False
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_requests WHERE provider_id=? AND idempotency_key=?",
                (provider_id, idempotency_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO provider_requests(
                        id,task_id,profile_id,provider_id,requested_model,idempotency_key,
                        status,billing_state,input_sha256,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request_id,
                        task_id,
                        profile_id,
                        provider_id,
                        requested_model,
                        idempotency_key,
                        "sending",
                        "pending",
                        input_sha256,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM provider_requests WHERE id=?", (request_id,)
                ).fetchone()
                created = True
            elif str(row["input_sha256"]) != input_sha256:
                raise ValueError("相同幂等键绑定了不同 Provider 输入")
        assert row is not None
        value = self._row(row)
        value["created"] = created
        return value

    def update_provider_request(self, request_id: str, **values: object) -> dict[str, Any]:
        allowed = {
            "status",
            "billing_state",
            "resolved_model",
            "provider_request_id",
            "usage_json",
            "cost_json",
            "result_json",
            "error_json",
        }
        current_before = self.get_provider_request(request_id)
        if current_before is None:
            raise KeyError(request_id)
        requested_status = values.get("status")
        if requested_status is not None:
            requested = str(requested_status)
            current_status = str(current_before["status"])
            if requested not in PROVIDER_REQUEST_STATUSES:
                raise ValueError(f"Unsupported provider request status: {requested}")
            if requested not in _PROVIDER_REQUEST_TRANSITIONS[current_status]:
                raise ValueError(
                    "Provider 请求终态不可回退或改写："
                    f"{current_status} -> {requested}"
                )
        current_status = str(current_before["status"])
        if current_status != "sending":
            for key, value in values.items():
                storage_key = {
                    "usage": "usage_json",
                    "cost": "cost_json",
                    "result": "result_json",
                    "error": "error_json",
                }.get(key, key)
                public_key = storage_key.removesuffix("_json")
                normalized = (value or {}) if storage_key.endswith("_json") else value
                if public_key == "status" and str(normalized) == current_status:
                    continue
                if current_before.get(public_key) != normalized:
                    raise ValueError("Provider 请求终态不可改写")
            return current_before
        assignments: list[str] = []
        parameters: list[object] = []
        for key, value in values.items():
            storage_key = {
                "usage": "usage_json",
                "cost": "cost_json",
                "result": "result_json",
                "error": "error_json",
            }.get(key, key)
            if storage_key not in allowed:
                continue
            if storage_key in {"usage_json", "cost_json", "result_json", "error_json"}:
                value = _json(value or {})
            assignments.append(f"{storage_key}=?")
            parameters.append(value)
        if not assignments:
            return current_before
        assignments.append("updated_at=?")
        parameters.extend([utc_now(), request_id])
        self.execute(
            f"UPDATE provider_requests SET {','.join(assignments)} WHERE id=?",
            parameters,
        )
        current = self.get_provider_request(request_id)
        if current is None:
            raise KeyError(request_id)
        self.emit_event(
            "provider_request.updated",
            {
                "request_id": request_id,
                "status": current["status"],
                "billing_state": current["billing_state"],
            },
            task_id=current.get("task_id"),
        )
        return current

    def get_provider_request(self, request_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM provider_requests WHERE id=?", (request_id,))

    def find_provider_request(
        self, provider_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self.fetch_one(
            "SELECT * FROM provider_requests WHERE provider_id=? AND idempotency_key=?",
            (provider_id, idempotency_key),
        )

    def list_provider_requests(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            return self.fetch_all(
                "SELECT * FROM provider_requests WHERE task_id=? ORDER BY created_at", (task_id,)
            )
        return self.fetch_all("SELECT * FROM provider_requests ORDER BY created_at DESC")

    def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int = 90) -> dict[str, str]:
        if not 15 <= ttl_seconds <= 3600:
            raise ValueError("Lease TTL must be 15..3600 seconds")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        token = uuid.uuid4().hex
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM worker_leases WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                expiry = datetime.fromisoformat(str(existing["lease_expires_at"]))
                # Acquisition always creates a fresh token.  Even another
                # thread/process using the same configured worker id must not
                # replace an active token, otherwise two tasks from one run can
                # execute concurrently and both send billable requests.
                if expiry > now_dt:
                    raise RuntimeError("Another worker owns this run")
            connection.execute(
                """INSERT INTO worker_leases(run_id,worker_id,lease_token,lease_expires_at,heartbeat_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    lease_token=excluded.lease_token,
                    lease_expires_at=excluded.lease_expires_at,
                    heartbeat_at=excluded.heartbeat_at""",
                (run_id, worker_id, token, expires, now),
            )
        return {
            "run_id": run_id,
            "worker_id": worker_id,
            "lease_token": token,
            "lease_expires_at": expires,
            "heartbeat_at": now,
        }

    def heartbeat_lease(self, run_id: str, token: str, ttl_seconds: int = 90) -> dict[str, str]:
        if not 15 <= ttl_seconds <= 3600:
            raise ValueError("Lease TTL must be 15..3600 seconds")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worker_leases WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or str(row["lease_token"]) != token:
                raise RuntimeError("Worker lease is missing or changed")
            if datetime.fromisoformat(str(row["lease_expires_at"])) <= now_dt:
                raise RuntimeError("Worker lease expired")
            connection.execute(
                "UPDATE worker_leases SET lease_expires_at=?,heartbeat_at=? WHERE run_id=?",
                (expires, now, run_id),
            )
            return {
                "run_id": run_id,
                "worker_id": str(row["worker_id"]),
                "lease_token": token,
                "lease_expires_at": expires,
                "heartbeat_at": now,
            }

    def release_lease(self, run_id: str, token: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM worker_leases WHERE run_id=? AND lease_token=?", (run_id, token)
            )
            return cursor.rowcount == 1
