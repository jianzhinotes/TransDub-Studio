"""Persistent speaker identity contracts for voice-cloned dubbing.

The legacy pipeline used per-line source clips but did not persist who spoke
each line.  A later F5 batch could therefore choose one global reference for a
multi-speaker interview.  This module resolves speakers before joint planning,
selects one durable identity anchor per speaker, and annotates every row so the
TTS backend can keep identity and prosody as separate concerns.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


CONTRACT_VERSION = "speaker-identity-v1"
CONTRACT_FILE = "speaker_identity_contract.json"


def _row_key(item: dict, index: int) -> str:
    stable = str(item.get("dub_unit_id") or "").strip()
    if stable:
        return stable
    payload = "|".join(str(value) for value in (
        index,
        item.get("start_time_source", item.get("start_time", 0)),
        item.get("end_time_source", item.get("end_time", 0)),
        item.get("ref_text", ""),
    ))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def _source_signature(source_audio: str) -> str:
    try:
        path = Path(source_audio)
        stat = path.stat()
        return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        return str(source_audio or "")


def _contract_signature(rows: list[dict], source_audio: str) -> str:
    payload = {
        "version": CONTRACT_VERSION,
        "source": _source_signature(source_audio),
        "rows": [
            (
                _row_key(item, index),
                int(item.get("start_time_source", item.get("start_time", 0)) or 0),
                int(item.get("end_time_source", item.get("end_time", 0)) or 0),
                str(item.get("speaker_id") or item.get("spk") or ""),
                _source_signature(str(
                    item.get("speaker_identity_ref")
                    or item.get("cluster_ref")
                    or ""
                )),
            )
            for index, item in enumerate(rows)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _materialize_references(
        rows: list[dict], source_audio: str, reference_dir: Path) -> dict[int, str]:
    source = Path(str(source_audio or ""))
    if not source.is_file():
        return {}
    reference_dir.mkdir(parents=True, exist_ok=True)
    references = {}
    for index, item in enumerate(rows):
        if str(item.get("role") or "").strip().lower() != "clone":
            continue
        start_ms = int(
            item.get("start_time_source", item.get("start_time", 0)) or 0)
        end_ms = int(item.get("end_time_source", item.get("end_time", 0)) or 0)
        if end_ms <= start_ms:
            continue
        target = reference_dir / f"{index:04d}-{_row_key(item, index)}.wav"
        if not target.is_file() or target.stat().st_size <= 44:
            try:
                from .reference_audio import slice_reference_audio
                slice_reference_audio(source, start_ms, end_ms, target, sample_rate=16000)
            except Exception:
                continue
        references[index] = str(target)
    return references


def _explicit_speaker(item: dict) -> str:
    return str(item.get("speaker_id") or item.get("spk") or "").strip()


def _stable_labels(raw_labels: dict[int, int], indices: list[int]) -> dict[int, str]:
    first_seen = {}
    for position, row_index in enumerate(indices):
        if position in raw_labels:
            label = raw_labels[position]
            first_seen.setdefault(label, row_index)
    ordered = {
        label: f"spk{rank}"
        for rank, (label, _first) in enumerate(
            sorted(first_seen.items(), key=lambda pair: pair[1]))
    }
    return {
        row_index: ordered[raw_labels[position]]
        for position, row_index in enumerate(indices)
        if position in raw_labels and raw_labels[position] in ordered
    }


def _anchor_score(item: dict, wav: str) -> tuple:
    from pydub import AudioSegment

    clip = AudioSegment.from_file(wav)
    duration_ms = len(clip)
    text = str(item.get("ref_text") or "").strip()
    ideal_penalty = abs(duration_ms - 7500)
    range_penalty = 0 if 5000 <= duration_ms <= 9500 else 20_000
    statement_penalty = 2500 if text.endswith(("?", "？")) else 0
    fragment_penalty = 0 if text.endswith((".", "!", "?", "。", "！", "？")) else 1800
    quiet_penalty = 6000 if clip.dBFS != float("-inf") and clip.dBFS < -35 else 0
    clipping_penalty = 6000 if clip.max_dBFS > -0.2 else 0
    return (
        range_penalty + ideal_penalty + statement_penalty + fragment_penalty
        + quiet_penalty + clipping_penalty,
        -duration_ms,
    )


def _select_anchors(
        rows: list[dict], assignments: dict[int, str], references: dict[int, str]
) -> dict[str, dict]:
    provided = {}
    for index, speaker_id in assignments.items():
        item = rows[index]
        wav = str(
            item.get("speaker_identity_ref") or item.get("cluster_ref") or ""
        )
        owner = str(item.get("cluster_ref_speaker_id") or speaker_id)
        if wav and Path(wav).is_file() and owner == speaker_id:
            provided.setdefault(speaker_id, {
                "wav": wav,
                "text": str(
                    item.get("speaker_identity_text")
                    or item.get("cluster_ref_text")
                    or item.get("ref_text")
                    or ""
                ).strip(),
                "row_key": _row_key(item, index),
                "start_ms": int(item.get(
                    "start_time_source", item.get("start_time", 0)) or 0),
                "end_ms": int(item.get(
                    "end_time_source", item.get("end_time", 0)) or 0),
                "provided": True,
            })
    candidates = {}
    for index, speaker_id in assignments.items():
        wav = references.get(index)
        if not wav:
            continue
        try:
            score = _anchor_score(rows[index], wav)
        except Exception:
            continue
        duration_ms = (
            int(rows[index].get("end_time_source", rows[index].get("end_time", 0)) or 0)
            - int(rows[index].get("start_time_source", rows[index].get("start_time", 0)) or 0)
        )
        if duration_ms < 2500 or duration_ms > 12_000:
            continue
        candidates.setdefault(speaker_id, []).append((score, index, wav))

    anchors = dict(provided)
    for speaker_id, values in candidates.items():
        if speaker_id in anchors:
            continue
        _score, index, wav = min(values, key=lambda value: value[0])
        anchors[speaker_id] = {
            "wav": wav,
            "text": str(rows[index].get("ref_text") or "").strip(),
            "row_key": _row_key(rows[index], index),
            "start_ms": int(rows[index].get(
                "start_time_source", rows[index].get("start_time", 0)) or 0),
            "end_ms": int(rows[index].get(
                "end_time_source", rows[index].get("end_time", 0)) or 0),
        }
    return anchors


def _apply_contract(rows: list[dict], payload: dict) -> None:
    assignments = payload.get("assignments") or {}
    speakers = payload.get("speakers") or {}
    for index, item in enumerate(rows):
        speaker_id = str(assignments.get(_row_key(item, index)) or "").strip()
        if not speaker_id:
            continue
        item["speaker_id"] = speaker_id
        item["speaker_cluster_id"] = f"contract:{speaker_id}"
        speaker = speakers.get(speaker_id) or {}
        anchor = speaker.get("anchor") or {}
        wav = str(anchor.get("wav") or "")
        if wav and Path(wav).is_file():
            item["cluster_ref"] = wav
            item["cluster_ref_text"] = str(anchor.get("text") or "")
            item["cluster_ref_speaker_id"] = speaker_id
            item["speaker_identity_required"] = True
            item["speaker_identity_ref"] = wav
            item["speaker_identity_text"] = str(anchor.get("text") or "")
        item["speaker_identity_source"] = str(payload.get("method") or "unknown")


def prepare_speaker_contract(
        queue: Iterable[dict], *, source_audio: str, work_dir: str | Path) -> dict:
    """Annotate a copied TTS queue with stable speaker IDs and identity anchors.

    Existing diarization labels are authoritative.  Otherwise a completely
    local MFCC clustering pass is used.  Failure to separate is represented as
    one speaker, never as a guessed cross-speaker identity.
    """
    rows = list(queue)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    contract_path = work / CONTRACT_FILE
    signature = _contract_signature(rows, source_audio)
    if contract_path.is_file():
        try:
            cached = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if cached.get("signature") == signature:
            _apply_contract(rows, cached)
            if all(
                not speaker.get("anchor")
                or Path(str(speaker["anchor"].get("wav") or "")).is_file()
                for speaker in (cached.get("speakers") or {}).values()
            ):
                cached["cache_hit"] = True
                return cached

    clone_indices = [
        index for index, item in enumerate(rows)
        if str(item.get("role") or "").strip().lower() == "clone"
    ]
    references = _materialize_references(
        rows, source_audio, work / "source_refs")
    explicit = {
        index: _explicit_speaker(rows[index])
        for index in clone_indices if _explicit_speaker(rows[index])
    }

    assignments = dict(explicit)
    method = "explicit"
    unresolved_reason = ""
    if len(explicit) != len(clone_indices):
        cluster_indices = [index for index in clone_indices if index in references]
        raw_labels = None
        if len(cluster_indices) >= 6:
            try:
                from videotrans.util.speaker_cluster import label_speakers
                raw_labels = label_speakers(
                    [references[index] for index in cluster_indices]
                )
            except Exception as error:
                unresolved_reason = str(error)
        stable = _stable_labels(raw_labels or {}, cluster_indices)
        if stable and len(stable) >= max(6, round(len(cluster_indices) * 0.8)):
            method = "auto_mfcc"
            if explicit:
                votes = {}
                for index, speaker_id in explicit.items():
                    acoustic = stable.get(index)
                    if acoustic:
                        bucket = votes.setdefault(acoustic, {})
                        bucket[speaker_id] = bucket.get(speaker_id, 0) + 1
                mapped = {
                    acoustic: max(bucket, key=bucket.get)
                    for acoustic, bucket in votes.items()
                }
                used = set(explicit.values())
                for acoustic in sorted(set(stable.values())):
                    if acoustic in mapped:
                        continue
                    suffix = 0
                    candidate = f"spk_auto{suffix}"
                    while candidate in used:
                        suffix += 1
                        candidate = f"spk_auto{suffix}"
                    mapped[acoustic] = candidate
                    used.add(candidate)
                for index, acoustic in stable.items():
                    assignments.setdefault(index, mapped[acoustic])
            else:
                assignments.update(stable)
        else:
            method = "single_speaker_fallback"
            unresolved_reason = unresolved_reason or "no reliable multi-speaker separation"
            fallback_id = next(iter(explicit.values()), "spk0")
            for index in clone_indices:
                assignments.setdefault(index, fallback_id)

    anchors = _select_anchors(rows, assignments, references)
    durations = {}
    for index, speaker_id in assignments.items():
        start_ms = int(rows[index].get(
            "start_time_source", rows[index].get("start_time", 0)) or 0)
        end_ms = int(rows[index].get(
            "end_time_source", rows[index].get("end_time", 0)) or 0)
        durations[speaker_id] = durations.get(speaker_id, 0) + max(end_ms - start_ms, 0)
    speakers = {
        speaker_id: {
            "duration_ms": durations.get(speaker_id, 0),
            "anchor": anchors.get(speaker_id),
            "identity_ready": speaker_id in anchors,
        }
        for speaker_id in sorted(set(assignments.values()))
    }
    payload = {
        "version": CONTRACT_VERSION,
        "signature": signature,
        "status": "ready" if speakers and all(
            item["identity_ready"] for item in speakers.values()) else "degraded",
        "method": method,
        "unresolved_reason": unresolved_reason,
        "assignments": {
            _row_key(rows[index], index): speaker_id
            for index, speaker_id in assignments.items()
        },
        "speakers": speakers,
        "rows": len(rows),
        "clone_rows": len(clone_indices),
        "cache_hit": False,
    }
    from .store import atomic_write_json
    atomic_write_json(contract_path, payload)
    _apply_contract(rows, payload)
    return payload
