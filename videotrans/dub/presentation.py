"""把联合规划结果转换为 UI/CLI 都能消费的只读视图数据。"""


def _candidate(segment, candidate_id):
    return next(
        (candidate for candidate in segment.text_candidates
         if candidate.id == candidate_id), None)


def build_plan_view(plan, project=None):
    unit_map = {unit.id: unit for unit in (project.units if project else [])}
    diagnostics = (plan.metadata or {}).get("generator_diagnostics") or {}
    diagnostic_counts = {"ok": 0, "partial": 0, "fallback": 0}
    cache_hits = 0
    for item in diagnostics.values():
        status = str(item.get("status") or "")
        if status in diagnostic_counts:
            diagnostic_counts[status] += 1
        cache_hits += int(bool(item.get("cache_hit")))

    rows = []
    for index, segment in enumerate(plan.segments):
        selected = _candidate(segment, segment.selected_text_candidate_id)
        baseline = next(
            (candidate for candidate in segment.text_candidates
             if candidate.kind in {"baseline", "legacy"}),
            segment.text_candidates[0] if segment.text_candidates else None,
        )
        selected_audio = next(
            (audio for audio in segment.audio_candidates
             if audio.id == segment.selected_audio_candidate_id), None)
        predicted_ms = int(
            segment.metrics.get("actual_duration_ms")
            or segment.metrics.get("predicted_duration_ms")
            or (selected.estimated_duration_ms if selected else 0) or 0)
        window_ms = max(int(segment.end_ms - segment.start_ms), 1)
        ratio = float(
            segment.metrics.get("actual_stretch_ratio")
            or segment.metrics.get("predicted_stretch_ratio")
            or predicted_ms / window_ms)
        current_audio_paths = []
        for unit_id in segment.unit_ids:
            unit = unit_map.get(unit_id)
            path = str((unit.legacy_payload if unit else {}).get("filename") or "")
            if path:
                current_audio_paths.append(path)
        if ratio > 1.25:
            risk = "overflow"
        elif ratio > 1.12:
            risk = "stretch"
        else:
            risk = "ok"
        details = []
        losses = segment.metrics.get("candidate_losses") or {}
        for candidate in segment.text_candidates:
            marker = "✓" if candidate.id == segment.selected_text_candidate_id else "·"
            duration = candidate.estimated_duration_ms or 0
            loss = losses.get(candidate.id)
            loss_text = f" loss={float(loss):.3f}" if loss is not None else ""
            details.append(
                f"{marker} {candidate.kind} | {duration} ms{loss_text}\n{candidate.text}")
        rows.append({
            "index": index + 1,
            "segment_id": segment.id,
            "unit_ids": list(segment.unit_ids),
            "unit_count": len(segment.unit_ids),
            "start_ms": int(segment.start_ms),
            "end_ms": int(segment.end_ms),
            "window_ms": window_ms,
            "source_text": segment.source_text,
            "baseline_text": baseline.text if baseline else "",
            "selected_text": selected.text if selected else "",
            "selected_kind": selected.kind if selected else "",
            "predicted_duration_ms": predicted_ms,
            "stretch_ratio": ratio,
            "risk": risk,
            "candidate_count": len(segment.text_candidates),
            "candidate_details": "\n\n".join(details),
            "current_audio_paths": current_audio_paths,
            "planned_audio_path": selected_audio.path if selected_audio else "",
            "audio_validated": bool(selected_audio),
        })

    return {
        "plan_id": plan.id,
        "status": plan.status,
        "score": float(plan.score),
        "segmentation_kind": plan.segmentation_kind,
        "candidate_generator": (plan.metadata or {}).get("candidate_generator", ""),
        "rows": rows,
        "diagnostic_counts": diagnostic_counts,
        "cache_hits": cache_hits,
        "risk_counts": {
            risk: sum(1 for row in rows if row["risk"] == risk)
            for risk in ("ok", "stretch", "overflow")
        },
    }
