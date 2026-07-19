"""从 DubUnit 构造说话轮次和基础韵律特征。"""

import re
import uuid

from .schema import DubProject, SourceTurn, WordTiming


def _word_timings(unit):
    words = []
    for raw in unit.legacy_payload.get("words") or []:
        text = str(raw.get("word") or raw.get("text") or "").strip()
        if not text:
            continue
        if "start_ms" in raw:
            start_ms = int(raw["start_ms"])
            end_ms = int(raw.get("end_ms", start_ms))
        else:
            # Whisper/MLX 常用秒；明显较大的值视作已经是毫秒。
            start = float(raw.get("start", 0) or 0)
            end = float(raw.get("end", start) or start)
            seconds_like = max(abs(start), abs(end)) <= (unit.source_end_ms / 1000.0 + 10)
            factor = 1000 if seconds_like else 1
            start_ms, end_ms = int(start * factor), int(end * factor)
        words.append(WordTiming(
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
            confidence=raw.get("probability", raw.get("confidence")),
        ))
    return words


def _turn_id(project_id, units):
    raw = f"{project_id}|{'|'.join(unit.id for unit in units)}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def build_source_turns(
        project: DubProject,
        *,
        scope_unit_ids=None,
        max_gap_ms: int = 900,
        max_turn_ms: int = 30000,
):
    allowed = set(scope_unit_ids) if scope_unit_ids is not None else None
    units = [unit for unit in project.units if allowed is None or unit.id in allowed]
    units.sort(key=lambda unit: (unit.source_start_ms, unit.source_end_ms))
    groups = []
    current = []
    for unit in units:
        if not current:
            current = [unit]
            continue
        previous = current[-1]
        gap = unit.source_start_ms - previous.source_end_ms
        total = unit.source_end_ms - current[0].source_start_ms
        if (unit.speaker_id == previous.speaker_id
                and gap <= max_gap_ms
                and total <= max_turn_ms):
            current.append(unit)
        else:
            groups.append(current)
            current = [unit]
    if current:
        groups.append(current)

    turns = []
    for group in groups:
        words = [word for unit in group for word in _word_timings(unit)]
        gaps = [max(0, group[i].source_start_ms - group[i - 1].source_end_ms)
                for i in range(1, len(group))]
        source_text = " ".join(unit.source_text.strip() for unit in group if unit.source_text.strip())
        duration_ms = max(group[-1].source_end_ms - group[0].source_start_ms, 1)
        token_count = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[\u4e00-\u9fff]", source_text))
        turns.append(SourceTurn(
            id=_turn_id(project.project_id, group),
            speaker_id=group[0].speaker_id,
            start_ms=group[0].source_start_ms,
            end_ms=group[-1].source_end_ms,
            source_text=source_text,
            words=words,
            unit_ids=[unit.id for unit in group],
            prosody={
                "inter_unit_gaps_ms": gaps,
                "source_units_per_second": round(token_count * 1000 / duration_ms, 3),
            },
        ))
    return turns


def merge_turns(existing, replacements, scope_unit_ids):
    """用本次范围的新 turn 替换与范围相交的旧 turn。"""
    scope = set(scope_unit_ids)
    kept = [turn for turn in existing if not scope.intersection(turn.unit_ids)]
    return sorted(kept + list(replacements), key=lambda turn: (turn.start_ms, turn.end_ms))
