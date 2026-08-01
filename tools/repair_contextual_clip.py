#!/usr/bin/env python3
"""Safely repair one failed clip with full-video Chinese voice context."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydub import AudioSegment

from videotrans import tts
from videotrans.dub.contextual_repair import contextual_chinese_anchor_bank
from videotrans.dub.quality_manifest import (
    QualityManifest, file_hash, queue_quality_coverage, text_hash, unit_key,
)
from videotrans.dub.store import atomic_write_json
from videotrans.tts._f5tts import F5TTS


def _clear_marks(root: Path, *names: str) -> None:
    sidecar = root / "lang_leak.json"
    try:
        marks = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(marks, dict):
            return
        for name in names:
            marks.pop(Path(name).name, None)
        if marks:
            atomic_write_json(sidecar, marks)
        else:
            sidecar.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError, TypeError):
        pass


def repair(queue_file: Path, line: int, language: str = "zh-cn") -> Path:
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    index = int(line) - 1
    if not isinstance(queue, list) or not 0 <= index < len(queue):
        raise ValueError(f"line must be between 1 and {len(queue) if isinstance(queue, list) else 0}")
    root = queue_file.parent
    coverage = queue_quality_coverage(
        queue, root,
        rules_version=F5TTS.QUALITY_RULES_VERSION,
        validator_model=F5TTS.VALIDATOR_MODEL,
        verify_audio_hashes=True,
    )
    for idx, entry in coverage["entries"].items():
        if entry.get("passed"):
            queue[idx].pop("lang_leak", None)
            queue[idx].pop("quality_status", None)
            queue[idx].pop("quality_failures", None)
        else:
            queue[idx]["lang_leak"] = str(entry.get("transcript") or "")[:200]
            queue[idx]["quality_status"] = "needs_review"

    target = queue[index]
    original = Path(str(target.get("filename") or ""))
    if not original.is_file():
        raise FileNotFoundError(f"original clip does not exist: {original}")
    bank = contextual_chinese_anchor_bank(queue, index)
    if not bank:
        raise RuntimeError("no verified Chinese anchor is available; original audio was preserved")
    print("Context anchors:")
    for number, anchor in enumerate(bank, 1):
        print(f"  {number}. {anchor['text']}  [{anchor['duration_ms']} ms]")

    staged = original.with_name(
        f".{original.stem}.context-repair-{time.time_ns()}{original.suffix or '.wav'}")
    repair_item = dict(target)
    repair_item.update({
        "filename": str(staged),
        "chinese_anchor_bank": bank,
        "chinese_anchor_ref": bank[0]["wav"],
        "chinese_anchor_text": bank[0]["text"],
    })
    for key in ("lang_leak", "quality_status", "quality_failures"):
        repair_item.pop(key, None)

    print(f"Repairing line {line}: {target.get('text', '')}")
    try:
        tts.run(
            queue_tts=[repair_item], language=language,
            tts_type=int(repair_item.get("tts_type") or 8),
            uuid=f"context-repair-{int(time.time())}", use_cache=False,
        )
        if not staged.is_file():
            raise RuntimeError("TTS backend returned without a candidate file")
        manifest = QualityManifest(root)
        entry = manifest.entries.get(unit_key(repair_item, index))
        passed = bool(
            isinstance(entry, dict)
            and entry.get("passed")
            and entry.get("audio_hash") == file_hash(staged)
            and entry.get("expected_text_hash") == text_hash(target.get("text") or "")
            and entry.get("rules_version") == F5TTS.QUALITY_RULES_VERSION
            and entry.get("validator_model") == F5TTS.VALIDATOR_MODEL
        )
        if not passed:
            transcript = str((entry or {}).get("transcript") or "")
            raise RuntimeError(
                f"strong quality review did not pass: {transcript[:240] or 'no transcript'}")
        duration_s = len(AudioSegment.from_file(staged)) / 1000.0
        os.replace(staged, original)
        target["dubbing_s"] = duration_s
        target["chinese_anchor_bank"] = bank
        for key in ("lang_leak", "quality_status", "quality_failures"):
            target.pop(key, None)
        atomic_write_json(queue_file, queue)
        _clear_marks(root, str(original), str(staged))
        print(f"PASS line {line}: {duration_s:.3f}s -> {original}")
        return original
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("queue_file", type=Path)
    parser.add_argument("--line", type=int, required=True, help="1-based line number")
    parser.add_argument("--language", default="zh-cn")
    args = parser.parse_args()
    try:
        repair(args.queue_file, args.line, args.language)
    except BaseException as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
