"""Low-overhead reference clip extraction for voice-cloning backends.

The normal path reads only the requested frames from a seekable audio file and
writes a 16 kHz mono WAV.  Callers retain FFmpeg as a compatibility fallback for
containers/codecs that libsndfile cannot seek.
"""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


def slice_reference_audio(
    source,
    start_ms,
    end_ms,
    target,
    *,
    sample_rate: int = 16000,
) -> str:
    """Extract ``start_ms:end_ms`` as mono PCM-16 WAV and return its path.

    Memory use is proportional to one reference clip, rather than the source
    video's duration.  The temporary file lives beside the target so replacing
    it is atomic on the same filesystem.
    """
    source_path = Path(source)
    target_path = Path(target)
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    sample_rate = int(sample_rate)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError(f"invalid reference range: {start_ms}->{end_ms}")
    if sample_rate <= 0:
        raise ValueError(f"invalid sample rate: {sample_rate}")

    with sf.SoundFile(str(source_path), mode="r") as audio:
        source_rate = int(audio.samplerate)
        start_frame = round(start_ms * source_rate / 1000)
        end_frame = round(end_ms * source_rate / 1000)
        if start_frame >= len(audio):
            raise ValueError(
                f"reference start exceeds source duration: {start_ms}ms"
            )
        audio.seek(start_frame)
        frames = audio.read(
            frames=max(1, min(end_frame, len(audio)) - start_frame),
            dtype="float32",
            always_2d=True,
        )
    if not frames.size:
        raise ValueError(f"empty reference range: {start_ms}->{end_ms}")

    mono = frames.mean(axis=1, dtype=np.float32)
    if source_rate != sample_rate:
        divisor = math.gcd(source_rate, sample_rate)
        mono = resample_poly(
            mono,
            sample_rate // divisor,
            source_rate // divisor,
        ).astype(np.float32, copy=False)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target_path.stem}-",
            suffix=".wav",
            dir=target_path.parent,
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
        sf.write(temp_name, mono, sample_rate, subtype="PCM_16", format="WAV")
        if Path(temp_name).stat().st_size <= 44:
            raise ValueError("reference clip contains no audio frames")
        os.replace(temp_name, target_path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    return str(target_path)
