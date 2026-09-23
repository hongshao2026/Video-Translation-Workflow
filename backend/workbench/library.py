"""Video library project creation and portable on-disk manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .database import WorkbenchDatabase
from .gates import validate_gate, validate_gate_file
from .settings import WorkbenchSettings

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}
SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
PROJECT_ID = re.compile(r"^prj_[A-Za-z0-9_.:-]{3,180}$")
RUN_ID = re.compile(r"^run_[A-Za-z0-9_.:-]{3,180}$")
PROFILE_ID = re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TEMPORARY_MEDIA_HOST_SUFFIXES = (
    ".googlevideo.com",
    ".googleusercontent.com",
    ".akamaized.net",
    ".cloudfront.net",
)
SENSITIVE_QUERY_MARKERS = (
    "access_key",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "expires",
    "key-pair-id",
    "policy",
    "signature",
    "token",
    "x-amz-",
)
SENSITIVE_QUERY_EXACT = {"auth", "jwt", "key", "password", "sig"}
SECRET_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "credential_ref",
    "password",
    "private_key",
    "refresh_token",
    "secret",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


def source_slug(source_kind: str, source: str) -> str:
    if source_kind == "video_url":
        parsed = urllib.parse.urlparse(source)
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.strip("/")
        elif parsed.hostname and parsed.hostname.lower().endswith("youtube.com"):
            candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        else:
            candidate = Path(parsed.path).stem
    else:
        candidate = Path(source).stem
    candidate = SAFE_ID.sub("-", candidate).strip("-_")[:48]
    return candidate or uuid.uuid4().hex[:12]


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _portable_public_url(value: str) -> str | None:
    """Return a credential-free public source URL, or ``None`` if it is ephemeral."""

    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    hostname = parsed.hostname.casefold().rstrip(".")
    if any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in TEMPORARY_MEDIA_HOST_SUFFIXES):
        return None
    for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered in SENSITIVE_QUERY_EXACT or any(
            marker in lowered for marker in SENSITIVE_QUERY_MARKERS
        ):
            return None
    # Fragments are browser-local and can contain OAuth tokens.  They are not
    # needed to redownload the source, so never persist them.
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        raise ValueError("项目清单包含无效相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("项目清单路径越出项目目录")
    if any(":" in part for part in path.parts):
        raise ValueError("项目清单路径包含设备限定符")
    return path.as_posix()


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered != "credential_configured" and any(
                marker in lowered for marker in SECRET_FIELD_MARKERS
            ):
                return True
            if _contains_secret_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(child) for child in value)
    return False


def _read_json_object(path: Path, *, maximum_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    if _is_link_or_junction(path) or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError(f"无法安全读取项目文件：{path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"项目文件不是有效 JSON：{path.name}") from exc
    if not isinstance(value, dict):
        # External migration documents use ValueError as the public validation contract.
        raise ValueError(f"项目文件必须是 JSON 对象：{path.name}")  # noqa: TRY004
    return value


class ProjectLibrary:
    def __init__(self, database: WorkbenchDatabase, settings: WorkbenchSettings) -> None:
        self.database = database
        self.settings = settings

    def create(
        self,
        *,
        source_kind: str,
        source: str,
        title: str | None = None,
        copy_local_file: bool = False,
    ) -> dict[str, Any]:
        source = source.strip()
        if source_kind == "video_url":
            parsed = urllib.parse.urlparse(source)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("视频链接必须是有效的 HTTP(S) 地址")
            portable_source = _portable_public_url(source)
            if not portable_source:
                raise ValueError("请输入无凭证的原始公开视频页，不要使用临时媒体直链或签名 URL")
            source = portable_source
            parsed = urllib.parse.urlparse(source)
            source_display = parsed.hostname + parsed.path
            local_source: Path | None = None
        elif source_kind == "local_file":
            local_source = Path(source).expanduser().resolve()
            if not local_source.is_file():
                raise ValueError("本地视频文件不存在")
            if local_source.suffix.lower() not in VIDEO_EXTENSIONS:
                raise ValueError("不支持这个本地视频格式")
            source = str(local_source)
            source_display = local_source.name
        else:
            raise ValueError("source_kind 必须是 video_url 或 local_file")

        self.settings.ensure_directories()
        slug = source_slug(source_kind, source)
        project_id = f"prj_{slug}_{uuid.uuid4().hex[:8]}"
        run_dir = self.settings.library_dir / f"{project_id}_run"
        for relative in ("source", "work", "qa", "deliverables/covers", "runtime", "exports"):
            (run_dir / relative).mkdir(parents=True, exist_ok=True)

        stored_source = source
        metadata: dict[str, Any] = {"portable_state": "ready", "source_bound": True}
        if local_source is not None:
            metadata["source_file"] = {
                "name": local_source.name,
                "byte_size": local_source.stat().st_size,
                "mtime_ns": local_source.stat().st_mtime_ns,
            }
            if copy_local_file:
                destination = run_dir / "source" / ("original" + local_source.suffix.lower())
                shutil.copy2(local_source, destination)
                stored_source = str(destination)
                metadata["source_file"]["copied_into_project"] = True
                metadata["source_file"]["sha256"] = sha256_file(destination)
            else:
                metadata["source_file"]["copied_into_project"] = False

        display_title = (title or (local_source.stem if local_source else slug)).strip()[:240]
        project = self.database.create_project(
            {
                "id": project_id,
                "title": display_title or "未命名视频",
                "source_kind": source_kind,
                "source": stored_source,
                "source_display": source_display,
                "library_path": str(run_dir.relative_to(self.settings.library_dir)),
                "status": "draft",
                "metadata": metadata,
            }
        )
        self._write_project_files(project, run_dir)
        return project

    def _write_project_files(self, project: dict[str, Any], run_dir: Path) -> None:
        self._write_project_manifest(project, run_dir)
        (run_dir / "PROJECT.md").write_text(
            "\n".join(
                [
                    f"# {project['title']}",
                    "",
                    f"project_id={project['id']}",
                    f"source_kind={project['source_kind']}",
                    "target_language=zh-CN",
                    "translation_mode=provider_agent_direct_quality_first",
                    "tts_speed=1.0",
                    "sync_strategy=video_retime_only",
                    "",
                    "## 当前状态",
                    "stage=01",
                    "next_gate=workflow_lock",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _source_reference(self, project: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
        source_kind = str(project["source_kind"])
        metadata = dict(project.get("metadata") or {})
        if source_kind == "video_url":
            portable = _portable_public_url(str(project.get("source") or ""))
            if portable:
                return {
                    "kind": "video_url",
                    "mode": "public_url",
                    "url": portable,
                }
            return {
                "kind": "video_url",
                "mode": "rebind_required",
                "reason": "temporary_or_credentialed_url_not_portable",
            }

        source_file = dict(metadata.get("source_file") or {})
        if source_file.get("copied_into_project"):
            raw_source = Path(str(project.get("source") or ""))
            resolved = raw_source.resolve() if raw_source.is_absolute() else (run_dir / raw_source).resolve()
            project_root = run_dir.resolve()
            if not resolved.is_relative_to(project_root):
                raise ValueError("复制入项目的源文件越出项目目录")
            if _is_link_or_junction(resolved) or not resolved.is_file():
                raise ValueError("复制入项目的源文件不存在或是链接")
            relative = resolved.relative_to(project_root).as_posix()
            if not relative.startswith("source/") or resolved.suffix.casefold() not in VIDEO_EXTENSIONS:
                raise ValueError("复制入项目的源文件路径无效")
            return {
                "kind": "local_file",
                "mode": "project_relative_file",
                "path": relative,
                "name": resolved.name,
                "byte_size": resolved.stat().st_size,
                "sha256": sha256_file(resolved),
            }
        return {
            "kind": "local_file",
            "mode": "rebind_required",
            "reason": "device_local_source_not_copied",
            "name": str(source_file.get("name") or project.get("source_display") or "source"),
            "byte_size": int(source_file.get("byte_size") or 0),
        }

    def _write_project_manifest(
        self,
        project: Mapping[str, Any],
        run_dir: Path,
        *,
        workflow: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing_path = run_dir / "project.json"
        if workflow is None and existing_path.is_file() and not _is_link_or_junction(existing_path):
            try:
                existing = _read_json_object(existing_path)
                existing_workflow = existing.get("workflow")
                workflow = existing_workflow if isinstance(existing_workflow, Mapping) else None
            except ValueError:
                workflow = None
        source_reference = self._source_reference(project, run_dir)
        source_display = project["source_display"]
        if (
            source_reference.get("kind") == "video_url"
            and source_reference.get("mode") == "rebind_required"
        ):
            source_display = "视频源（需重新绑定）"
        manifest = {
            "schema_version": 2,
            "project_id": project["id"],
            "title": project["title"],
            "source_kind": project["source_kind"],
            "source_display": source_display,
            "source_reference": source_reference,
            "created_at": project["created_at"],
            "paths_are_relative_to_project": True,
            "credentials_included": False,
            "runtime_process_state_included": False,
            "provider_request_ledger_included": False,
            "workflow": dict(workflow) if workflow is not None else None,
        }
        atomic_json(existing_path, manifest)
        return manifest

    def bind_workflow(
        self,
        project: Mapping[str, Any],
        *,
        run_id: str,
        preset_id: str,
        workflow_lock_path: Path,
    ) -> dict[str, Any]:
        run_dir = self.path_for(dict(project))
        resolved_lock = workflow_lock_path.resolve()
        if not resolved_lock.is_relative_to(run_dir.resolve()) or not resolved_lock.is_file():
            raise ValueError("工作流锁不在项目目录内")
        workflow = {
            "run_id": run_id,
            "preset_id": preset_id,
            "workflow_lock": {
                "path": resolved_lock.relative_to(run_dir.resolve()).as_posix(),
                "sha256": sha256_file(resolved_lock),
            },
        }
        return self._write_project_manifest(project, run_dir, workflow=workflow)

    def path_for(self, project: dict[str, Any]) -> Path:
        candidate = (self.settings.library_dir / str(project["library_path"])).resolve()
        if not candidate.is_relative_to(self.settings.library_dir.resolve()):
            raise ValueError("Project path escapes the configured library")
        return candidate

    def _require_real_project_tree(self, project_dir: Path) -> tuple[Path, str]:
        self.settings.ensure_directories()
        library_root = self.settings.library_dir.resolve()
        requested = project_dir.expanduser().absolute()
        try:
            requested.relative_to(library_root)
        except ValueError as exc:
            raise ValueError("只能登记资料库根目录内的项目") from exc
        cursor = library_root
        for part in requested.relative_to(library_root).parts:
            cursor = cursor / part
            if _is_link_or_junction(cursor):
                raise ValueError("项目路径不能包含符号链接或目录联接")
        resolved = requested.resolve()
        if resolved == library_root or not resolved.is_relative_to(library_root):
            raise ValueError("项目目录必须位于资料库根目录之下")
        if not resolved.is_dir():
            raise ValueError("项目目录不存在")
        for current_root, directory_names, file_names in os.walk(
            resolved, topdown=True, followlinks=False
        ):
            current = Path(current_root)
            for name in [*directory_names, *file_names]:
                if _is_link_or_junction(current / name):
                    raise ValueError("项目目录包含不能安全恢复的链接")
        relative = resolved.relative_to(library_root).as_posix()
        return resolved, relative

    @staticmethod
    def _restore_source_reference(
        manifest: Mapping[str, Any], project_root: Path
    ) -> tuple[str, str, dict[str, Any]]:
        reference = manifest.get("source_reference")
        if not isinstance(reference, Mapping):
            raise ValueError("project.json 缺少可迁移的 source_reference")  # noqa: TRY004
        kind = str(reference.get("kind") or "")
        mode = str(reference.get("mode") or "")
        if kind != str(manifest.get("source_kind") or "") or kind not in {
            "video_url",
            "local_file",
        }:
            raise ValueError("项目源类型与 source_reference 不一致")
        metadata: dict[str, Any] = {
            "portable_state": "restored",
            "source_bound": False,
            "source_rebind_required": True,
        }

        def safe_name(value: object, fallback: str = "source") -> str:
            name = str(value or fallback)
            if (
                not name
                or len(name) > 255
                or name in {".", ".."}
                or Path(name).name != name
                or "/" in name
                or "\\" in name
                or "\x00" in name
            ):
                raise ValueError("source_reference 文件名无效")
            return name

        if kind == "video_url" and mode == "public_url":
            safe_url = _portable_public_url(str(reference.get("url") or ""))
            if not safe_url or safe_url != reference.get("url"):
                raise ValueError("公开视频源 URL 不安全或已被篡改")
            metadata.update(source_bound=True, source_rebind_required=False)
            return safe_url, urllib.parse.urlsplit(safe_url).hostname or "video URL", metadata
        if kind == "local_file" and mode == "project_relative_file":
            relative = _safe_relative_path(reference.get("path"))
            if not relative.startswith("source/"):
                raise ValueError("项目内源文件必须位于 source 目录")
            candidate = project_root.joinpath(*PurePosixPath(relative).parts)
            expected_hash = str(reference.get("sha256") or "")
            expected_size = reference.get("byte_size")
            if not SHA256.fullmatch(expected_hash) or isinstance(expected_size, bool) or not isinstance(
                expected_size, int
            ) or expected_size < 0:
                raise ValueError("项目内源文件绑定缺少有效大小或哈希")
            metadata["source_file"] = {
                "name": safe_name(reference.get("name"), candidate.name),
                "byte_size": expected_size,
                "sha256": expected_hash,
                "copied_into_project": True,
            }
            if candidate.exists():
                if _is_link_or_junction(candidate) or not candidate.is_file():
                    raise ValueError("项目内源文件不是普通文件")
                if candidate.stat().st_size != expected_size or sha256_file(candidate) != expected_hash:
                    raise ValueError("项目内源文件与 project.json 哈希绑定不一致")
                metadata.update(source_bound=True, source_rebind_required=False)
                return str(candidate), candidate.name, metadata
            metadata["source_missing_after_transfer"] = True
            return "", safe_name(reference.get("name"), candidate.name), metadata
        if mode != "rebind_required":
            raise ValueError("source_reference 模式无效")
        if kind == "local_file":
            size = reference.get("byte_size", 0)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("本地源文件大小无效")
            metadata["source_file"] = {
                "name": safe_name(reference.get("name")),
                "byte_size": size,
                "copied_into_project": False,
            }
            return "", metadata["source_file"]["name"], metadata
        return "", "视频源（需重新绑定）", metadata

    @staticmethod
    def _restored_stages(project_root: Path) -> tuple[list[dict[str, Any]], str, bool]:
        # Only deterministic, hash-bound gates advance a cold snapshot.  A
        # missing media file naturally keeps later gates from validating.
        completed_through = 1
        gate_candidates: tuple[tuple[int, str, list[Path]], ...] = (
            (3, "ad_edit_gate", [project_root / "qa" / "ad_edit_gate.json"]),
            (5, "translation_gate", [project_root / "qa" / "translation_gate.json"]),
            (7, "production_gate", [project_root / "qa" / "production_gate.json"]),
            (
                8,
                "publication_package_gate",
                sorted((project_root / "qa").glob("publication_package_gate_v*.json")),
            ),
        )
        for stage_number, kind, paths in gate_candidates:
            if any(
                path.is_file()
                and not _is_link_or_junction(path)
                and validate_gate_file(path, kind=kind, artifact_root=project_root).valid
                for path in paths
            ):
                completed_through = max(completed_through, stage_number)
        from .workflows import STAGES  # Local import avoids a module cycle.

        all_complete = completed_through == 8
        current_number = 8 if all_complete else completed_through + 1
        stages: list[dict[str, Any]] = []
        for stage in STAGES:
            number = int(stage["key"])
            status = "completed" if number <= completed_through else (
                "paused" if number == current_number else "draft"
            )
            stages.append({**stage, "status": status})
        return stages, f"{current_number:02d}", all_complete

    def restore_existing(self, project_dir: Path) -> dict[str, Any]:
        """Register a verified cold project snapshot in this device's SQLite index."""

        project_root, library_path = self._require_real_project_tree(project_dir)
        manifest = _read_json_object(project_root / "project.json")
        if manifest.get("schema_version") != 2:
            raise ValueError("项目清单版本不可安全恢复；请在原设备重新导出")
        if (
            manifest.get("paths_are_relative_to_project") is not True
            or manifest.get("credentials_included") is not False
            or manifest.get("runtime_process_state_included") is not False
            or manifest.get("provider_request_ledger_included") is not False
        ):
            raise ValueError("项目清单声明包含设备状态、凭证或付费请求账本")
        if _contains_secret_field(manifest):
            raise ValueError("project.json 包含禁止迁移的凭证字段")
        project_id = str(manifest.get("project_id") or "")
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("project.json 的 project_id 无效")
        if self.database.get_project(project_id) is not None:
            raise ValueError("该 project_id 已在资料库中")
        if self.database.get_project_by_library_path(library_path) is not None:
            raise ValueError("该项目目录已在资料库中")
        title = str(manifest.get("title") or "").strip()
        if not title or len(title) > 240:
            raise ValueError("project.json 的项目标题无效")

        workflow = manifest.get("workflow")
        if not isinstance(workflow, Mapping):
            raise ValueError("项目尚未绑定可恢复的工作流运行")  # noqa: TRY004
        run_id = str(workflow.get("run_id") or "")
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("项目运行 ID 无效")
        lock_binding = workflow.get("workflow_lock")
        if not isinstance(lock_binding, Mapping):
            raise ValueError("project.json 缺少工作流锁绑定")  # noqa: TRY004
        lock_relative = _safe_relative_path(lock_binding.get("path"))
        if lock_relative != "qa/workflow_lock.json":
            raise ValueError("工作流锁路径必须为 qa/workflow_lock.json")
        expected_lock_hash = str(lock_binding.get("sha256") or "")
        lock_path = project_root / "qa" / "workflow_lock.json"
        if not SHA256.fullmatch(expected_lock_hash) or sha256_file(lock_path) != expected_lock_hash:
            raise ValueError("workflow_lock.json 与 project.json 哈希绑定不一致")
        lock = _read_json_object(lock_path)
        if str(lock.get("project_id") or "") != project_id or str(lock.get("run_id") or "") != run_id:
            raise ValueError("工作流锁绑定了不同的项目或运行")
        result = validate_gate("workflow_lock", lock, artifact_root=project_root)
        if not result.valid:
            raise ValueError("工作流锁无法在本机验证：" + "; ".join(result.errors))

        provider_lock = lock.get("provider_lock")
        prompt_lock = lock.get("prompt_lock")
        if not isinstance(provider_lock, Mapping) or not isinstance(prompt_lock, Mapping):
            raise ValueError("工作流锁缺少冻结 Provider 或提示词配置")  # noqa: TRY004
        if _contains_secret_field(provider_lock) or _contains_secret_field(prompt_lock):
            raise ValueError("冻结配置包含禁止迁移的凭证字段")
        if str(prompt_lock.get("preset_id") or "") != str(workflow.get("preset_id") or ""):
            raise ValueError("项目清单与提示词锁的 preset_id 不一致")
        prompt_artifact = prompt_lock.get("artifact")
        if not isinstance(prompt_artifact, Mapping):
            raise ValueError("提示词锁缺少冻结制品")  # noqa: TRY004
        prompt_relative = _safe_relative_path(prompt_artifact.get("path"))
        prompt_path = project_root.joinpath(*PurePosixPath(prompt_relative).parts)
        prompt_sha = str(prompt_artifact.get("sha256") or "")
        if not SHA256.fullmatch(prompt_sha) or sha256_file(prompt_path) != prompt_sha:
            raise ValueError("冻结提示词制品缺失或已篡改")
        prompt_pack = _read_json_object(prompt_path)
        declared_prompt_sha = str(prompt_lock.get("prompt_pack_sha256") or "")
        if not SHA256.fullmatch(declared_prompt_sha):
            raise ValueError("提示词锁的源版本哈希无效")

        frozen_profiles = provider_lock.get("profiles")
        if not isinstance(frozen_profiles, Mapping):
            raise ValueError("冻结 Provider 配置格式无效")  # noqa: TRY004
        if provider_lock.get("fallback_policy") != "manual":
            raise ValueError("恢复运行只能使用手动 Provider 降级策略")
        restored_profiles: list[dict[str, Any]] = []
        credential_rebind: list[str] = []
        for profile_id, raw_profile in frozen_profiles.items():
            profile_id = str(profile_id)
            if not PROFILE_ID.fullmatch(profile_id) or not isinstance(raw_profile, Mapping):
                raise ValueError("冻结 Provider 配置 ID 无效")
            service_kind = str(raw_profile.get("service_kind") or "")
            if service_kind not in {"llm", "speech"}:
                raise ValueError("冻结 Provider 服务类型无效")
            provider_id = str(raw_profile.get("provider_id") or "")
            model = str(raw_profile.get("model") or "")
            if not provider_id or len(provider_id) > 120 or not model or len(model) > 240:
                raise ValueError("冻结 Provider 标识或模型无效")
            base_url = raw_profile.get("base_url")
            if base_url is not None:
                parsed = urllib.parse.urlsplit(str(base_url))
                remote_https = parsed.scheme == "https" and bool(parsed.hostname)
                local_http = (
                    parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
                if (
                    not (remote_https or local_http)
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("冻结 Provider 接口地址不安全")
            restored_profiles.append(
                {
                    "id": profile_id,
                    "service_kind": service_kind,
                    "provider_id": provider_id,
                    "base_url": base_url,
                    "model": model,
                    "config": dict(raw_profile.get("config") or {}),
                    "capability": dict(raw_profile.get("capability") or {}),
                }
            )
            existing = self.database.get_provider_profile(profile_id)
            if existing is None or not existing.get("credential_ref"):
                credential_rebind.append(profile_id)

        role_bindings = dict(provider_lock.get("role_bindings") or {})
        if set(role_bindings) - {"T", "A", "B", "C"}:
            raise ValueError("冻结工作流包含未知 Provider 角色")
        referenced_profiles = {str(value) for value in role_bindings.values()}
        speech_profile = provider_lock.get("speech_profile_id")
        if speech_profile:
            referenced_profiles.add(str(speech_profile))
        if not referenced_profiles.issubset({str(key) for key in frozen_profiles}):
            raise ValueError("工作流角色引用了未冻结的 Provider 配置")
        profile_by_id = {str(row["id"]): row for row in restored_profiles}
        if any(
            profile_by_id[str(profile_id)]["service_kind"] != "llm"
            for profile_id in role_bindings.values()
        ):
            raise ValueError("翻译角色必须绑定 LLM Provider")
        if speech_profile and profile_by_id[str(speech_profile)]["service_kind"] != "speech":
            raise ValueError("语音角色必须绑定 Speech Provider")

        source, source_display, metadata = self._restore_source_reference(manifest, project_root)
        stages, current_stage, all_complete = self._restored_stages(project_root)
        needs_provider = bool(credential_rebind) and int(current_stage) >= 4 and not all_complete
        run_status = "machine_passed" if all_complete else (
            "waiting_provider" if needs_provider else "paused"
        )
        if not all_complete:
            for stage in stages:
                if stage["key"] == current_stage:
                    stage["status"] = run_status
        project_status = run_status if run_status in {
            "machine_passed",
            "waiting_provider",
            "paused",
        } else "paused"
        metadata.update(
            {
                "restored_run_id": run_id,
                "credentials_restored": False,
                "active_tasks_restored": False,
                "provider_request_ledger_restored": False,
                "credential_rebind_profile_ids": sorted(credential_rebind),
            }
        )
        receipt = {
            "schema_version": "dub-workbench-attach-receipt/v1",
            "status": "pass",
            "project_id": project_id,
            "run_id": run_id,
            "workflow_lock_sha256": expected_lock_hash,
            "credentials_restored": False,
            "active_tasks_restored": False,
            "provider_request_ledger_restored": False,
            "credential_rebind_profile_ids": sorted(credential_rebind),
            "source_rebind_required": not metadata["source_bound"],
        }
        receipt_path = project_root / ".dub-workbench" / "attach-receipt.json"
        atomic_json(receipt_path, receipt)
        try:
            self.database.restore_project_run(
                project={
                    "id": project_id,
                    "title": title,
                    "source_kind": str(manifest["source_kind"]),
                    "source": source,
                    "source_display": source_display,
                    "library_path": library_path,
                    "status": project_status,
                    "current_stage": current_stage,
                    "needs_attention": bool(credential_rebind)
                    or not metadata["source_bound"],
                    "metadata": metadata,
                    "created_at": manifest.get("created_at"),
                },
                run_id=run_id,
                preset={
                    "id": str(prompt_lock["preset_id"]),
                    "name": str(prompt_lock.get("name") or prompt_lock["preset_id"]),
                    "version": int(prompt_lock.get("version") or 1),
                    "prompt_pack": prompt_pack,
                    "prompt_pack_sha256": declared_prompt_sha,
                },
                stages=stages,
                provider_lock=provider_lock,
                prompt_lock=prompt_lock,
                provider_profiles=restored_profiles,
                run_status=run_status,
                current_stage=current_stage,
            )
        except Exception:
            receipt_path.unlink(missing_ok=True)
            raise
        detail = self.detail(project_id)
        assert detail is not None
        return {"status": "attached", "project": detail, "restore": receipt}

    attach = restore_existing

    def rebind_source(self, project_id: str, *, source_kind: str, source: str) -> dict[str, Any]:
        project = self.database.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if source_kind == "video_url":
            portable = _portable_public_url(source)
            if not portable:
                raise ValueError("只能重新绑定无凭证、非临时直链的 HTTP(S) 视频页")
            metadata = dict(project.get("metadata") or {})
            metadata.update(source_bound=True, source_rebind_required=False)
            updated = self.database.rebind_project_source(
                project_id,
                source_kind=source_kind,
                source=portable,
                source_display=urllib.parse.urlsplit(portable).hostname or "video URL",
                metadata=metadata,
            )
        elif source_kind == "local_file":
            local = Path(source).expanduser().resolve()
            if _is_link_or_junction(local) or not local.is_file() or local.suffix.casefold() not in VIDEO_EXTENSIONS:
                raise ValueError("重新绑定的本地视频不存在、是链接或格式不支持")
            metadata = dict(project.get("metadata") or {})
            metadata.update(
                source_bound=True,
                source_rebind_required=False,
                source_file={
                    "name": local.name,
                    "byte_size": local.stat().st_size,
                    "mtime_ns": local.stat().st_mtime_ns,
                    "copied_into_project": False,
                },
            )
            updated = self.database.rebind_project_source(
                project_id,
                source_kind=source_kind,
                source=str(local),
                source_display=local.name,
                metadata=metadata,
            )
        else:
            raise ValueError("source_kind 必须是 video_url 或 local_file")
        self._write_project_manifest(updated, self.path_for(updated))
        return updated

    def detail(self, project_id: str) -> dict[str, Any] | None:
        project = self.database.get_project(project_id)
        if project is None:
            return None
        project["runs"] = self.database.list_runs(project_id)
        return project
