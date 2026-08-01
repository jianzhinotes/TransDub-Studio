import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from videotrans.dub.audio_mastering import (
    dialogue_first_filters,
    mix_dialogue_background,
)
from videotrans.task.trans_create import TransCreate


def _fake_audio(path):
    path.write_bytes(b"RIFF" + b"0" * 80)
    return str(path)


def test_dialogue_first_graph_preserves_voice_and_guards_peaks():
    modes = dict(dialogue_first_filters(background_volume=9, limiter=9))
    graph = modes["dialogue_ducking"]

    assert "asplit=2" in graph
    assert "sidechaincompress=" in graph
    assert "volume=1.200" in graph
    assert "normalize=0" in graph
    assert "alimiter=limit=0.980" in graph


def test_mixer_uses_dynamic_ducking_by_default(tmp_path):
    dialogue = _fake_audio(tmp_path / "dialogue.wav")
    background = _fake_audio(tmp_path / "background.wav")
    output = tmp_path / "mix.wav"
    calls = []

    def runner(command, cmd_dir=None):
        calls.append((command, cmd_dir))
        Path(command[-1]).write_bytes(b"RIFF" + b"1" * 80)

    report = mix_dialogue_background(
        runner,
        dialogue=dialogue,
        background=background,
        output=output,
        cmd_dir=tmp_path,
        background_volume=0.24,
    )

    assert report["mode"] == "dialogue_ducking"
    assert report["fallback_count"] == 0
    assert report["ducking_enabled"] is True
    assert len(calls) == 1
    assert "sidechaincompress" in calls[0][0][6]


def test_mixer_falls_back_without_losing_background_volume_limit(tmp_path):
    output = tmp_path / "mix.wav"
    calls = []

    def runner(command, cmd_dir=None):
        calls.append(command)
        if "sidechaincompress" in command[6]:
            raise RuntimeError("filter unavailable")
        Path(command[-1]).write_bytes(b"RIFF" + b"2" * 80)

    report = mix_dialogue_background(
        runner,
        dialogue="dialogue.wav",
        background="background.wav",
        output=output,
        background_volume=0.3,
    )

    assert report["mode"] == "static_limited"
    assert report["fallback_count"] == 1
    assert "volume=0.300" in calls[1][6]
    assert "alimiter" in calls[1][6]


def test_disabling_ducking_starts_with_static_peak_protected_mix(tmp_path):
    output = tmp_path / "mix.wav"
    calls = []

    def runner(command, cmd_dir=None):
        calls.append(command)
        Path(command[-1]).write_bytes(b"RIFF" + b"3" * 80)

    report = mix_dialogue_background(
        runner,
        dialogue="dialogue.wav",
        background="background.wav",
        output=output,
        enable_ducking=False,
    )

    assert report["mode"] == "static_limited"
    assert report["ducking_enabled"] is False
    assert "sidechaincompress" not in calls[0][6]


def test_mix_report_is_saved_with_output_and_editable_project(tmp_path):
    cache = tmp_path / "cache"
    output = tmp_path / "output"
    cache.mkdir()
    output.mkdir()
    task = SimpleNamespace(
        cfg=SimpleNamespace(
            cache_folder=str(cache),
            target_dir=str(output),
            noextname="interview",
        )
    )

    TransCreate._record_audio_mix(
        task, "separated_background",
        {"mode": "dialogue_ducking", "fallback_count": 0},
    )

    assert (cache / "audio_mix_report.json").is_file()
    assert (output / "audio_mix_report.json").is_file()
    assert (output / "interview.tdproj" / "audio_mix_report.json").is_file()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_real_ffmpeg_ducks_background_during_dialogue_and_avoids_clipping(tmp_path):
    sample_rate = 48000
    timeline = np.arange(sample_rate * 3, dtype=np.float32) / sample_rate
    dialogue = np.zeros_like(timeline)
    speaking = (timeline >= 1.0) & (timeline < 2.0)
    dialogue[speaking] = 0.25 * np.sin(2 * np.pi * 400 * timeline[speaking])
    background = 0.1 * np.sin(2 * np.pi * 1000 * timeline)
    dialogue_path = tmp_path / "dialogue.wav"
    background_path = tmp_path / "background.wav"
    output = tmp_path / "mix.wav"
    sf.write(dialogue_path, dialogue, sample_rate)
    sf.write(background_path, background, sample_rate)

    def runner(command, cmd_dir=None):
        subprocess.run(
            [shutil.which("ffmpeg"), *command], cwd=cmd_dir,
            check=True, capture_output=True,
        )

    report = mix_dialogue_background(
        runner,
        dialogue=dialogue_path,
        background=background_path,
        output=output,
        background_volume=0.8,
    )
    mixed, rate = sf.read(output, always_2d=True)
    mono = mixed.mean(axis=1)

    def bin_energy(start, end, frequency):
        segment = mono[int(start * rate):int(end * rate)]
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(segment.size)))
        frequencies = np.fft.rfftfreq(segment.size, 1 / rate)
        return spectrum[np.argmin(np.abs(frequencies - frequency))]

    assert report["mode"] == "dialogue_ducking"
    assert bin_energy(1.3, 1.8, 1000) < bin_energy(0.2, 0.7, 1000) * 0.8
    assert np.max(np.abs(mixed)) <= 0.91
