"""Run a bounded MiniMax connectivity check without exposing credentials.

The default mode only uses non-generating model and voice catalogue endpoints.
Pass ``--live`` to authorize exactly one short text completion and one short
speech synthesis request.  There are no automatic retries.  Audio and the
sanitized report are written below the ignored runtime directory by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.providers import (
    LLMRequest,
    MiniMaxLLMProvider,
    MiniMaxSpeechProvider,
    ProviderError,
    ProviderProfile,
    SpeechRequest,
)

REGIONS = {
    "cn": ("https://api.minimax.cn/v1", "https://api.minimax.cn"),
    "global": ("https://api.minimax.io/v1", "https://api.minimax.io"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--credential-file",
        type=Path,
        help="UTF-8 text file containing only the API key; the path is not recorded.",
    )
    source.add_argument(
        "--credential-env",
        default="MINIMAX_API_KEY",
        help="Environment variable containing the API key (default: MINIMAX_API_KEY).",
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="cn")
    parser.add_argument("--model", help="Model ID for the optional live text call.")
    parser.add_argument("--voice", help="Voice ID for the optional live speech call.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make one minimal billable text call and one minimal billable speech call.",
    )
    parser.add_argument(
        "--probe-video",
        action="store_true",
        help="Read the existing video-task list; never creates a video generation task.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "runtime" / "connectivity",
        help="Ignored directory for the sanitized report and optional audio.",
    )
    return parser.parse_args()


def load_key(args: argparse.Namespace) -> str:
    if args.credential_file:
        value = args.credential_file.read_text(encoding="utf-8-sig").strip()
    else:
        value = os.environ.get(args.credential_env, "").strip()
    if not 8 <= len(value) <= 512 or any(character.isspace() for character in value):
        raise ValueError("MiniMax credential is missing or malformed")
    return value


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def choose_model(model_ids: list[str], requested: str | None) -> str:
    if requested:
        if requested not in model_ids:
            raise ValueError("Requested model is not visible to this credential")
        return requested
    for candidate in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2"):
        if candidate in model_ids:
            return candidate
    if not model_ids:
        raise ValueError("MiniMax returned no available text models")
    return model_ids[0]


def choose_voice(voice_ids: list[str], requested: str | None) -> str:
    if requested:
        if requested not in voice_ids:
            raise ValueError("Requested voice is not visible to this credential")
        return requested
    preferred = "Chinese (Mandarin)_Reliable_Executive"
    if preferred in voice_ids:
        return preferred
    if not voice_ids:
        raise ValueError("MiniMax returned no available voices")
    return voice_ids[0]


def main() -> int:
    args = parse_args()
    key = load_key(args)
    llm_base, speech_base = REGIONS[args.region]
    llm = MiniMaxLLMProvider(
        ProviderProfile(
            profile_id="minimax-connectivity-llm",
            provider_id="minimax-llm",
            kind="llm",
            base_url=llm_base,
            model=args.model,
            credential_ref="runtime-only",
        ),
        key,
    )
    speech = MiniMaxSpeechProvider(
        ProviderProfile(
            profile_id="minimax-connectivity-speech",
            provider_id="minimax-speech",
            kind="speech",
            base_url=speech_base,
            model="speech-2.8-turbo",
            credential_ref="runtime-only",
        ),
        key,
    )

    started = datetime.now(UTC)
    report: dict[str, Any] = {
        "schema_version": 1,
        "provider": "MiniMax",
        "region": args.region,
        "started_at": started.isoformat(),
        "live_generation_authorized": bool(args.live),
        "automatic_retries": 0,
    }
    try:
        models = llm.list_models()
        voices = speech.list_voices()
        model_ids = [item.model_id for item in models]
        voice_ids = [item.voice_id for item in voices]
        model = choose_model(model_ids, args.model)
        voice = choose_voice(voice_ids, args.voice)
        report["probes"] = {
            "models": {"status": "pass", "count": len(model_ids)},
            "voices": {"status": "pass", "count": len(voice_ids)},
        }
        if args.probe_video:
            try:
                response = requests.get(
                    f"{speech_base}/v2/query/video_generation",
                    params={"page_num": 1, "page_size": 1},
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=(30, 30),
                )
            except requests.RequestException as exc:
                raise RuntimeError("MiniMax video status probe failed") from exc
            try:
                video_body = response.json()
            except ValueError as exc:
                raise RuntimeError("MiniMax video status probe returned invalid JSON") from exc
            service_code = (
                (video_body.get("base_resp") or {}).get("status_code")
                if isinstance(video_body, dict)
                else None
            )
            if response.status_code != 200 or service_code not in (None, 0):
                raise RuntimeError("MiniMax video status probe was rejected")
            report["probes"]["video_tasks"] = {
                "status": "pass",
                "operation": "list_existing_tasks",
                "created_generation_task": False,
                "billable_generation_requested": False,
            }
        report["selection"] = {"model": model, "voice": voice}

        if args.live:
            completion = llm.generate(
                LLMRequest.from_messages(
                    [{"role": "user", "content": "只回复 OK。"}],
                    model=model,
                    temperature=0,
                    max_output_tokens=16,
                    idempotency_key="dub-workbench-minimax-connectivity-llm-v1",
                )
            )
            speech_result = speech.synthesize(
                SpeechRequest(
                    text="测试。",
                    voice_id=voice,
                    model="speech-2.8-turbo",
                    speed=1.0,
                    idempotency_key="dub-workbench-minimax-connectivity-speech-v1",
                )
            )
            args.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = started.strftime("%Y%m%dT%H%M%SZ")
            audio_path = args.output_dir / f"minimax_tts_smoke_{stamp}.{speech_result.audio_format}"
            audio_path.write_bytes(speech_result.audio)
            report["live"] = {
                "llm": {
                    "status": "pass",
                    "model": completion.model,
                    "response_sha256": sha256(completion.text.encode("utf-8")),
                    "output_tokens": completion.usage.output_tokens,
                },
                "speech": {
                    "status": "pass",
                    "model": speech_result.model,
                    "voice": speech_result.voice_id,
                    "speed": 1.0,
                    "audio_format": speech_result.audio_format,
                    "audio_bytes": len(speech_result.audio),
                    "audio_sha256": sha256(speech_result.audio),
                    "usage_units": speech_result.usage_units,
                    "artifact": audio_path.name,
                },
            }
        else:
            report["live"] = {"status": "not_run"}
        report["status"] = "pass"
    except ProviderError as exc:
        report["status"] = "fail"
        report["error"] = exc.public_dict()
    except Exception as exc:  # noqa: BLE001 - top-level probe reports a sanitized failure type.
        report["status"] = "fail"
        report["error"] = {"type": type(exc).__name__}
    finally:
        key = ""

    report["finished_at"] = datetime.now(UTC).isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / f"minimax_connectivity_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": report["status"], "report": report_path.name}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
