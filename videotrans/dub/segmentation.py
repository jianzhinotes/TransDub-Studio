"""以说话轮次为边界生成少量可解释的语义分段方案。"""

import hashlib
from dataclasses import dataclass, field
from typing import List

from .schema import DubUnit, SourceTurn


@dataclass
class PlanningGroup:
    id: str
    unit_ids: List[str]
    speaker_id: str
    start_ms: int
    end_ms: int
    source_text: str
    baseline_text: str


@dataclass
class SegmentationOption:
    id: str
    kind: str
    groups: List[PlanningGroup] = field(default_factory=list)
    boundary_cost: float = 0.0


def selected_text(unit: DubUnit) -> str:
    selected = next(
        (candidate for candidate in unit.text_candidates
         if candidate.id == unit.selected_text_candidate_id), None)
    if selected:
        return selected.text
    return str(unit.legacy_payload.get("text") or "")


def _join_text(parts, separator="，"):
    output = ""
    for part in (str(value or "").strip() for value in parts):
        if not part:
            continue
        if output and output[-1:] not in "，。！？；：,.!?;:" and part[:1] not in "，。！？；：,.!?;:":
            output += separator
        output += part
    return output


def _group(units):
    raw = "|".join(unit.id for unit in units)
    gid = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return PlanningGroup(
        id=f"segment-{gid}",
        unit_ids=[unit.id for unit in units],
        speaker_id=units[0].speaker_id,
        start_ms=units[0].planned_start_ms,
        end_ms=units[-1].planned_end_ms,
        source_text=_join_text([unit.source_text for unit in units], separator=" "),
        baseline_text=_join_text([selected_text(unit) for unit in units]),
    )


def _short(unit):
    duration = unit.planned_end_ms - unit.planned_start_ms
    text = selected_text(unit)
    return duration < 1500 or len(text.strip("，。！？,.!? ")) <= 6


def build_segmentation_options(
        turn: SourceTurn,
        unit_map,
        *,
        merge_gap_ms: int = 700,
        max_group_ms: int = 9000,
):
    units = [unit_map[unit_id] for unit_id in turn.unit_ids if unit_id in unit_map]
    if not units:
        return []
    baseline_groups = [_group([unit]) for unit in units]
    short_count = sum(1 for unit in units if _short(unit))
    options = [SegmentationOption(
        id=f"{turn.id}:baseline",
        kind="baseline",
        groups=baseline_groups,
        boundary_cost=short_count * 0.04,
    )]

    merged_groups = []
    merge_count = 0
    i = 0
    while i < len(units):
        current = [units[i]]
        if i + 1 < len(units):
            nxt = units[i + 1]
            gap = nxt.planned_start_ms - units[i].planned_end_ms
            combined_ms = nxt.planned_end_ms - units[i].planned_start_ms
            if ((_short(units[i]) or _short(nxt))
                    and gap <= merge_gap_ms
                    and combined_ms <= max_group_ms
                    and nxt.speaker_id == units[i].speaker_id):
                current.append(nxt)
                merge_count += 1
                i += 1
        merged_groups.append(_group(current))
        i += 1

    if merge_count:
        options.append(SegmentationOption(
            id=f"{turn.id}:merge-short",
            kind="merge_short",
            groups=merged_groups,
            # 合并会增加少量边界改动成本，但消除短句接缝通常更自然。
            boundary_cost=merge_count * 0.025,
        ))
    return options
