"""Disposable strong-ASR worker used by the dubbing quality gate."""

from __future__ import annotations

import json
import traceback
from pathlib import Path


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


def validate_faster_whisper_files(
    *,
    files,
    model_path,
    cpu_threads=4,
    logs_file=None,
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
        transcripts = {}
        total = len(files)
        for pos, row in enumerate(files, 1):
            idx, filename = int(row[0]), str(row[1])
            segments, _ = model.transcribe(
                filename,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                temperature=0,
            )
            transcripts[idx] = "".join(segment.text for segment in segments).strip()
            _progress(logs_file, f"强模型逐段核验 {pos}/{total}")
        return transcripts, None
    except BaseException as error:
        return False, f"{error}\n{traceback.format_exc()}"


def validate_mlx_whisper_files(*, files, model_path, logs_file=None, **_ignored):
    """Validate clips with the same large-v3-turbo weights via MLX/Metal."""
    try:
        import mlx_whisper

        transcripts = {}
        total = len(files)
        for pos, row in enumerate(files, 1):
            idx, filename = int(row[0]), str(row[1])
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
            _progress(logs_file, f"MLX 强模型逐段核验 {pos}/{total}")
        return transcripts, None
    except BaseException as error:
        return False, f"{error}\n{traceback.format_exc()}"
