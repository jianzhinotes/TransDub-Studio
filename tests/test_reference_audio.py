from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from videotrans.dub.reference_audio import slice_reference_audio


def test_slice_reference_audio_resamples_and_downmixes(tmp_path):
    source_rate = 44100
    duration_s = 2.0
    time = np.arange(round(source_rate * duration_s)) / source_rate
    stereo = np.column_stack((
        0.4 * np.sin(2 * np.pi * 220 * time),
        0.2 * np.sin(2 * np.pi * 440 * time),
    )).astype(np.float32)
    source = tmp_path / "source.wav"
    target = tmp_path / "nested" / "reference.wav"
    sf.write(source, stereo, source_rate, subtype="PCM_16")

    result = slice_reference_audio(source, 250, 1250, target)

    assert Path(result) == target
    info = sf.info(target)
    assert info.samplerate == 16000
    assert info.channels == 1
    assert abs(info.frames - 16000) <= 1
    assert info.subtype == "PCM_16"


def test_slice_reference_audio_replaces_target_atomically(tmp_path):
    source = tmp_path / "source.wav"
    target = tmp_path / "reference.wav"
    sf.write(source, np.ones(32000, dtype=np.float32) * 0.1, 16000)
    target.write_bytes(b"old")

    slice_reference_audio(source, 0, 500, target)

    assert sf.info(target).frames == 8000
    assert not list(tmp_path.glob(".reference-*.wav"))


@pytest.mark.parametrize("start_ms,end_ms", [(-1, 100), (100, 100), (200, 100)])
def test_slice_reference_audio_rejects_invalid_ranges(tmp_path, start_ms, end_ms):
    source = tmp_path / "source.wav"
    sf.write(source, np.zeros(1600, dtype=np.float32), 16000)

    with pytest.raises(ValueError):
        slice_reference_audio(source, start_ms, end_ms, tmp_path / "out.wav")
