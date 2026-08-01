"""Context recovery for safe clip-level dubbing repair."""

from __future__ import annotations

import re
from pathlib import Path


def _speaker_identity(item: dict) -> str:
    return str(item.get("speaker_cluster_id") or item.get("cluster_ref") or "")


def _duration_ms(item: dict) -> int:
    try:
        seconds = float(item.get("dubbing_s") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        return int(seconds * 1000)
    try:
        from pydub import AudioSegment
        return len(AudioSegment.from_file(item.get("filename")))
    except Exception:
        return 0


def _style(text: str) -> str:
    text = str(text or "").strip()
    if text.endswith(("?", "？")):
        return "question"
    if text.endswith(("!", "！")):
        return "exclamation"
    return "statement"


def contextual_chinese_anchor_bank(
        queue: list[dict], target_index: int, *, max_anchors: int = 3) -> list[dict]:
    """Choose verified Chinese anchors from the full editing context.

    Explicit speaker IDs win. Older projects often lack them, so source-voice
    similarity ranks a bounded set of nearby clean clips; temporal proximity is
    the deterministic fallback. No model download or network access is needed.
    """
    if not 0 <= int(target_index) < len(queue):
        return []
    target = queue[int(target_index)]
    target_speaker = _speaker_identity(target)
    candidates = []
    for index, item in enumerate(queue):
        if index == target_index:
            continue
        filename = Path(str(item.get("filename") or ""))
        text = str(item.get("text") or "").strip()
        if not filename.is_file() or len(re.findall(r"[\u4e00-\u9fff]", text)) < 6:
            continue
        if item.get("lang_leak") or str(item.get("quality_status") or "").startswith("needs_"):
            continue
        speaker = _speaker_identity(item)
        if target_speaker and speaker != target_speaker:
            continue
        duration_ms = _duration_ms(item)
        if not 2500 <= duration_ms <= 12000:
            continue
        latin_penalty = 1 if re.search(r"[A-Za-z]", text) else 0
        candidates.append({
            "index": index,
            "wav": str(filename),
            "text": text if text.endswith(("。", "！", "？")) else text + "。",
            "duration_ms": duration_ms,
            "style": _style(text),
            "cjk_chars": len(re.findall(r"[\u4e00-\u9fff]", text)),
            "distance": abs(index - int(target_index)),
            "latin_penalty": latin_penalty,
            "ref_wav": str(item.get("ref_wav") or ""),
        })
    if not candidates:
        return []

    # Keep voice comparison bounded on long videos. The feature cache makes
    # repeated local repairs cheap, while a failure cleanly falls back to time.
    candidates.sort(key=lambda row: (row["distance"], row["latin_penalty"]))
    shortlist = candidates[:48]
    voice_rank = {}
    target_ref = str(target.get("ref_wav") or "")
    refs = [row["ref_wav"] for row in shortlist]
    if Path(target_ref).is_file() and sum(Path(ref).is_file() for ref in refs) >= 3:
        try:
            from videotrans.util.speaker_cluster import rank_similar_speakers
            ranked = rank_similar_speakers(target_ref, refs)
            voice_rank = {candidate_pos: rank for rank, candidate_pos in enumerate(ranked)}
        except Exception:
            voice_rank = {}

    target_style = _style(target.get("text"))
    ranked = sorted(enumerate(shortlist), key=lambda pair: (
        # When available, voice identity dominates proximity.
        voice_rank.get(pair[0], len(shortlist) + pair[1]["distance"]),
        pair[1]["latin_penalty"],
        0 if pair[1]["style"] == target_style else 1,
        pair[1]["distance"],
        abs(pair[1]["duration_ms"] - 6500),
    ))
    return [
        {key: row[key] for key in (
            "wav", "text", "duration_ms", "style", "cjk_chars")}
        for _position, row in ranked[:max(1, int(max_anchors))]
    ]
