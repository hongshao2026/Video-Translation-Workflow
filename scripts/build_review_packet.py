"""Deterministic source/review projection with full ID and hash evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def build(source: Path, output: Path, mode: str) -> dict:
    value = json.loads(source.read_text(encoding="utf-8-sig"))
    slots = value.get("slots", value.get("segments")) if isinstance(value, dict) else value
    if not isinstance(slots, list) or not slots:
        raise ValueError("Expected a nonempty slots/segments list")
    if mode == "source" and any(s.get("subtitle_zh") or s.get("recording_zh") for s in slots):
        raise ValueError("Agent T source packets must come from frozen source, not historical Chinese drafts")
    ids = [s["id"] for s in slots]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate stable IDs")
    fields = ["id", "source_text", "speaker", "speaker_id", "speaker_hint", "context", "chapter_id"]
    if mode == "review":
        fields.append("subtitle_zh")
    projected = []
    for slot in slots:
        if not slot.get("source_text") or (mode == "review" and not slot.get("subtitle_zh")):
            raise ValueError("Required source/translation text is empty")
        projected.append({key: slot[key] for key in fields if key in slot})
    text = "\n".join(json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in projected) + "\n"
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    if output.exists() or manifest_path.exists():
        raise ValueError("Use a new version; packet or manifest already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    manifest = {"schema_version": 1, "status": "pass", "mode": mode, "source": str(source.resolve()),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "output": str(output.resolve()), "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "slot_count": len(ids), "id_sequence_sha256": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                "missing_ids": [], "duplicate_ids": [], "order_preserved": True, "text_verbatim": True,
                "fields": fields, "scope": "Projection validation only; not translation review or production approval."}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["source", "review"], required=True)
    args = parser.parse_args()
    report = build(args.source, args.output, args.mode)
    print(json.dumps({"status": report["status"], "slot_count": report["slot_count"], "output": report["output"]}, ensure_ascii=True))
