"""Generate a resumable local library of official MiniMax Mandarin previews."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.minimax_client import (
    MiniMaxClient,
    MiniMaxError,
    SpeechConfig,
    estimate_cost,
)
from backend.minimax_preview_cache import (
    PREVIEW_SAMPLE_TEXT,
    is_mandarin_system_voice,
    load_preview_manifest,
)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _filename(voice_id: str, extension: str) -> str:
    digest = hashlib.sha256(voice_id.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.{extension}"


def generate_library(
    *,
    api_key: str,
    output_dir: Path,
    requests_per_minute: int,
    sample_text: str = PREVIEW_SAMPLE_TEXT,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest = load_preview_manifest(manifest_path)
    manifest.update(
        {
            "version": 1,
            "sample_text": sample_text,
            "config": SpeechConfig(emotion="calm").public_dict(),
        }
    )
    entries: dict[str, dict[str, Any]] = manifest["voices"]

    client = MiniMaxClient(api_key)
    account_voices = client.list_voices()
    mandarin_voices = [voice for voice in account_voices if is_mandarin_system_voice(voice)]
    if len(mandarin_voices) != 58:
        raise RuntimeError(
            f"当前账号识别到 {len(mandarin_voices)} 个普通话系统音色，预期 58；为避免误扣费，已停止。"
        )

    config = SpeechConfig(emotion="calm")
    estimate = estimate_cost(sample_text, config.model)
    interval = 60 / requests_per_minute
    ready_before = sum(
        1
        for voice in mandarin_voices
        if (entry := entries.get(voice["voice_id"]))
        and entry.get("status") == "ready"
        and (output_dir / str(entry.get("filename") or "")).is_file()
    )
    print(
        f"CATALOG voices={len(account_voices)} mandarin=58 cached={ready_before} "
        f"pending={58 - ready_before} billable_chars_each={estimate['billable_characters']}",
        flush=True,
    )

    generated = 0
    skipped = 0
    failures = 0
    last_call_started = 0.0
    for index, voice in enumerate(mandarin_voices, start=1):
        voice_id = voice["voice_id"]
        existing = entries.get(voice_id) or {}
        existing_path = output_dir / str(existing.get("filename") or "")
        if existing.get("status") == "ready" and existing_path.is_file() and existing_path.stat().st_size:
            skipped += 1
            print(f"SKIP {index:02d}/58 {voice['voice_name']}", flush=True)
            continue

        elapsed = time.monotonic() - last_call_started
        if last_call_started and elapsed < interval:
            time.sleep(interval - elapsed)
        last_call_started = time.monotonic()
        print(f"CALL {index:02d}/58 {voice['voice_name']}", flush=True)
        try:
            result = client.synthesize(sample_text, voice_id, config)
            extension = result.audio_format if result.audio_format in {"mp3", "wav", "flac"} else config.format
            filename = _filename(voice_id, extension)
            destination = output_dir / filename
            destination.write_bytes(result.audio)
            actual_characters = int(
                result.extra_info.get("usage_characters") or estimate["billable_characters"]
            )
            entries[voice_id] = {
                **voice,
                "catalog_index": index,
                "filename": filename,
                "status": "ready",
                "model": config.model,
                "sample_text": sample_text,
                "usage_characters": actual_characters,
                "audio_length_ms": result.extra_info.get("audio_length"),
                "trace_id": result.trace_id,
                "generated_at": datetime.now(UTC).isoformat(),
            }
            generated += 1
            print(
                f"READY {index:02d}/58 bytes={len(result.audio)} chars={actual_characters} file={filename}",
                flush=True,
            )
        except MiniMaxError as exc:
            entries[voice_id] = {
                **voice,
                "catalog_index": index,
                "status": "uncertain" if exc.uncertain_completion else "failed",
                "error": str(exc),
                "trace_id": exc.trace_id,
                "attempted_at": datetime.now(UTC).isoformat(),
            }
            failures += 1
            print(
                f"FAILED {index:02d}/58 uncertain={exc.uncertain_completion} message={exc}",
                flush=True,
            )
        finally:
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            _write_manifest(manifest_path, manifest)

    report = {
        "total": len(mandarin_voices),
        "generated": generated,
        "skipped": skipped,
        "failures": failures,
        "manifest": str(manifest_path),
    }
    print("COMPLETE " + json.dumps(report, ensure_ascii=False), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rpm", type=int, choices=(10, 20), default=10)
    args = parser.parse_args()
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if len(api_key) < 8:
        raise RuntimeError("API Key 文件内容无效")
    report = generate_library(
        api_key=api_key,
        output_dir=args.output_dir,
        requests_per_minute=args.rpm,
    )
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
