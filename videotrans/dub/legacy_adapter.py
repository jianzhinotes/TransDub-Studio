"""旧 queue_tts 与 DubProject v2 之间的无损兼容层。"""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

from .schema import (
    AudioCandidate,
    DubProject,
    DubUnit,
    SpeakerTrack,
    TextCandidate,
)


_ID_NAMESPACE = uuid.UUID("c3831034-790c-4cf4-a859-53e447a433ea")


def make_project_id(source_video: str, target_language: str) -> str:
    identity = f"{Path(source_video or '').expanduser()}|{target_language or ''}"
    return str(uuid.uuid5(_ID_NAMESPACE, identity))


def _item_dict(item) -> dict:
    if isinstance(item, dict):
        return copy.deepcopy(item)
    if hasattr(item, "__dict__"):
        return copy.deepcopy(vars(item))
    if hasattr(item, "items"):
        return {k: copy.deepcopy(v) for k, v in item.items()}
    return copy.deepcopy(dict(item))


def _unit_id(project_id: str, item: dict, index: int) -> str:
    existing = str(item.get("dub_unit_id") or "").strip()
    if existing:
        return existing
    raw = "|".join(str(x) for x in (
        project_id,
        item.get("line", index + 1),
        item.get("start_time_source", item.get("start_time", 0)),
        item.get("end_time_source", item.get("end_time", 0)),
        item.get("ref_text", ""),
    ))
    return str(uuid.uuid5(_ID_NAMESPACE, raw))


def ensure_queue_unit_ids(queue: Iterable, project_id: str):
    """给 queue 条目补稳定 ID；原 list/dict 就地更新并返回。"""
    for index, item in enumerate(queue):
        item["dub_unit_id"] = _unit_id(project_id, _item_dict(item), index)
        if item.get("revision") is None:
            item["revision"] = 1
        if item.get("stale_reasons") is None:
            item["stale_reasons"] = []
    return queue


def _candidate_id(unit_id: str, kind: str, value: str) -> str:
    digest = hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:12]
    return f"{unit_id}:{kind}:{digest}"


def queue_to_units(queue: Iterable, project_id: str):
    rows = [_item_dict(item) for item in queue]
    ensure_queue_unit_ids(rows, project_id)
    units = []
    for index, item in enumerate(rows):
        unit_id = item["dub_unit_id"]
        text = str(item.get("text") or "")
        text_id = _candidate_id(unit_id, "text", text)
        text_candidate = TextCandidate(id=text_id, text=text, kind="legacy")

        audio_candidates = []
        selected_audio = None
        filename = str(item.get("filename") or "")
        if filename:
            audio_id = _candidate_id(unit_id, "audio", filename)
            duration = item.get("dubbing_s")
            duration_ms = int(float(duration) * 1000) if duration not in (None, "") else None
            audio_candidates.append(AudioCandidate(
                id=audio_id,
                text_candidate_id=text_id,
                path=filename,
                backend=str(item.get("tts_type", "legacy")),
                duration_ms=duration_ms,
            ))
            selected_audio = audio_id

        speaker = str(item.get("speaker_id") or item.get("spk") or "spk0")
        units.append(DubUnit(
            id=unit_id,
            speaker_id=speaker,
            source_start_ms=int(item.get("start_time_source", item.get("start_time", 0)) or 0),
            source_end_ms=int(item.get("end_time_source", item.get("end_time", 0)) or 0),
            source_text=str(item.get("ref_text") or ""),
            planned_start_ms=int(item.get("start_time", 0) or 0),
            planned_end_ms=int(item.get("end_time", 0) or 0),
            revision=int(item.get("revision", 1) or 1),
            stale_reasons=sorted(set(item.get("stale_reasons") or [])),
            selected_text_candidate_id=text_id,
            selected_audio_candidate_id=selected_audio,
            text_candidates=[text_candidate],
            audio_candidates=audio_candidates,
            legacy_payload=item,
            metadata={"legacy_index": index},
        ))
    return units


def project_from_queue(
        queue: Iterable,
        *,
        project_id: str,
        name: str,
        source_language: str,
        target_language: str,
        existing: Optional[DubProject] = None,
) -> DubProject:
    """从旧队列创建或同步 v2 项目，同时保留未来规划器写入的候选历史。"""
    incoming = queue_to_units(queue, project_id)
    previous = {unit.id: unit for unit in (existing.units if existing else [])}
    merged = []
    for unit in incoming:
        old = previous.get(unit.id)
        if old:
            # queue 仍是第一阶段的当前选择来源，但不能抹掉规划器的历史候选。
            old_by_text = {c.id: c for c in old.text_candidates}
            for candidate in unit.text_candidates:
                old_by_text[candidate.id] = candidate
            old_by_audio = {c.id: c for c in old.audio_candidates}
            for candidate in unit.audio_candidates:
                old_by_audio[candidate.id] = candidate
            unit.text_candidates = list(old_by_text.values())
            unit.audio_candidates = list(old_by_audio.values())
            unit.quality_reports = old.quality_reports
            unit.metadata = {**old.metadata, **unit.metadata}
        merged.append(unit)

    speakers = {}
    for unit in merged:
        speakers.setdefault(unit.speaker_id, SpeakerTrack(id=unit.speaker_id, name=unit.speaker_id))
    if existing:
        for speaker in existing.speakers:
            speakers[speaker.id] = speaker

    project = DubProject(
        project_id=project_id,
        name=name,
        source_language=source_language or "",
        target_language=target_language or "",
        created_at=existing.created_at if existing else int(time.time()),
        speakers=list(speakers.values()),
        source_turns=existing.source_turns if existing else [],
        units=merged,
        plans=existing.plans if existing else [],
        selected_plan_id=existing.selected_plan_id if existing else None,
        settings=existing.settings if existing else {},
        metadata=existing.metadata if existing else {},
    )
    return project


def units_to_queue(units: Iterable[DubUnit]) -> list:
    """把 v2 单元还原为旧队列；用于旧 align/render 兼容路径。"""
    queue = []
    for unit in units:
        item = copy.deepcopy(unit.legacy_payload)
        item["dub_unit_id"] = unit.id
        item["revision"] = unit.revision
        item["stale_reasons"] = list(unit.stale_reasons)
        item["start_time"] = unit.planned_start_ms
        item["end_time"] = unit.planned_end_ms
        item["start_time_source"] = unit.source_start_ms
        item["end_time_source"] = unit.source_end_ms
        item["speaker_id"] = unit.speaker_id

        selected_text = next(
            (c for c in unit.text_candidates if c.id == unit.selected_text_candidate_id), None)
        if selected_text:
            item["text"] = selected_text.text
        selected_audio = next(
            (c for c in unit.audio_candidates if c.id == unit.selected_audio_candidate_id), None)
        if selected_audio:
            item["filename"] = selected_audio.path
            if selected_audio.duration_ms is not None:
                item["dubbing_s"] = selected_audio.duration_ms / 1000.0
        queue.append(item)
    return queue


def plan_to_queue(project: DubProject, plan) -> list:
    """Materialize a selected joint plan as the legacy pre-TTS queue."""
    unit_map = {unit.id: unit for unit in project.units}
    covered = set()
    rows = []

    def timecode(ms):
        ms = max(int(ms or 0), 0)
        hours, rem = divmod(ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    for index, segment in enumerate(sorted(plan.segments, key=lambda s: (s.start_ms, s.end_ms))):
        units = [unit_map[uid] for uid in segment.unit_ids if uid in unit_map]
        if not units:
            continue
        selected = next(
            (candidate for candidate in segment.text_candidates
             if candidate.id == segment.selected_text_candidate_id), None)
        if selected is None:
            continue
        covered.update(unit.id for unit in units)
        item = copy.deepcopy(units[0].legacy_payload)
        start_ms = int(segment.start_ms)
        end_ms = int(segment.end_ms)
        item.update({
            "line": index + 1,
            "text": selected.text,
            "ref_text": segment.source_text,
            "start_time": start_ms,
            "end_time": end_ms,
            "startraw": timecode(start_ms),
            "endraw": timecode(end_ms),
            "start_time_source": min(unit.source_start_ms for unit in units),
            "end_time_source": max(unit.source_end_ms for unit in units),
            "dub_unit_id": segment.id,
            "planned_segment_id": segment.id,
            "source_unit_ids": list(segment.unit_ids),
        })
        old_output = Path(str(item.get("filename") or "candidate.wav"))
        digest = hashlib.sha1(
            f"{segment.id}|{selected.text}|{item.get('role')}|{item.get('tts_type')}".encode("utf-8")
        ).hexdigest()[:16]
        item["filename"] = str(old_output.parent / f"smart-{index}-{digest}.wav")
        if item.get("role") == "clone" or item.get("ref_wav"):
            item["ref_wav"] = str(old_output.parent / f"clone-smart-{index}.wav")
        rows.append(item)

    # Limited preview plans must not silently drop the untouched tail.
    rows.extend(units_to_queue(unit for unit in project.units if unit.id not in covered))
    for index, item in enumerate(rows):
        item["line"] = index + 1
    return rows
