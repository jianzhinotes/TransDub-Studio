"""翻译、分段、目标时长、TTS 和质量反馈的联合规划闭环。"""

import copy
import hashlib
import uuid
from pathlib import Path

from .backends.base import SynthesisRequest
from .constraints import QualityProfile, candidate_loss, stretch_ratio
from .duration import DurationModel
from .quality import evaluate_audio
from .schema import (
    AudioCandidate,
    PlannedSegment,
    PlanningRevision,
    QualityReport,
)
from .segmentation import build_segmentation_options
from .source_analysis import build_source_turns, merge_turns
from .translation import ChineseCandidateGenerator


class JointDubPlanner:
    def __init__(self, *, profile=None, candidate_generator=None, duration_model=None):
        self.profile = profile or QualityProfile()
        self.candidate_generator = candidate_generator or ChineseCandidateGenerator()
        self.duration_model = duration_model

    def optimize(self, project, *, limit=20, backend=None,
                 candidate_dir=None, synthesize=False):
        units = sorted(project.units, key=lambda unit: (unit.planned_start_ms, unit.planned_end_ms))
        scope_units = units[:max(int(limit), 0)] if limit is not None else units
        if not scope_units:
            raise ValueError("No dubbing units available for joint planning")
        scope_ids = [unit.id for unit in scope_units]
        unit_map = {unit.id: unit for unit in project.units}
        duration_model = self.duration_model or DurationModel.from_project(project)

        turns = build_source_turns(project, scope_unit_ids=scope_ids)
        project.source_turns = merge_turns(project.source_turns, turns, scope_ids)

        selected_segments = []
        selected_kinds = []
        turn_choices = {}
        generator_diagnostics = {}
        total_score = 0.0
        for turn in turns:
            options = build_segmentation_options(turn, unit_map)
            turn_candidates = self._generate_turn_candidates(
                project, turn, options, duration_model)
            diagnostics = getattr(self.candidate_generator, "last_diagnostics", None)
            if diagnostics:
                generator_diagnostics[turn.id] = copy.deepcopy(diagnostics)
            evaluated = []
            for option in options:
                segments, score = self._evaluate_option(
                    option, duration_model, turn_candidates.get(option.id, {}))
                evaluated.append((score, option, segments))
            if not evaluated:
                continue
            score, option, segments = min(evaluated, key=lambda row: row[0])
            selected_segments.extend(segments)
            selected_kinds.append(option.kind)
            turn_choices[turn.id] = {
                "selected": option.kind,
                "alternatives": {item[1].kind: round(item[0], 6) for item in evaluated},
            }
            total_score += score

        plan = PlanningRevision(
            id=str(uuid.uuid4()),
            scope_unit_ids=scope_ids,
            segmentation_kind="+".join(sorted(set(selected_kinds))),
            segments=selected_segments,
            score=round(total_score, 6),
            status="planned",
            metadata={
                "turn_choices": turn_choices,
                "quality_profile": {
                    "preferred_stretch": self.profile.preferred_stretch,
                    "max_stretch": self.profile.max_stretch,
                },
                "limit": limit,
                "candidate_generator": getattr(
                    self.candidate_generator, "name",
                    self.candidate_generator.__class__.__name__),
                "generator_diagnostics": generator_diagnostics,
            },
        )
        project.plans.append(plan)
        project.selected_plan_id = plan.id

        if synthesize:
            if backend is None or not candidate_dir:
                raise ValueError("backend and candidate_dir are required when synthesize=True")
            self._synthesize_plan(
                project, plan, backend=backend,
                candidate_dir=Path(candidate_dir), unit_map=unit_map)
        return plan

    def _generate_turn_candidates(self, project, turn, options, duration_model):
        generate_turn = getattr(self.candidate_generator, "generate_turn", None)
        if callable(generate_turn):
            return generate_turn(
                turn=turn,
                options=options,
                target_language=project.target_language,
                duration_model=duration_model,
            )
        # 兼容早期自定义逐段候选器。
        return {
            option.id: {
                group.id: self.candidate_generator.generate(
                    segment_id=group.id,
                    baseline_text=group.baseline_text,
                    target_duration_ms=max(group.end_ms - group.start_ms, 1),
                    speaker_id=group.speaker_id,
                    duration_model=duration_model,
                )
                for group in option.groups
            }
            for option in options
        }

    def _evaluate_option(self, option, duration_model, generated):
        segments = []
        total = float(option.boundary_cost)
        for group in option.groups:
            window_ms = max(group.end_ms - group.start_ms, 1)
            candidates = generated.get(group.id, [])
            if not candidates:
                candidates = ChineseCandidateGenerator().generate(
                    segment_id=group.id,
                    baseline_text=group.baseline_text,
                    target_duration_ms=window_ms,
                    speaker_id=group.speaker_id,
                    duration_model=duration_model,
                )
            losses = {
                candidate.id: candidate_loss(candidate, window_ms, self.profile)
                for candidate in candidates
            }
            selected = min(candidates, key=lambda candidate: losses[candidate.id])
            total += losses[selected.id]
            segments.append(PlannedSegment(
                id=group.id,
                unit_ids=group.unit_ids,
                speaker_id=group.speaker_id,
                start_ms=group.start_ms,
                end_ms=group.end_ms,
                source_text=group.source_text,
                text_candidates=candidates,
                selected_text_candidate_id=selected.id,
                metrics={
                    "candidate_losses": {key: round(value, 6) for key, value in losses.items()},
                    "predicted_duration_ms": selected.estimated_duration_ms,
                    "predicted_stretch_ratio": round(
                        stretch_ratio(selected.estimated_duration_ms or window_ms, window_ms), 4),
                },
            ))
        return segments, total

    @staticmethod
    def _selected_text(segment):
        return next(
            candidate for candidate in segment.text_candidates
            if candidate.id == segment.selected_text_candidate_id)

    @staticmethod
    def _audio_id(request, artifact):
        digest = hashlib.sha1(
            f"{request.text_candidate_id}|{artifact.path}|{artifact.duration_ms}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{request.segment_id}:audio:{digest}"

    @staticmethod
    def _output_path(candidate_dir, segment, candidate, attempt):
        segment_key = hashlib.sha1(segment.id.encode("utf-8")).hexdigest()[:12]
        text_key = hashlib.sha1(candidate.id.encode("utf-8")).hexdigest()[:12]
        return candidate_dir / segment_key / f"{text_key}-a{attempt}.wav"

    def _request(self, project, segment, candidate, attempt, candidate_dir, unit_map):
        payload = copy.deepcopy(unit_map[segment.unit_ids[0]].legacy_payload)
        output = self._output_path(candidate_dir, segment, candidate, attempt)
        request_id = f"{segment.id}|{candidate.id}|{attempt}"
        return SynthesisRequest(
            id=request_id,
            segment_id=segment.id,
            text_candidate_id=candidate.id,
            text=candidate.text,
            output_path=str(output),
            language=project.target_language,
            speaker_id=segment.speaker_id,
            legacy_payload=payload,
            settings={"attempt": attempt},
        )

    @staticmethod
    def _safe_synthesize(backend, requests):
        """批量失败时二分隔离坏段，避免一段失败拖垮其余十九段。"""
        artifacts = {}
        errors = {}

        def run(batch):
            if not batch:
                return
            try:
                returned = backend.synthesize_batch(batch)
                found = {artifact.request_id: artifact for artifact in returned}
                for request in batch:
                    if request.id in found:
                        artifacts[request.id] = found[request.id]
                    else:
                        errors[request.id] = "backend returned no artifact"
            except Exception as error:
                if len(batch) == 1 or not backend.should_isolate_failure(batch, error):
                    for request in batch:
                        errors[request.id] = str(error)
                else:
                    middle = len(batch) // 2
                    run(batch[:middle])
                    run(batch[middle:])

        run(list(requests))
        return artifacts, errors

    def _retry_candidate(self, segment, current, attempted):
        alternatives = [
            candidate for candidate in segment.text_candidates
            if candidate.id not in attempted
            and (candidate.estimated_duration_ms or 10**9) < (current.estimated_duration_ms or 10**9)
            and float(candidate.semantic_score or 0) >= float(current.semantic_score or 0) - 0.10
        ]
        if not alternatives:
            return None
        return min(
            alternatives,
            key=lambda candidate: candidate_loss(
                candidate, segment.end_ms - segment.start_ms, self.profile),
        )

    def _synthesize_plan(self, project, plan, *, backend, candidate_dir, unit_map):
        candidate_dir.mkdir(parents=True, exist_ok=True)
        attempted = {segment.id: set() for segment in plan.segments}
        chosen_ratios = {}
        pending = []
        request_context = {}
        for segment in plan.segments:
            candidate = self._selected_text(segment)
            request = self._request(
                project, segment, candidate, 1, candidate_dir, unit_map)
            attempted[segment.id].add(candidate.id)
            request_context[request.id] = (segment, candidate, 1)
            pending.append(request)

        for attempt_round in range(1, self.profile.max_audio_attempts + 1):
            artifacts, errors = self._safe_synthesize(backend, pending)
            retry_requests = []
            for request in pending:
                segment, candidate, attempt = request_context[request.id]
                artifact = artifacts.get(request.id)
                if artifact is None:
                    segment.quality_reports.append(QualityReport(
                        id=str(uuid.uuid4()),
                        unit_id=segment.id,
                        passed=False,
                        hard_failures=["synthesis_error"],
                        metrics={"error": errors.get(request.id, "unknown synthesis error")},
                    ))
                    continue

                audio = AudioCandidate(
                    id=self._audio_id(request, artifact),
                    text_candidate_id=candidate.id,
                    path=artifact.path,
                    backend=artifact.backend,
                    duration_ms=artifact.duration_ms,
                    settings=request.settings,
                )
                segment.audio_candidates.append(audio)
                report = evaluate_audio(segment, artifact, self.profile)
                report.audio_candidate_id = audio.id
                segment.quality_reports.append(report)
                ratio = report.metrics["stretch_ratio"]

                # 先保留任何合格结果；更短候选只有实际改善时才替换。
                previous_ratio = chosen_ratios.get(segment.id)
                if report.passed and (previous_ratio is None or ratio < previous_ratio):
                    segment.selected_text_candidate_id = candidate.id
                    segment.selected_audio_candidate_id = audio.id
                    chosen_ratios[segment.id] = ratio
                    segment.metrics["actual_duration_ms"] = artifact.duration_ms
                    segment.metrics["actual_stretch_ratio"] = ratio

                should_retry = (
                    not report.passed or "stretch_above_preferred" in report.warnings
                )
                if should_retry and attempt_round < self.profile.max_audio_attempts:
                    shorter = self._retry_candidate(segment, candidate, attempted[segment.id])
                    if shorter is not None:
                        attempted[segment.id].add(shorter.id)
                        retry = self._request(
                            project, segment, shorter, attempt + 1,
                            candidate_dir, unit_map)
                        request_context[retry.id] = (segment, shorter, attempt + 1)
                        retry_requests.append(retry)
            pending = retry_requests
            if not pending:
                break

        missing = [segment.id for segment in plan.segments if not segment.selected_audio_candidate_id]
        plan.status = "failed" if missing else "validated"
        plan.metadata["backend"] = backend.name
        plan.metadata["failed_segment_ids"] = missing

    def synthesize_existing_plan(self, project, plan, *, backend, candidate_dir):
        """只合成既有规划，不重新分段、翻译或调用候选生成器。"""
        if plan not in project.plans:
            raise ValueError("Planning revision does not belong to this project")
        unit_map = {unit.id: unit for unit in project.units}
        missing_units = sorted({
            unit_id for segment in plan.segments for unit_id in segment.unit_ids
            if unit_id not in unit_map
        })
        if missing_units:
            raise ValueError(f"Plan references missing units: {', '.join(missing_units)}")
        self._synthesize_plan(
            project, plan, backend=backend,
            candidate_dir=Path(candidate_dir), unit_map=unit_map)
        project.selected_plan_id = plan.id
        project.touch()
        return plan
