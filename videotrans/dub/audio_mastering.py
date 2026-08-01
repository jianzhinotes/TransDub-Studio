"""Dialogue-first mastering for dubbed long videos.

The generated dialogue must remain intelligible without making separated music
and effects sound lifeless.  The primary graph uses the dialogue as a sidechain
to duck the background only while somebody speaks, then mixes without FFmpeg's
automatic volume division and applies a true peak guard.  Two progressively more
compatible graphs keep old FFmpeg builds usable without returning to full-volume
background mixing.
"""

from __future__ import annotations

from pathlib import Path


MIX_POLICY_VERSION = "dialogue-first-v1"


def _clamp(value, lower, upper, fallback):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(fallback)
    return max(float(lower), min(float(upper), value))


def dialogue_first_filters(
        *, background_volume=0.8, threshold=0.025, ratio=8.0,
        attack_ms=15, release_ms=280, limiter=0.891) -> list[tuple[str, str]]:
    """Return primary and compatibility filter graphs in preference order."""
    volume = _clamp(background_volume, 0.0, 1.2, 0.8)
    threshold = _clamp(threshold, 0.001, 0.2, 0.025)
    ratio = _clamp(ratio, 1.0, 20.0, 8.0)
    attack = int(_clamp(attack_ms, 1, 100, 15))
    release = int(_clamp(release_ms, 20, 2000, 280))
    peak = _clamp(limiter, 0.5, 0.98, 0.891)
    audio_format = (
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    )
    ducked = (
        f"[0:a]{audio_format},asplit=2[dialogue_mix][dialogue_sc];"
        f"[1:a]{audio_format},volume={volume:.3f}[background];"
        f"[background][dialogue_sc]sidechaincompress="
        f"threshold={threshold:.4f}:ratio={ratio:.2f}:"
        f"attack={attack}:release={release}:detection=rms[ducked];"
        "[dialogue_mix][ducked]amix=inputs=2:duration=first:"
        f"dropout_transition=0:normalize=0,alimiter=limit={peak:.3f}:"
        "attack=5:release=50[out]"
    )
    static_limited = (
        f"[0:a]{audio_format}[dialogue];"
        f"[1:a]{audio_format},volume={volume:.3f}[background];"
        "[dialogue][background]amix=inputs=2:duration=first:"
        f"dropout_transition=0:normalize=0,alimiter=limit={peak:.3f}:"
        "attack=5:release=50[out]"
    )
    static_compat = (
        f"[1:a]volume={volume:.3f}[background];"
        "[0:a][background]amix=inputs=2:duration=first:"
        "dropout_transition=0[out]"
    )
    return [
        ("dialogue_ducking", ducked),
        ("static_limited", static_limited),
        ("static_compat", static_compat),
    ]


def _resolved_output(output, cmd_dir=None) -> Path:
    path = Path(str(output))
    if path.is_absolute() or not cmd_dir:
        return path
    return Path(cmd_dir) / path


def mix_dialogue_background(
        runner, *, dialogue, background, output, cmd_dir=None,
        background_volume=0.8, threshold=0.025, ratio=8.0,
        attack_ms=15, release_ms=280, limiter=0.891,
        enable_ducking=True) -> dict:
    """Mix dialogue/background with bounded fallbacks and return a report."""
    target = _resolved_output(output, cmd_dir)
    failures = []
    strategies = dialogue_first_filters(
        background_volume=background_volume,
        threshold=threshold,
        ratio=ratio,
        attack_ms=attack_ms,
        release_ms=release_ms,
        limiter=limiter,
    )
    if not bool(enable_ducking):
        strategies = strategies[1:]
    for mode, filter_graph in strategies:
        target.unlink(missing_ok=True)
        command = [
            "-y",
            "-i", str(dialogue),
            "-i", str(background),
            "-filter_complex", filter_graph,
            "-map", "[out]",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(output),
        ]
        try:
            runner(command, cmd_dir=cmd_dir)
            if not target.is_file() or target.stat().st_size <= 44:
                raise RuntimeError("audio mixer did not create a valid WAV")
            return {
                "policy_version": MIX_POLICY_VERSION,
                "mode": mode,
                "background_volume": _clamp(
                    background_volume, 0.0, 1.2, 0.8),
                "peak_limit": _clamp(limiter, 0.5, 0.98, 0.891),
                "fallback_count": len(failures),
                "fallback_errors": failures,
                "ducking_enabled": bool(enable_ducking),
                "output": str(target),
            }
        except Exception as error:
            failures.append(f"{mode}: {error}")
    raise RuntimeError("；".join(failures))
