"""Disposable strong-ASR worker used by the dubbing quality gate."""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from videotrans.dub.store import atomic_write_json


CHECKPOINT_SCHEMA_VERSION = 1


def _progress(path, text):
    if not path:
        return
    try:
        Path(path).write_text(
            json.dumps({"type": "logs", "text": text}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_checkpoint(path, identity):
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("identity") != identity:
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_checkpoint(path, identity, entries):
    if not path:
        return
    atomic_write_json(Path(path), {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "identity": identity,
        "updated_at": int(time.time()),
        "entries": entries,
    })


def _row_parts(row):
    return (
        int(row[0]),
        str(row[1]),
        str(row[2]) if len(row) > 2 else "",
    )


def validate_faster_whisper_files(
    *,
    files,
    model_path,
    cpu_threads=4,
    logs_file=None,
    checkpoint_file=None,
    checkpoint_identity=None,
):
    """Transcribe files one by one in a short-lived process.

    Returning from this worker destroys the CTranslate2 process and guarantees
    native allocations are reclaimed before F5 is started for any repair.
    """
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(model_path),
            device="cpu",
            compute_type="int8",
            cpu_threads=max(int(cpu_threads or 1), 1),
            num_workers=1,
        )
        identity = dict(checkpoint_identity or {})
        entries = _load_checkpoint(checkpoint_file, identity)
        transcripts = {}
        total = len(files)
        for pos, row in enumerate(files, 1):
            idx, filename, signature = _row_parts(row)
            cached = entries.get(signature) if signature else None
            if isinstance(cached, dict) and "transcript" in cached:
                transcripts[idx] = str(cached.get("transcript") or "")
                _progress(logs_file, f"强模型逐段核验 {pos}/{total}（断点复用）")
                continue
            segments, _ = model.transcribe(
                filename,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                temperature=0,
            )
            transcripts[idx] = "".join(segment.text for segment in segments).strip()
            if signature:
                entries[signature] = {
                    "transcript": transcripts[idx],
                    "updated_at": int(time.time()),
                }
                _save_checkpoint(checkpoint_file, identity, entries)
            _progress(logs_file, f"强模型逐段核验 {pos}/{total}")
        return transcripts, None
    except BaseException as error:
        return False, f"{error}\n{traceback.format_exc()}"


def validate_mlx_whisper_files(
        *, files, model_path, logs_file=None, checkpoint_file=None,
        checkpoint_identity=None, **_ignored):
    """Validate clips with the same large-v3-turbo weights via MLX/Metal."""
    try:
        import mlx_whisper

        identity = dict(checkpoint_identity or {})
        entries = _load_checkpoint(checkpoint_file, identity)
        transcripts = {}
        total = len(files)
        for pos, row in enumerate(files, 1):
            idx, filename, signature = _row_parts(row)
            cached = entries.get(signature) if signature else None
            if isinstance(cached, dict) and "transcript" in cached:
                transcripts[idx] = str(cached.get("transcript") or "")
                _progress(logs_file, f"MLX 强模型逐段核验 {pos}/{total}（断点复用）")
                continue
            result = mlx_whisper.transcribe(
                filename,
                path_or_hf_repo=str(model_path),
                language="zh",
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                temperature=0,
                word_timestamps=False,
                verbose=None,
            )
            transcripts[idx] = str(result.get("text") or "").strip()
            if signature:
                entries[signature] = {
                    "transcript": transcripts[idx],
                    "updated_at": int(time.time()),
                }
                _save_checkpoint(checkpoint_file, identity, entries)
            _progress(logs_file, f"MLX 强模型逐段核验 {pos}/{total}")
        return transcripts, None
    except BaseException as error:
        return False, f"{error}\n{traceback.format_exc()}"
